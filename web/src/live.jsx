import { useEffect, useRef, useState } from "react";

import { App } from "./App.jsx";
import {
  absorbDeskEvent,
  acceptDeskEvent,
  insertFetchedRow,
  mergeDeskRows,
  subscribeBody,
} from "./deskEvents.js";
import {
  authorizeUrl,
  createVerifier,
  exchangeCode,
  loadIncident,
  loadIncidents,
  principalFromIdToken,
  sendDashboard,
} from "./session.js";

const TOKEN_KEY = "oerf.idToken";
const ACCESS_KEY = "oerf.accessToken";
const VERIFIER_KEY = "oerf.codeVerifier";

export function LiveDesk({ config }) {
  const [accessToken, setAccessToken] = useState(() => sessionStorage.getItem(ACCESS_KEY) || "");
  const [idToken, setIdToken] = useState(() => sessionStorage.getItem(TOKEN_KEY) || "");
  const [principal, setPrincipal] = useState(() => principalFromIdToken(tokenFromStorage()));
  const [incidents, setIncidents] = useState(null);
  const [watchId, setWatchId] = useState("");
  const incidentsRef = useRef(incidents);
  incidentsRef.current = incidents;
  const redirectUri = window.location.origin;

  useEffect(() => {
    const code = new URLSearchParams(window.location.search).get("code");
    const verifier = sessionStorage.getItem(VERIFIER_KEY);
    if (!code || !verifier) {
      return undefined;
    }
    let cancel = false;
    exchangeCode({ ...config, redirectUri, code, verifier })
      .then((session) => {
        if (cancel) {
          return;
        }
        sessionStorage.setItem(TOKEN_KEY, session.idToken);
        sessionStorage.setItem(ACCESS_KEY, session.accessToken);
        sessionStorage.removeItem(VERIFIER_KEY);
        window.history.replaceState({}, "", window.location.pathname);
        setIdToken(session.idToken);
        setAccessToken(session.accessToken);
        setPrincipal(principalFromIdToken(session.idToken));
      })
      .catch((error) => {
        if (!cancel) {
          setIncidents({ error: error.message });
        }
      });
    return () => {
      cancel = true;
    };
  }, [config, redirectUri]);

  useEffect(() => {
    if (!principal.authenticated || !idToken) {
      return undefined;
    }
    let cancel = false;
    setIncidents(null);
    loadIncidents(config.apiUrl, idToken)
      .then((rows) => {
        if (!cancel) {
          setIncidents(rows);
        }
      })
      .catch((error) => {
        if (cancel) {
          return;
        }
        if (error.message === "Sign in required") {
          sessionStorage.removeItem(TOKEN_KEY);
          sessionStorage.removeItem(ACCESS_KEY);
          setIdToken("");
          setAccessToken("");
          setPrincipal(principalFromIdToken(""));
        }
        setIncidents({ error: error.message });
      });
    return () => {
      cancel = true;
    };
  }, [config.apiUrl, principal.authenticated, idToken]);

  useEffect(() => {
    if (!principal.authenticated || !accessToken || !config.wsUrl) {
      return undefined;
    }
    const scope = watchId ? "incident" : "tenant";
    const pending = new Set();
    let cancel = false;
    const socket = new WebSocket(`${config.wsUrl}?token=${encodeURIComponent(accessToken)}`);
    socket.addEventListener("open", () => {
      socket.send(JSON.stringify(subscribeBody(scope, watchId)));
    });
    socket.addEventListener("message", (message) => {
      const accepted = acceptDeskEvent(message.data, {
        tenantId: principal.tenantId,
        scope,
        incidentId: watchId,
      });
      if (!accepted) {
        return;
      }
      const snapshot = incidentsRef.current;
      if (!Array.isArray(snapshot)) {
        return;
      }
      const incidentId = accepted.incident.incident_id;
      if (snapshot.some((item) => item.incident_id === incidentId)) {
        setIncidents((current) => mergeDeskRows(current, accepted));
        return;
      }
      if (pending.has(incidentId)) {
        return;
      }
      pending.add(incidentId);
      absorbDeskEvent(snapshot, accepted, (id) => loadIncident(config.apiUrl, idToken, id))
        .then((next) => {
          if (cancel) {
            return;
          }
          const fetched = Array.isArray(next)
            ? next.find((item) => item.incident_id === incidentId)
            : null;
          setIncidents((current) => insertFetchedRow(current, fetched));
        })
        .catch(() => {})
        .finally(() => pending.delete(incidentId));
    });
    return () => {
      cancel = true;
      socket.close();
    };
  }, [
    accessToken,
    config.apiUrl,
    config.wsUrl,
    idToken,
    principal.authenticated,
    principal.tenantId,
    watchId,
  ]);

  async function signIn() {
    const { verifier, challenge } = await createVerifier();
    sessionStorage.setItem(VERIFIER_KEY, verifier);
    window.location.assign(authorizeUrl({ ...config, redirectUri, challenge }));
  }

  async function submit(request) {
    await sendDashboard(config.apiUrl, idToken, request);
  }

  async function select(incidentId) {
    setWatchId(incidentId);
    const detail = await loadIncident(config.apiUrl, idToken, incidentId);
    setIncidents((current) => {
      if (!Array.isArray(current)) {
        return [detail];
      }
      const rest = current.filter((item) => item.incident_id !== incidentId);
      return [detail, ...rest];
    });
  }

  if (!principal.authenticated) {
    return (
      <main className="gate">
        <section className="gate-card">
          <p className="kicker">OERF</p>
          <h1>Sign in</h1>
          <p>The desk loads incidents for the tenant on your Cognito session.</p>
          {incidents?.error ? <p className="note" role="alert">{incidents.error}</p> : null}
          <button type="button" className="send" onClick={signIn}>Sign in</button>
        </section>
      </main>
    );
  }

  return (
    <App
      principal={principal}
      incidents={incidents}
      onSubmit={submit}
      onSelect={select}
    />
  );
}

function tokenFromStorage() {
  return sessionStorage.getItem(TOKEN_KEY) || "";
}
