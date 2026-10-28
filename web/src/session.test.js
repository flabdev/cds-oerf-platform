import assert from "node:assert/strict";
import test from "node:test";

import {
  authorizeUrl,
  createVerifier,
  dashboardError,
  exchangeCode,
  exchangeOnce,
  liveConfig,
  loadIncidents,
  authFailure,
  clearStoredSession,
  logoutUrl,
  presentIncident,
  principalFromIdToken,
  sendDashboard,
  shouldOpenSocket,
  signOutTarget,
  socketAuthFailed,
  stripAuthCode,
} from "./session.js";

test("missing Cognito settings keep the sample desk", () => {
  assert.equal(liveConfig({}).mode, "demo");
  assert.equal(liveConfig({ VITE_API_URL: "https://api.example.com" }).mode, "demo");
});

test("a live config drops a trailing slash and the https prefix", () => {
  const config = liveConfig({
    VITE_API_URL: "https://api.example.com/",
    VITE_COGNITO_DOMAIN: "https://oerf.auth.us-east-1.amazoncognito.com/",
    VITE_USER_POOL_CLIENT_ID: "client-1",
  });
  assert.equal(config.mode, "live");
  assert.equal(config.apiUrl, "https://api.example.com");
  assert.equal(config.domain, "oerf.auth.us-east-1.amazoncognito.com");
  assert.equal(config.wsUrl, "");
});

test("dev live mode calls the Vite proxy instead of RestApiUrl", () => {
  const config = liveConfig({
    DEV: true,
    VITE_API_URL: "https://api.example.com/prod/",
    VITE_COGNITO_DOMAIN: "oerf.auth.us-east-1.amazoncognito.com",
    VITE_USER_POOL_CLIENT_ID: "client-1",
  });
  assert.equal(config.mode, "live");
  assert.equal(config.apiUrl, "/api");
});

test("the websocket url is the compute stack output", () => {
  const config = liveConfig({
    VITE_API_URL: "https://api.example.com",
    VITE_COGNITO_DOMAIN: "oerf.auth.us-east-1.amazoncognito.com",
    VITE_USER_POOL_CLIENT_ID: "client-1",
    VITE_WS_URL: "wss://abc.execute-api.us-east-1.amazonaws.com/prod/",
  });
  assert.equal(config.wsUrl, "wss://abc.execute-api.us-east-1.amazonaws.com/prod");
});

test("the id token supplies the tenant and role, and an expired token signs out", () => {
  const token = jwt({
    "custom:tenant_id": "t2",
    "cognito:groups": ["tenant_admin"],
    email: "lead@example.com",
    exp: 2_000,
  });
  const principal = principalFromIdToken(token, 1_000);
  assert.equal(principal.authenticated, true);
  assert.equal(principal.tenantId, "t2");
  assert.equal(principal.role, "tenant_admin");
  assert.equal(principal.name, "lead@example.com");
  assert.equal(principalFromIdToken(token, 3_000_000).authenticated, false);
});

test("the logout url is the hosted Cognito logout page", () => {
  const url = new URL(logoutUrl({
    domain: "oerf.auth.us-east-1.amazoncognito.com",
    clientId: "client-1",
    logoutUri: "http://localhost:5173",
  }));
  assert.equal(url.pathname, "/logout");
  assert.equal(url.searchParams.get("client_id"), "client-1");
  assert.equal(url.searchParams.get("logout_uri"), "http://localhost:5173");
});

test("sign-out uses the dashboard client and the app origin", () => {
  const target = signOutTarget({
    domain: "oerf.auth.us-east-1.amazoncognito.com",
    clientId: "dashboard-client",
    logoutUri: "https://d111.cloudfront.net",
  });
  const url = new URL(target);
  assert.equal(url.pathname, "/logout");
  assert.equal(url.searchParams.get("client_id"), "dashboard-client");
  assert.equal(url.searchParams.get("logout_uri"), "https://d111.cloudfront.net");
});

