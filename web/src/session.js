/** Cognito sign-in and dashboard API calls. A missing config keeps the sample desk. */

import { readTransitions } from "./states.js";

const GROUPS = "cognito:groups";
const TENANT = "custom:tenant_id";

export const ID_TOKEN_KEY = "oerf.idToken";
export const ACCESS_TOKEN_KEY = "oerf.accessToken";
export const VERIFIER_KEY = "oerf.codeVerifier";

const STORED_KEYS = [ID_TOKEN_KEY, ACCESS_TOKEN_KEY, VERIFIER_KEY];

export function liveConfig(env) {
  const apiUrl = trimSlash(env?.VITE_API_URL);
  const domain = trimSlash(env?.VITE_COGNITO_DOMAIN).replace(/^https:\/\//, "");
  // Dashboard hosted-UI client (OerfApiStack UserPoolClientId), not the HTTP API client.
  const clientId = String(env?.VITE_USER_POOL_CLIENT_ID || "").trim();
  // OerfComputeStack output WebSocketApiUrl. One WebSocket for the dashboard.
  const wsUrl = trimSlash(env?.VITE_WS_URL);
  if (!apiUrl || !domain || !clientId) {
    return { mode: "demo" };
  }
  // Vite proxies /api to RestApiUrl so localhost does not need API CORS.
  const liveApiUrl = env?.DEV ? "/api" : apiUrl;
  return { mode: "live", apiUrl: liveApiUrl, domain, clientId, wsUrl };
}

export function principalFromIdToken(token, now = Date.now()) {
  const payload = decodePayload(token);
  if (!payload || (payload.exp && payload.exp * 1000 <= now)) {
    return { authenticated: false, tenantId: "", role: "ops_user", name: "" };
  }
  const groups = parseGroups(payload[GROUPS]);
  let role = "ops_user";
  if (groups.includes("platform_admin")) {
    role = "platform_admin";
  } else if (groups.includes("tenant_admin")) {
    role = "tenant_admin";
  }
  return {
    authenticated: true,
    tenantId: String(payload[TENANT] || "").trim(),
    role,
    name: String(payload.email || payload["cognito:username"] || "").trim(),
  };
}

export function authorizeUrl({ domain, clientId, redirectUri, challenge }) {
  const url = new URL(`https://${domain}/oauth2/authorize`);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("client_id", clientId);
  url.searchParams.set("redirect_uri", redirectUri);
  url.searchParams.set("scope", "openid email profile aws.cognito.signin.user.admin");
  url.searchParams.set("code_challenge", challenge);
  url.searchParams.set("code_challenge_method", "S256");
  return url.toString();
}

export function logoutUrl({ domain, clientId, logoutUri }) {
  const url = new URL(`https://${domain}/logout`);
  url.searchParams.set("client_id", clientId);
  url.searchParams.set("logout_uri", logoutUri);
  return url.toString();
}

/** Cognito logout URL, or "" when the hosted-UI client, domain, or origin is missing. */
export function signOutTarget({ domain, clientId, logoutUri } = {}) {
  const host = String(domain || "").trim();
  const id = String(clientId || "").trim();
  const uri = String(logoutUri || "").trim();
  if (!host || !id || !uri) {
    return "";
  }
  return logoutUrl({ domain: host, clientId: id, logoutUri: uri });
}

export function clearStoredSession(primary = globalThis.sessionStorage, extra = globalThis.localStorage) {
  for (const key of STORED_KEYS) {
    primary?.removeItem?.(key);
    if (extra && extra !== primary) {
      extra.removeItem?.(key);
    }
  }
}

export function authFailure(error) {
  return error?.status === 401 || error?.message === "Sign in required";
}

export function shouldOpenSocket({ signingOut, authenticated, accessToken, wsUrl }) {
  return !signingOut && Boolean(authenticated) && Boolean(accessToken) && Boolean(wsUrl);
}

/** A handshake that never opened is an authorizer rejection. A later close is not. */
export function socketAuthFailed({ opened, code, signingOut }) {
  if (signingOut || opened) {
    return false;
  }
  return code === 1006 || code === 1008 || code === 4001 || code === 4401 || code === 4403;
}

export async function createVerifier() {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  const verifier = base64url(bytes);
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return { verifier, challenge: base64url(new Uint8Array(digest)) };
}

const exchangeFlight = { key: "", promise: null };

export function stripAuthCode(location, replaceState) {
  const params = new URLSearchParams(location.search);
  if (!params.has("code")) {
    return false;
  }
  params.delete("code");
  const query = params.toString();
  replaceState({}, "", `${location.pathname}${query ? `?${query}` : ""}${location.hash || ""}`);
  return true;
}

export async function exchangeCode({ domain, clientId, redirectUri, code, verifier, fetchImpl = fetch }) {
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: clientId,
    redirect_uri: redirectUri,
    code,
    code_verifier: verifier,
  });
  const response = await fetchImpl(`https://${domain}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!response.ok) {
    throw new Error("Cognito did not return a session. Sign in again.");
  }
  const payload = await response.json();
  if (!payload.access_token) {
    throw new Error("Cognito did not return a session");
  }
  return {
    idToken: payload.id_token || payload.access_token,
    accessToken: payload.access_token,
  };
}

export function exchangeOnce(args) {
  const key = [args.domain, args.clientId, args.redirectUri, args.code, args.verifier].join("\n");
  if (exchangeFlight.promise && exchangeFlight.key === key) {
    return exchangeFlight.promise;
  }
  exchangeFlight.key = key;
  exchangeFlight.promise = exchangeCode(args).finally(() => {
    if (exchangeFlight.key === key) {
      exchangeFlight.key = "";
      exchangeFlight.promise = null;
    }
  });
  return exchangeFlight.promise;
}

export async function loadIncidents(apiUrl, token, fetchImpl = fetch) {
  const body = await apiResult(apiUrl, token, "/incidents", { method: "GET" }, fetchImpl);
  return Array.isArray(body.incidents) ? body.incidents.map(presentIncident) : [];
}

export async function loadIncident(apiUrl, token, incidentId, fetchImpl = fetch) {
  const body = await apiResult(
    apiUrl,
    token,
    `/incidents/${encodeURIComponent(incidentId)}`,
    { method: "GET" },
    fetchImpl,
  );
  return presentIncident(body);
}

export async function sendDashboard(apiUrl, token, request, fetchImpl = fetch) {
  return apiResult(apiUrl, token, request.path, {
    method: request.method,
    headers: request.idempotencyKey ? { "Idempotency-Key": request.idempotencyKey } : {},
    body: JSON.stringify(request.body),
  }, fetchImpl);
}

export function localTime(value, timeZone) {
  if (typeof value !== "string" || !value.trim()) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    const match = value.match(/^(\d{2}:\d{2})/);
    return match ? match[1] : "";
  }
  return new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone,
  }).format(date);
}

export function presentIncident(item, timeZone) {
  const transitions = readTransitions({ timeline: item.timeline });
  const timeline = (item.timeline || []).map((entry) => presentTimelineEntry(entry, timeZone)).filter(Boolean);
  const clock = localTime(item.time, timeZone) || localTime(item.opened_at, timeZone);
  return {
    ...item,
    timeline,
    transitions,
    readings: Array.isArray(item.readings) ? item.readings : [],
    time: clock || (typeof item.time === "string" ? item.time : ""),
  };
}

export function dashboardError(status) {
  if (status === 403) {
    return "You don't have permission for this action";
  }
  if (status === 409) {
    return "Incident already changed state; refreshed";
  }
  return `Action failed (HTTP ${status})`;
}

function presentTimelineEntry(entry, timeZone) {
  if (typeof entry === "string") {
    return entry;
  }
  if (!entry || typeof entry !== "object") {
    return "";
  }
  if ("from_state" in entry || "to_state" in entry || entry.at) {
    const from = typeof entry.from_state === "string" && entry.from_state.trim()
      ? entry.from_state.trim()
      : "—";
    const to = typeof entry.to_state === "string" && entry.to_state.trim()
      ? entry.to_state.trim()
      : (typeof entry.state === "string" ? entry.state : "—");
    const at = localTime(entry.at, timeZone);
    return [at, `${from} → ${to}`].filter(Boolean).join(" ");
  }
  return [entry.state, entry.reason].filter(Boolean).join(" ");
}

async function apiResult(apiUrl, token, path, options, fetchImpl) {
  const response = await fetchImpl(`${apiUrl}${path}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  if (response.status === 401) {
    const error = new Error("Sign in required");
    error.status = 401;
    throw error;
  }
  if (!response.ok) {
    const error = new Error(dashboardError(response.status));
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function decodePayload(token) {
  if (typeof token !== "string" || token.split(".").length < 2) {
    return null;
  }
  try {
    const json = JSON.parse(atob(pad(token.split(".")[1])));
    return json && typeof json === "object" ? json : null;
  } catch {
    return null;
  }
}

function parseGroups(raw) {
  if (Array.isArray(raw)) {
    return raw.map((item) => String(item));
  }
  if (typeof raw !== "string" || !raw.trim()) {
    return [];
  }
  const text = raw.trim();
  if (text.startsWith("[")) {
    try {
      const parsed = JSON.parse(text);
      return Array.isArray(parsed) ? parsed.map((item) => String(item)) : [text];
    } catch {
      return [text];
    }
  }
  return [text];
}

function base64url(bytes) {
  let text = "";
  for (const byte of bytes) {
    text += String.fromCharCode(byte);
  }
  return btoa(text).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function pad(value) {
  const normalized = value.replaceAll("-", "+").replaceAll("_", "/");
  const extra = normalized.length % 4;
  if (extra === 0) {
    return normalized;
  }
  return normalized + "=".repeat(4 - extra);
}

function trimSlash(value) {
  return String(value || "").trim().replace(/\/$/, "");
}
