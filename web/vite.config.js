import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

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
  return {
    plugins: [react()],
    server: {
      proxy: apiProxy(env.VITE_API_URL),
    },
  };
});
