import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App.jsx";
import { ConfigGate } from "./ConfigGate.jsx";
import { loadRuntimeConfig } from "./config.js";
import { DeskBoundary } from "./DeskBoundary.jsx";
import { LiveDesk } from "./live.jsx";
import { liveConfig } from "./session.js";
import "./styles.css";

const root = createRoot(document.getElementById("root"));

loadRuntimeConfig().then((loaded) => {
  if (!loaded.ok) {
    root.render(
      <StrictMode>
        <ConfigGate message={loaded.error} />
      </StrictMode>,
    );
    return;
  }
  const config = liveConfig(loaded.settings);
  const params = new URLSearchParams(window.location.search);
  const principal = {
    authenticated: params.get("signedOut") !== "1",
    tenantId: params.get("tenant") || "t2",
    role: params.get("role") || "tenant_admin",
    name: params.get("name") || "",
  };
  root.render(
    <StrictMode>
      <DeskBoundary>
        {config.mode === "live" ? <LiveDesk config={config} /> : <App principal={principal} />}
      </DeskBoundary>
    </StrictMode>,
  );
});
