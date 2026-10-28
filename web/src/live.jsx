import { useCallback, useEffect, useRef, useState } from "react";

import { App } from "./App.jsx";
import {
  absorbDeskEvent,
  acceptDeskEvent,
  insertFetchedRow,
  mergeDeskRows,
  replaceIncidentRow,
  subscribeBody,
} from "./deskEvents.js";
import {
  ACCESS_TOKEN_KEY,
  ID_TOKEN_KEY,
  VERIFIER_KEY,
  authFailure,
  authorizeUrl,
  clearStoredSession,
  createVerifier,
  exchangeOnce,
  loadIncident,
  loadIncidents,
  principalFromIdToken,
  sendDashboard,
  shouldOpenSocket,
  signOutTarget,
  socketAuthFailed,
  stripAuthCode,
} from "./session.js";

export function LiveDesk({ config }) {
  const [idToken, setIdToken] = useState(() => tokenFromStorage());
  const [accessToken, setAccessToken] = useState(() => sessionStorage.getItem(ACCESS_TOKEN_KEY) || "");
  const [principal, setPrincipal] = useState(() => principalFromIdToken(tokenFromStorage()));
  const [incidents, setIncidents] = useState(null);
  const [watchId, setWatchId] = useState("");
  const [connected, setConnected] = useState(false);
  const [expired, setExpired] = useState(false);
  const incidentsRef = useRef(incidents);
  const signingOut = useRef(false);
  const socketRef = useRef(null);
  incidentsRef.current = incidents;
  const redirectUri = window.location.origin;

  const expireSession = useCallback(() => {
    if (signingOut.current) {
      return;
    }
    clearStoredSession();
    setIdToken("");
    setAccessToken("");
    setWatchId("");
    setConnected(false);
    setIncidents(null);
    setPrincipal(principalFromIdToken(""));
    setExpired(true);
  }, []);

  useEffect(() => {
    const code = new URLSearchParams(window.location.search).get("code");
    const verifier = sessionStorage.getItem(VERIFIER_KEY);
    if (!code || !verifier || signingOut.current) {
      return undefined;
    }
    let cancel = false;
    exchangeOnce({ ...config, redirectUri, code, verifier })
      .then((session) => {
        if (signingOut.current) {
          return;
        }
        sessionStorage.setItem(ID_TOKEN_KEY, session.idToken);
        sessionStorage.setItem(ACCESS_TOKEN_KEY, session.accessToken);
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
        if (!cancel && !signingOut.current) {
          setIncidents(rows);
        }
      })
      .catch((error) => {
        if (cancel) {
          return;
        }
        if (authFailure(error)) {
          expireSession();
          return;
        }
        setIncidents({ error: error.message });
      });
    return () => {
      cancel = true;
    };
  }, [config.apiUrl, expireSession, principal.authenticated, idToken]);

  useEffect(() => {
    function onPageShow(event) {
      if (!event.persisted || sessionStorage.getItem(ID_TOKEN_KEY)) {
        return;
      }
      signingOut.current = true;
      setIdToken("");
      setAccessToken("");
      setWatchId("");
      setConnected(false);
      setIncidents(null);
      setPrincipal(principalFromIdToken(""));
    }
    window.addEventListener("pageshow", onPageShow);
    return () => window.removeEventListener("pageshow", onPageShow);
  }, []);

  useEffect(() => {
    if (!shouldOpenSocket({
      signingOut: signingOut.current,
      authenticated: principal.authenticated,
      accessToken,
      wsUrl: config.wsUrl,
    })) {
      return undefined;
    }
    const scope = watchId ? "incident" : "tenant";
    const pending = new Set();
    let cancel = false;
    let opened = false;
    const socket = new WebSocket(`${config.wsUrl}?token=${encodeURIComponent(accessToken)}`);
    socketRef.current = socket;
    socket.addEventListener("open", () => {
      opened = true;
      if (!cancel && !signingOut.current) {
        setConnected(true);
      }
      if (signingOut.current) {
        socket.close(1000);
        return;
      }
      socket.send(JSON.stringify(subscribeBody(scope, watchId)));
    });
    socket.addEventListener("close", (event) => {
      if (socketRef.current === socket) {
        socketRef.current = null;
      }
      if (!cancel) {
        setConnected(false);
      }
      if (socketAuthFailed({
        opened,
        code: event.code,
        signingOut: signingOut.current || cancel,
      })) {
        expireSession();
      }
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
        if (accepted.kind === "update") {
          loadIncident(config.apiUrl, idToken, incidentId)
            .then((detail) => {
              if (!cancel && !signingOut.current) {
                setIncidents((current) => replaceIncidentRow(current, detail));
              }
            })
            .catch((error) => {
              if (!cancel && authFailure(error)) {
                expireSession();
              }
            });
        }
        return;
      }
      if (pending.has(incidentId)) {
        return;
      }
      pending.add(incidentId);
      absorbDeskEvent(snapshot, accepted, (id) => loadIncident(config.apiUrl, idToken, id))
        .then((next) => {
          if (cancel || signingOut.current) {
            return;
          }
          const fetched = Array.isArray(next)
            ? next.find((item) => item.incident_id === incidentId)
            : null;
          setIncidents((current) => insertFetchedRow(current, fetched));
        })
        .catch((error) => {
          if (!cancel && authFailure(error)) {
            expireSession();
          }
        })
        .finally(() => pending.delete(incidentId));
    });
    return () => {
      cancel = true;
      setConnected(false);
      if (socketRef.current === socket) {
        socketRef.current = null;
      }
      socket.close(1000);
    };
  }, [
    accessToken,
    config.apiUrl,
    config.wsUrl,
    expireSession,
    idToken,
    principal.authenticated,
    principal.tenantId,
    watchId,
  ]);

  async function signIn() {
    signingOut.current = false;
    setExpired(false);
    const { verifier, challenge } = await createVerifier();
    sessionStorage.setItem(VERIFIER_KEY, verifier);
    window.location.assign(authorizeUrl({ ...config, redirectUri, challenge }));
  }

  function signOut() {
    signingOut.current = true;
    const socket = socketRef.current;
    socketRef.current = null;
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
      socket.close(1000);
    }
    clearStoredSession();
    setIncidents(null);
    setWatchId("");
    setConnected(false);
    setIdToken("");
    setAccessToken("");
    setPrincipal(principalFromIdToken(""));
    const target = signOutTarget({
      domain: config.domain,
      clientId: config.clientId,
      logoutUri: redirectUri,
    });
    if (target) {
      window.location.replace(target);
      return;
    }
    const next = new URL(window.location.href);
    next.search = "";
    next.hash = "";
    window.location.replace(next.toString());
  }

  async function submit(request) {
    try {
      await sendDashboard(config.apiUrl, idToken, request);
    } catch (error) {
      if (authFailure(error)) {
        expireSession();
        return;
      }
      throw error;
    }
  }

  const refreshIncident = useCallback(async (incidentId) => {
    if (!incidentId || !idToken || signingOut.current) {
      return;
    }
    try {
      const detail = await loadIncident(config.apiUrl, idToken, incidentId);
      if (!signingOut.current) {
        setIncidents((current) => replaceIncidentRow(current, detail));
      }
    } catch (error) {
      if (authFailure(error)) {
        expireSession();
      }
    }
  }, [config.apiUrl, expireSession, idToken]);

  async function select(incidentId) {
    if (signingOut.current) {
      return;
    }
    setWatchId(incidentId);
    try {
      const detail = await loadIncident(config.apiUrl, idToken, incidentId);
      if (signingOut.current) {
        return;
      }
      setIncidents((current) => {
        if (!Array.isArray(current)) {
          return [detail];
        }
        const rest = current.filter((item) => item.incident_id !== incidentId);
        return [detail, ...rest];
      });
    } catch (error) {
      if (authFailure(error)) {
        expireSession();
      }
    }
  }

  if (!principal.authenticated) {
    return (
      <main className="gate">
        <section className="gate-card">
          <div className="brand">
            <img className="brand-mark" src="/logo.png" alt="" />
            <span className="brand-name">
              OERF
              <small>Excursion desk</small>
            </span>
          </div>
          {expired ? (
            <>
              <h1>Your session expired</h1>
              <p>Sign in again to keep working on your incidents.</p>
              <button type="button" className="send" onClick={signOut}>Sign in again</button>
            </>
          ) : (
            <>
              <h1>Sign in to the excursion desk</h1>
              <p>You will see the open temperature incidents for your tenant.</p>
              {incidents?.error ? <p className="note" role="alert">{incidents.error}</p> : null}
              <button type="button" className="send" onClick={signIn}>Sign in</button>
            </>
          )}
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
      onRefresh={refreshIncident}
      live={connected}
    />
  );
}

function tokenFromStorage() {
  return sessionStorage.getItem(ID_TOKEN_KEY) || "";
}