test("sign-out stays on the app when the hosted logout config is missing", () => {
  assert.equal(signOutTarget({ domain: "", clientId: "dashboard-client", logoutUri: "http://localhost:5173" }), "");
  assert.equal(signOutTarget({ domain: "oerf.auth.example.com", clientId: "", logoutUri: "http://localhost:5173" }), "");
  assert.equal(signOutTarget({ domain: "oerf.auth.example.com", clientId: "dashboard-client", logoutUri: "" }), "");
});

test("stored tokens are removed from both stores", () => {
  const primary = memoryStore();
  const extra = memoryStore();
  primary.setItem("oerf.idToken", "id");
  primary.setItem("oerf.accessToken", "access");
  primary.setItem("oerf.codeVerifier", "verifier");
  extra.setItem("oerf.idToken", "id");
  clearStoredSession(primary, extra);
  assert.equal(primary.getItem("oerf.idToken"), null);
  assert.equal(primary.getItem("oerf.accessToken"), null);
  assert.equal(primary.getItem("oerf.codeVerifier"), null);
  assert.equal(extra.getItem("oerf.idToken"), null);
});

test("the socket stays closed after sign-out and after an auth handshake failure", () => {
  assert.equal(shouldOpenSocket({
    signingOut: true,
    authenticated: true,
    accessToken: "access",
    wsUrl: "wss://example.test/prod",
  }), false);
  assert.equal(shouldOpenSocket({
    signingOut: false,
    authenticated: false,
    accessToken: "",
    wsUrl: "wss://example.test/prod",
  }), false);
  assert.equal(shouldOpenSocket({
    signingOut: false,
    authenticated: true,
    accessToken: "access",
    wsUrl: "wss://example.test/prod",
  }), true);
  assert.equal(socketAuthFailed({ opened: false, code: 1006, signingOut: false }), true);
  assert.equal(socketAuthFailed({ opened: false, code: 1006, signingOut: true }), false);
  assert.equal(socketAuthFailed({ opened: true, code: 1006, signingOut: false }), false);
});

test("the authorize url is the hosted Cognito page", async () => {
  const { challenge } = await createVerifier();
  const url = new URL(authorizeUrl({
    domain: "oerf.auth.us-east-1.amazoncognito.com",
    clientId: "client-1",
    redirectUri: "http://127.0.0.1:5173",
    challenge,
  }));
  assert.equal(url.pathname, "/oauth2/authorize");
  assert.equal(url.searchParams.get("client_id"), "client-1");
  assert.equal(url.searchParams.get("code_challenge_method"), "S256");
  assert.equal(
    url.searchParams.get("scope"),
    "openid email profile aws.cognito.signin.user.admin",
  );
});

test("incidents and actions use the bearer token and hide no task token", async () => {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({ url, options });
    if (url.endsWith("/actions")) {
      return { ok: true, status: 202, json: async () => ({ action_id: "act-1", status: "queued" }) };
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({
        incidents: [
          {
            incident_id: "INC-1",
            shipment_id: "PH-2841",
            state: "ASSESSING",
            timeline: [{ state: "OPEN", reason: "excursion" }],
          },
        ],
      }),
    };
  };
  const rows = await loadIncidents("https://api.example.com", "id-token", fetchImpl);
  assert.equal(rows[0].timeline[0], "OPEN excursion");
  assert.equal(calls[0].options.headers.Authorization, "Bearer id-token");
  const queued = await sendDashboard(
    "https://api.example.com",
    "id-token",
    {
      method: "POST",
      path: "/incidents/INC-1/actions",
      idempotencyKey: "act-1",
      body: { action: "cancel" },
    },
    fetchImpl,
  );
  assert.equal(queued.action_id, "act-1");
  assert.equal(calls[1].options.headers["Idempotency-Key"], "act-1");
  assert.equal(calls[1].options.body.includes("task_token"), false);
});

