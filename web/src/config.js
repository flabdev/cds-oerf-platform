/**
 * Desk settings. Production reads `/config.json`. Vite dev falls back to
 * `VITE_*` only when that file is missing.
 */

const REQUIRED = ["apiUrl", "cognitoDomain", "userPoolClientId", "wsUrl"];

export const CONFIG_MISSING =
  "The desk configuration file is missing (config.json).";
export const CONFIG_INVALID =
  "The desk configuration is invalid. config.json must include apiUrl, cognitoDomain, userPoolClientId, and wsUrl.";
export const CONFIG_UNAVAILABLE = "The desk could not load config.json.";

export function settingsFromEnv(env) {
  return {
    apiUrl: text(env?.VITE_API_URL),
    cognitoDomain: text(env?.VITE_COGNITO_DOMAIN),
    userPoolClientId: text(env?.VITE_USER_POOL_CLIENT_ID),
    wsUrl: text(env?.VITE_WS_URL),
    dev: true,
  };
}

export function validateDeskConfig(body, { dev = false } = {}) {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return { ok: false, error: CONFIG_INVALID };
  }
  const settings = {
    apiUrl: text(body.apiUrl),
    cognitoDomain: text(body.cognitoDomain),
    userPoolClientId: text(body.userPoolClientId),
    wsUrl: text(body.wsUrl),
    dev,
  };
  const region = text(body.region);
  const userPoolId = text(body.userPoolId);
  if (region) {
    settings.region = region;
  }
  if (userPoolId) {
    settings.userPoolId = userPoolId;
  }
  if (REQUIRED.some((key) => !settings[key])) {
    return { ok: false, error: CONFIG_INVALID };
  }
  return { ok: true, settings };
}

export async function loadDeskConfig({
  fetchImpl = fetch,
  dev = false,
  env = {},
} = {}) {
  let response;
  try {
    response = await fetchImpl("/config.json", { cache: "no-store" });
  } catch {
    return { ok: false, error: CONFIG_UNAVAILABLE };
  }
  if (response.status === 404) {
    if (dev) {
      return { ok: true, settings: settingsFromEnv(env) };
    }
    return { ok: false, error: CONFIG_MISSING };
  }
  if (!response.ok) {
    return { ok: false, error: CONFIG_UNAVAILABLE };
  }
  let body;
  try {
    body = await response.json();
  } catch {
    return { ok: false, error: CONFIG_INVALID };
  }
  return validateDeskConfig(body, { dev });
}

export function loadRuntimeConfig() {
  return loadDeskConfig({ dev: import.meta.env.DEV, env: runtimeEnv() });
}

function runtimeEnv() {
  // Vite drops this branch from the production bundle, so a build does not
  // inline VITE_* values even when they are set in the environment.
  if (import.meta.env.DEV) {
    return {
      VITE_API_URL: import.meta.env.VITE_API_URL,
      VITE_COGNITO_DOMAIN: import.meta.env.VITE_COGNITO_DOMAIN,
      VITE_USER_POOL_CLIENT_ID: import.meta.env.VITE_USER_POOL_CLIENT_ID,
      VITE_WS_URL: import.meta.env.VITE_WS_URL,
    };
  }
  return {};
}

function text(value) {
  return String(value ?? "").trim();
}
