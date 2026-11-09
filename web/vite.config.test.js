import assert from "node:assert/strict";
import test from "node:test";

import { apiProxy, resolveProxyApiUrl } from "./vite.config.js";

test("the dev proxy rewrites /api onto the RestApiUrl stage", () => {
  const proxy = apiProxy("https://api.example.com/prod/");
  assert.equal(proxy["/api"].target, "https://api.example.com");
  assert.equal(proxy["/api"].rewrite("/api/incidents"), "/prod/incidents");
  assert.equal(proxy["/api"].rewrite("/api/incidents/INC-1/actions"), "/prod/incidents/INC-1/actions");
});

test("a missing API URL leaves the proxy off", () => {
  assert.equal(apiProxy(""), undefined);
});

test("the dev proxy prefers public/config.json over VITE_API_URL", () => {
  const chosen = resolveProxyApiUrl(
    "https://env.example.com/prod",
    JSON.stringify({ apiUrl: "https://config.example.com/prod/" }),
  );
  assert.equal(chosen, "https://config.example.com/prod/");
  assert.equal(resolveProxyApiUrl("https://env.example.com/prod", ""), "https://env.example.com/prod");
  assert.equal(resolveProxyApiUrl("https://env.example.com/prod", "{"), "https://env.example.com/prod");
});
