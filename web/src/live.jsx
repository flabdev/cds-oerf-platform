import { useEffect, useState } from "react";

import { App } from "./App.jsx";
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
  const [token, setToken] = useState(() => sessionStorage.getItem(ACCESS_KEY) || "");
  const [principal, setPrincipal] = useState(() => principalFromIdToken(tokenFromStorage()));
  const [incidents, setIncidents] = useState(null);
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
        setToken(session.accessToken);
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
    if (!principal.authenticated || !token) {
      return undefined;
    }
    let cancel = false;
    setIncidents(null);
    loadIncidents(config.apiUrl, token)
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
          setToken("");
          setPrincipal(principalFromIdToken(""));
        }
        setIncidents({ error: error.message });
      });
    return () => {
      cancel = true;
    };
  }, [config.apiUrl, principal.authenticated, token]);

  async function signIn() {
    const { verifier, challenge } = await createVerifier();
    sessionStorage.setItem(VERIFIER_KEY, verifier);
    window.location.assign(authorizeUrl({ ...config, redirectUri, challenge }));
  }

  async function submit(request) {
    await sendDashboard(config.apiUrl, token, request);
  }

  async function select(incidentId) {
    const detail = await loadIncident(config.apiUrl, token, incidentId);
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
