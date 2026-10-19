import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App.jsx";
import "./styles.css";

const params = new URLSearchParams(window.location.search);
const principal = {
  authenticated: params.get("signedOut") !== "1",
  tenantId: params.get("tenant") || "t2",
  role: params.get("role") || "tenant_admin",
  name: params.get("name") || "",
};

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <App principal={principal} />
  </StrictMode>,
);
