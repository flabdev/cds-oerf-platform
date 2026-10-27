import { useEffect, useState } from "react";

import { App } from "./App.jsx";
import { acceptDeskEvent, mergeDeskRows, subscribeBody } from "./deskEvents.js";
import {
  authorizeUrl,
  createVerifier,
  exchangeOnce,
  loadIncident,
  loadIncidents,
  logoutUrl,
  presentIncident,
  principalFromIdToken,
  sendDashboard,
  stripAuthCode,
} from "./session.js";

const TOKEN_KEY = "oerf.idToken";
const ACCESS_KEY = "oerf.accessToken";
const VERIFIER_KEY = "oerf.codeVerifier";

export function LiveDesk({ config }) {
  const [idToken, setIdToken] = useState(() => sessionStorage.getItem(TOKEN_KEY) || "");
  const [accessToken, setAccessToken] = useState(() => sessionStorage.getItem(ACCESS_KEY) || "");
  const [principal, setPrincipal] = useState(() => principalFromIdToken(sessionStorage.getItem(TOKEN_KEY) || ""));
  const [incidents, setIncidents] = useState(null);
  const [watchId, setWatchId] = useState("");
  const redirectUri = window.location.origin;

  useEffect(() => {
    const code = new URLSearchParams(window.location.search).get("code");
    const verifier = sessionStorage.getItem(VERIFIER_KEY);
    if (!code || !verifier) {
      return undefined;
    }
    let cancel = false;
    exchangeOnce({ ...config, redirectUri, code, verifier })
      .then((session) => {
        sessionStorage.setItem(TOKEN_KEY, session.idToken);
        sessionStorage.setItem(ACCESS_KEY, session.accessToken);
        sessionStorage.removeItem(VERIFIER_KEY);
        stripAuthCode(window.location, window.history.replaceState.bind(window.history));
        if (cancel) {
          return;
        }
        setIdToken(session.idToken);
        setAccessToken(session.accessToken);
        setPrincipal(principalFromIdToken(session.idToken));
      })
      .catch((error) => {
        sessionStorage.removeItem(VERIFIER_KEY);
        stripAuthCode(window.location, window.history.replaceState.bind(window.history));
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
      setIncidents((current) =>
        mergeDeskRows(current, {
          ...accepted,
          incident: presentIncident(accepted.incident),
        }),
      );
    });
    return () => {
      socket.close();
    };
  }, [config.wsUrl, principal.authenticated, principal.tenantId, accessToken, watchId]);

  async function signIn() {
    const { verifier, challenge } = await createVerifier();
    sessionStorage.setItem(VERIFIER_KEY, verifier);
    window.location.assign(authorizeUrl({ ...config, redirectUri, challenge }));
  }

  function signOut() {
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(ACCESS_KEY);
    sessionStorage.removeItem(VERIFIER_KEY);
    window.location.assign(logoutUrl({
      domain: config.domain,
      clientId: config.clientId,
      logoutUri: redirectUri,
    }));
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
      onSignOut={signOut}
    />
  );
}