test("the code exchange keeps the id token for REST and the access token for WebSocket", async () => {
  const session = await exchangeCode({
    domain: "oerf.auth.us-east-1.amazoncognito.com",
    clientId: "client-1",
    redirectUri: "http://127.0.0.1:5173",
    code: "code-1",
    verifier: "verifier",
    fetchImpl: async () => ({
      ok: true,
      status: 200,
      json: async () => ({ id_token: "id-1", access_token: "access-1" }),
    }),
  });
  assert.equal(session.idToken, "id-1");
  assert.equal(session.accessToken, "access-1");
});

test("a failed code exchange does not invent a session", async () => {
  await assert.rejects(
    () => exchangeCode({
      domain: "oerf.auth.us-east-1.amazoncognito.com",
      clientId: "client-1",
      redirectUri: "http://127.0.0.1:5173",
      code: "code-1",
      verifier: "verifier",
      fetchImpl: async () => ({ ok: false, status: 400, json: async () => ({}) }),
    }),
    /Cognito did not return a session/,
  );
  assert.equal(presentIncident({ timeline: ["10:16 opened"] }).timeline[0], "10:16 opened");
});

test("a second exchange of the same code reuses the in-flight request", async () => {
  let calls = 0;
  let finish;
  const fetchImpl = () => new Promise((resolve) => {
    calls += 1;
    finish = resolve;
  });
  const args = {
    domain: "oerf.auth.us-east-1.amazoncognito.com",
    clientId: "client-1",
    redirectUri: "http://localhost:5173",
    code: "code-once",
    verifier: "verifier",
    fetchImpl,
  };
  const first = exchangeOnce(args);
  const second = exchangeOnce(args);
  finish({
    ok: true,
    status: 200,
    json: async () => ({ id_token: "id-1", access_token: "access-1" }),
  });
  const [one, two] = await Promise.all([first, second]);
  assert.equal(calls, 1);
  assert.equal(one.accessToken, "access-1");
  assert.equal(two.accessToken, "access-1");
});

test("the callback drops the authorization code from the URL", () => {
  const replaced = [];
  const stripped = stripAuthCode(
    { pathname: "/", search: "?code=abc&x=1", hash: "" },
    (...args) => replaced.push(args),
  );
  assert.equal(stripped, true);
  assert.equal(replaced[0][2], "/?x=1");
});

test("a 401 is an auth failure", async () => {
  const fetchImpl = async () => ({ ok: false, status: 401, json: async () => ({}) });
  await assert.rejects(
    () => loadIncidents("https://api.example.com", "token", fetchImpl),
    (error) => {
      assert.equal(error.status, 401);
      assert.equal(error.message, "Sign in required");
      assert.equal(authFailure(error), true);
      return true;
    },
  );
});

test("a rejected action names the HTTP status", async () => {
  assert.equal(dashboardError(403), "You don't have permission for this action");
  assert.equal(dashboardError(409), "Incident already changed state; refreshed");
  assert.equal(dashboardError(500), "Action failed (HTTP 500)");
  const fetchImpl = async () => ({ ok: false, status: 403, json: async () => ({}) });
  await assert.rejects(
    () => sendDashboard("https://api.example.com", "token", {
      method: "POST",
      path: "/incidents/INC-1/actions",
      body: { action: "cancel" },
    }, fetchImpl),
    (error) => {
      assert.equal(error.status, 403);
      assert.match(error.message, /permission/);
      return true;
    },
  );
});

function memoryStore() {
  const data = new Map();
  return {
    setItem(key, value) {
      data.set(key, value);
    },
    getItem(key) {
      return data.has(key) ? data.get(key) : null;
    },
    removeItem(key) {
      data.delete(key);
    },
  };
}

function jwt(payload) {
  const body = btoa(JSON.stringify(payload)).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
  return `header.${body}.sig`;
}
