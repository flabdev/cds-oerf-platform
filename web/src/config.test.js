import assert from "node:assert/strict";
import test from "node:test";

import {
  CONFIG_INVALID,
  CONFIG_MISSING,
  loadDeskConfig,
  settingsFromEnv,
} from "./config.js";

const valid = {
  apiUrl: "https://api.example.com/prod/",
  cognitoDomain: "oerf.auth.us-east-1.amazoncognito.com",
  userPoolClientId: "client-1",
  wsUrl: "wss://ws.example.com/prod/",
  region: "us-east-1",
  userPoolId: "us-east-1_example",
};

test("a valid config.json is returned and the fetch is not cached", async () => {
  let seen;
  const loaded = await loadDeskConfig({
    dev: false,
    fetchImpl: async (url, options) => {
      seen = { url, options };
      return { status: 200, ok: true, json: async () => valid };
    },
  });
  assert.equal(seen.url, "/config.json");
  assert.equal(seen.options.cache, "no-store");
  assert.equal(loaded.ok, true);
  assert.equal(loaded.settings.apiUrl, valid.apiUrl);
  assert.equal(loaded.settings.cognitoDomain, valid.cognitoDomain);
  assert.equal(loaded.settings.userPoolClientId, valid.userPoolClientId);
  assert.equal(loaded.settings.wsUrl, valid.wsUrl);
  assert.equal(loaded.settings.region, "us-east-1");
  assert.equal(loaded.settings.userPoolId, "us-east-1_example");
  assert.equal(loaded.settings.dev, false);
});

test("a config.json that omits a required field is rejected", async () => {
  const body = { ...valid };
  delete body.userPoolClientId;
  const loaded = await loadDeskConfig({
    dev: false,
    fetchImpl: async () => ({ status: 200, ok: true, json: async () => body }),
  });
  assert.equal(loaded.ok, false);
  assert.equal(loaded.error, CONFIG_INVALID);
  assert.equal(loaded.settings, undefined);
});

test("a missing config.json in production does not use VITE_* values", async () => {
  const loaded = await loadDeskConfig({
    dev: false,
    env: {
      VITE_API_URL: "https://api.example.com",
      VITE_COGNITO_DOMAIN: "oerf.auth.example.com",
      VITE_USER_POOL_CLIENT_ID: "client-1",
      VITE_WS_URL: "wss://ws.example.com/prod",
    },
    fetchImpl: async () => ({ status: 404, ok: false, json: async () => ({}) }),
  });
  assert.equal(loaded.ok, false);
  assert.equal(loaded.error, CONFIG_MISSING);
  assert.equal(loaded.settings, undefined);
});

test("Vite dev uses VITE_* when config.json is missing", async () => {
  const env = {
    VITE_API_URL: "https://api.example.com/",
    VITE_COGNITO_DOMAIN: "https://oerf.auth.example.com/",
    VITE_USER_POOL_CLIENT_ID: "client-1",
    VITE_WS_URL: "wss://ws.example.com/prod/",
  };
  const loaded = await loadDeskConfig({
    dev: true,
    env,
    fetchImpl: async () => ({ status: 404, ok: false, json: async () => ({}) }),
  });
  assert.deepEqual(loaded.settings, settingsFromEnv(env));
});
