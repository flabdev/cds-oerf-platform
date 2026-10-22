import assert from "node:assert/strict";
import test from "node:test";

import {
  authorizeUrl,
  createVerifier,
  exchangeCode,
  liveConfig,
  loadIncidents,
  presentIncident,
  principalFromIdToken,
  sendDashboard,
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

test("the code exchange keeps the access token for the HTTP API", async () => {
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

function jwt(payload) {
  const body = btoa(JSON.stringify(payload)).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
  return `header.${body}.sig`;
}
