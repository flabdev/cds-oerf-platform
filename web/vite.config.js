import fs from "node:fs";

import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export function resolveProxyApiUrl(envApiUrl, configText) {
  if (typeof configText === "string" && configText.trim()) {
    try {
      const parsed = JSON.parse(configText);
      const apiUrl = String(parsed?.apiUrl || "").trim();
      if (apiUrl) {
        return apiUrl;
      }
    } catch {
      // A broken local config.json still falls through to VITE_API_URL.
    }
  }
  return envApiUrl;
}

function missingConfigJson404() {
  return {
    name: "missing-config-json-404",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const path = String(req.url || "").split("?")[0];
        if (path !== "/config.json") {
          next();
          return;
        }
        try {
          fs.accessSync(new URL("./public/config.json", import.meta.url));
        } catch {
          res.statusCode = 404;
          res.end();
          return;
        }
        next();
      });
    },
  };
}

export function apiProxy(apiUrl) {
  const remote = String(apiUrl || "")
    .trim()
    .replace(/\/$/, "");
  if (!remote) {
    return undefined;
  }
  const url = new URL(remote);
  const prefix = url.pathname === "/" ? "" : url.pathname.replace(/\/$/, "");
  return {
    "/api": {
      target: url.origin,
      changeOrigin: true,
      rewrite: (path) => `${prefix}${path.slice("/api".length)}`,
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  let configText = "";
  try {
    configText = fs.readFileSync(new URL("./public/config.json", import.meta.url), "utf8");
  } catch {
    configText = "";
  }
  return {
    plugins: [react(), missingConfigJson404()],
    server: {
      proxy: apiProxy(resolveProxyApiUrl(env.VITE_API_URL, configText)),
    },
  };
});
