import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App.jsx";
import { DeskBoundary } from "./DeskBoundary.jsx";
import { LiveDesk } from "./live.jsx";
import { liveConfig } from "./session.js";
import "./styles.css";

const config = liveConfig(import.meta.env);
const params = new URLSearchParams(window.location.search);
const principal = {
  authenticated: params.get("signedOut") !== "1",
  tenantId: params.get("tenant") || "t2",
  role: params.get("role") || "tenant_admin",
  name: params.get("name") || "",
};

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <DeskBoundary>
      {config.mode === "live" ? <LiveDesk config={config} /> : <App principal={principal} />}
    </DeskBoundary>
  </StrictMode>,
);
