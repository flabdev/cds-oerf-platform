import { useEffect, useMemo, useRef, useState } from "react";

import { ActivityFeed } from "./components/ActivityFeed.jsx";
import { StageStepper } from "./components/StageStepper.jsx";
import { StatePill } from "./components/StatePill.jsx";
import { TemperatureChart } from "./components/TemperatureChart.jsx";
import { buildDesk } from "./deskView.js";
import {
  actionNotice,
  actionRequest,
  allowanceDraft,
  incidentListView,
  isTerminal,
  needsWaitingEngagement,
  rankingNote,
  sessionView,
  textField,
} from "./model.js";
import { stageProgress } from "./states.js";

function minutesAgo(minutes) {
  return new Date(Date.now() - minutes * 60 * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");
}

const PROFILE = {
  product_profiles: [
    {
      id: "PP-2-8-45",
      name: "Biologic 2-8",
      range_c: [2, 8],
      excursion_allowance_min: 45,
      allowance_mode: "cumulative",
    },
  ],
};

const SAMPLE = [
  {
    incident_id: "INC-000001",
    state: "IN_TRANSIT",
    stage: "IN_TRANSIT",
    shipment_id: "PH-2841",
    product_profile_id: "PP-2-8-45",
    config_snapshot: PROFILE,
    allowance_remaining: 33,
    opened_at: minutesAgo(18),
    range_c: [2, 8],
    readings: [
      { ts: minutesAgo(18), temperature_c: 5.4 },
      { ts: minutesAgo(12), temperature_c: 8.6 },
      { ts: minutesAgo(6), temperature_c: 9.1 },
      { ts: minutesAgo(2), temperature_c: 8.4 },
    ],
    timeline: [
      { at: minutesAgo(16), from_state: "OPEN", to_state: "DIAGNOSING" },
      { at: minutesAgo(14), from_state: "DIAGNOSING", to_state: "ASSESSING" },
      { at: minutesAgo(11), from_state: "ASSESSING", to_state: "SECURING_FACILITY" },
      { at: minutesAgo(8), from_state: "SECURING_FACILITY", to_state: "IN_TRANSIT" },
    ],
    options: [
      { id: "FAC-A", spare_minutes: 24, projected_minutes: 21, feasible: true },
      { id: "continue", spare_minutes: 41, projected_minutes: 4, feasible: true },
      { id: "FAC-B", spare_minutes: 9, projected_minutes: 36, feasible: false },
    ],
    recommendation: { recommended_option: "FAC-A" },
    evidence: { fault_codes: ["E17"] },
    engagement_status: "WAITING",
    engagement_id: "ENG-01",
  },
  {
    incident_id: "INC-000002",
    state: "ASSESSING",
    stage: "ASSESSING",
    shipment_id: "VX-3101",
    product_profile_id: "PP-2-8-45",
    config_snapshot: PROFILE,
    allowance_remaining: 41,
    opened_at: minutesAgo(9),
    range_c: [2, 8],
    readings: [
      { ts: minutesAgo(9), temperature_c: 6.2 },
      { ts: minutesAgo(4), temperature_c: 7.1 },
    ],
    timeline: [
      { at: minutesAgo(7), from_state: "OPEN", to_state: "DIAGNOSING" },
      { at: minutesAgo(5), from_state: "DIAGNOSING", to_state: "ASSESSING" },
    ],
    options: [
      { id: "FAC-A", spare_minutes: 28, projected_minutes: 17, feasible: true },
      { id: "continue", spare_minutes: 36, projected_minutes: 9, feasible: true },
    ],
    recommendation: { recommended_option: "FAC-A" },
    evidence: { fault_codes: [] },
    engagement_status: "WAITING",
    engagement_id: "ENG-02",
  },
  {
    incident_id: "INC-000003",
    state: "CANCELLED",
    stage: "CANCELLED",
    shipment_id: "SBX-0001",
    product_profile_id: "PP-2-8-45",
    config_snapshot: PROFILE,
    allowance_remaining: 12,
    opened_at: minutesAgo(40),
    range_c: [2, 8],
    readings: [
      { ts: minutesAgo(40), temperature_c: 4.2 },
      { ts: minutesAgo(30), temperature_c: 9.4 },
    ],
    timeline: [
      { at: minutesAgo(38), from_state: "OPEN", to_state: "DIAGNOSING" },
      { at: minutesAgo(34), from_state: "DIAGNOSING", to_state: "ASSESSING" },
      { at: minutesAgo(30), from_state: "ASSESSING", to_state: "SECURING_FACILITY" },
      { at: minutesAgo(26), from_state: "SECURING_FACILITY", to_state: "IN_TRANSIT" },
      { at: minutesAgo(20), from_state: "IN_TRANSIT", to_state: "CANCELLED" },
    ],
    options: [],
    recommendation: null,
    evidence: { fault_codes: [] },
    engagement_status: "",
    engagement_id: "",
  },
];

const PENDING = {
  cancel: "Cancel requested…",
  confirm_transfer: "Transfer confirmation requested…",
  pick_option: "Option requested…",
};

export function App({ principal, incidents = SAMPLE, onSubmit, onSelect, onSignOut, onRefresh, live }) {
  const session = sessionView(principal);
  const [selectedTenant, setSelectedTenant] = useState(session.tenantId || "t1");
  const [allowance, setAllowance] = useState(45);
  const [notice, setNotice] = useState("");
  const [actionError, setActionError] = useState("");
  const [pendingAction, setPendingAction] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [infoOpen, setInfoOpen] = useState(false);
  const [accountOpen, setAccountOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const accountRef = useRef(null);
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState("open");
  const [section, setSection] = useState("incidents");
  const [selectedId, setSelectedId] = useState("INC-000001");
  const [now, setNow] = useState(() => Date.now());
  const showAllowance = session.role === "tenant_admin";

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 30000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!confirming) {
      return undefined;
    }
    function onKey(event) {
      if (event.key === "Escape") {
        setConfirming(false);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [confirming]);

  useEffect(() => {
    if (!accountOpen) {
      return undefined;
    }
    function onPointer(event) {
      if (!accountRef.current?.contains(event.target)) {
        setAccountOpen(false);
      }
    }
    function onKey(event) {
      if (event.key === "Escape") {
        setAccountOpen(false);
      }
    }
    window.addEventListener("pointerdown", onPointer);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("pointerdown", onPointer);
      window.removeEventListener("keydown", onKey);
    };
  }, [accountOpen]);

  useEffect(() => {
    setPendingAction("");
    setActionError("");
    setConfirming(false);
  }, [selectedId]);

  const visible = useMemo(() => {
    if (!Array.isArray(incidents)) {
      return incidents;
    }
    return incidents.filter((item) => !item.tenant_id || item.tenant_id === selectedTenant);
  }, [incidents, selectedTenant]);
  const openList = incidentListView(session.state === "signed-out" ? { error: "Sign in required" } : visible);
  const list = scope === "all" ? allIncidents(visible, session) : openList;
  const rows = list.state === "ready"
    ? list.incidents.filter((item) => {
      const haystack = [
        textField(item.shipment_id, ""),
        textField(item.incident_id, ""),
        textField(item.state, ""),
      ].join(" ").toLowerCase();
      return haystack.includes(query.trim().toLowerCase());
    })
    : [];
  const current = rows.find((item) => item.incident_id === selectedId) || rows[0] || null;
  const view = current ? buildDesk(current, now) : null;
  const progress = current ? stageProgress(current) : null;
  const openCount = Array.isArray(visible) ? visible.filter((item) => !isTerminal(item.state)).length : 0;

  const currentId = current?.incident_id || "";
  const currentState = current?.state || "";
  useEffect(() => {
    if (!pendingAction || !currentId) {
      return undefined;
    }
    if (pendingAction === "cancel" && isTerminal(currentState)) {
      setPendingAction("");
      return undefined;
    }
    let cancel = false;
    const timer = setTimeout(() => {
      Promise.resolve()
        .then(() => onRefresh?.(currentId))
        .catch(() => {})
        .finally(() => {
          if (!cancel) {
            setPendingAction("");
          }
        });
    }, 3000);
    return () => {
      cancel = true;
      clearTimeout(timer);
    };
  }, [pendingAction, currentId, currentState, onRefresh]);

  async function submit(action, optionId) {
    if (!current || isTerminal(current.state) || pendingAction) {
      return;
    }
    const engagement = typeof current.engagement_id === "string" ? current.engagement_id.trim() : "";
    if (needsWaitingEngagement(action) && !engagement) {
      setActionError(actionNotice(action, { missingEngagement: true }));
      return;
    }
    const actionId = globalThis.crypto?.randomUUID?.() || `${action}-${Date.now()}`;
    const request = actionRequest(current.incident_id, action, optionId, actionId, engagement);
    setActionError("");
    setConfirming(false);
    setPendingAction(action);
    try {
      await onSubmit?.(request);
    } catch (error) {
      setPendingAction("");
      setActionError(error instanceof Error ? error.message : "Action failed");
      if (error && error.status === 409) {
        onRefresh?.(current.incident_id);
      }
    }
  }

  function signOut() {
    setAccountOpen(false);
    if (onSignOut) {
      onSignOut();
      return;
    }
    const url = new URL(window.location.href);
    url.searchParams.set("signedOut", "1");
    window.location.replace(url);
  }

  function publishAllowance() {
    const draft = allowanceDraft(
      { product_profiles: [{ id: "PP-2-8-45", excursion_allowance_min: 45 }] },
      allowance,
    );
    setNotice(rankingNote(draft.product_profiles[0].excursion_allowance_min));
    onSubmit?.({
      method: "PUT",
      path: `/admin/tenants/${selectedTenant}/domains/cold_chain_excursion/config`,
      body: draft,
    });
  }

  const shipment = textField(view?.shipmentId, textField(current?.incident_id, "this incident"));
  const initial = session.displayName.slice(0, 1).toUpperCase();
  const identity = [session.displayName, session.tenantId, session.role].filter(Boolean).join(" · ");

  return (
    <div className="shell">
      <div className={sidebarOpen ? "window" : "window collapsed"}>
        <aside className={sidebarOpen ? "rail" : "rail collapsed"} id="sidebar">
          {session.state === "ready" ? (
            <div className="account" ref={accountRef}>
              <button
                type="button"
                className="profile"
                aria-haspopup="menu"
                aria-expanded={accountOpen}
                aria-controls="account-menu"
                aria-label="Account"
                onClick={() => setAccountOpen((open) => !open)}
              >
                <span className="avatar" aria-hidden="true">{initial}</span>
                {sidebarOpen ? (
                  <div>
                    <strong>{session.displayName}</strong>
                    <span>{selectedTenant}</span>
                  </div>
                ) : null}
              </button>
              {accountOpen ? (
                <div id="account-menu" className="account-menu" role="menu" aria-label="Account">
                  <p>{identity}</p>
                  <button type="button" role="menuitem" onClick={signOut}>Sign out</button>
                </div>
              ) : null}
            </div>
          ) : (
            <div className="profile">
              <span className="avatar" aria-hidden="true">{initial}</span>
              {sidebarOpen ? (
                <div>
                  <strong>{session.displayName}</strong>
                  <span>{selectedTenant}</span>
                </div>
              ) : null}
            </div>
          )}

          <nav aria-label="Desk">
            <p className="nav-label">Desk</p>
            <button
              type="button"
              className={section === "incidents" ? "nav-item active" : "nav-item"}
              aria-current={section === "incidents" ? "page" : undefined}
              onClick={() => { setSection("incidents"); setNotice(""); }}
            >
              <span className="dot teal" aria-hidden="true" />
              {sidebarOpen ? <span>Incidents</span> : <span className="sr-only">Incidents</span>}
              {sidebarOpen ? <em>{openCount}</em> : null}
            </button>
            {showAllowance ? (
              <button
                type="button"
                className={section === "allowance" ? "nav-item active" : "nav-item"}
                aria-current={section === "allowance" ? "page" : undefined}
                onClick={() => { setSection("allowance"); setNotice(""); }}
              >
                <span className="dot amber" aria-hidden="true" />
                {sidebarOpen ? <span>Allowance</span> : <span className="sr-only">Allowance</span>}
              </button>
            ) : null}
          </nav>

          {sidebarOpen && session.canSwitchTenant ? (
            <label className="tenant-field">
              Tenant
              <select value={selectedTenant} onChange={(event) => setSelectedTenant(event.target.value)}>
                <option value="t1">t1</option>
                <option value="t2">t2</option>
              </select>
            </label>
          ) : null}

          <div className="rail-foot">
            <button type="button" className="ghost-icon" aria-label="About this site" onClick={() => setInfoOpen(true)}>
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <circle cx="12" cy="12" r="9" />
                <path d="M12 11v6" />
                <circle className="info-dot" cx="12" cy="7.5" r="1.1" />
              </svg>
            </button>
            <button
              type="button"
              className="ghost-icon"
              aria-expanded={sidebarOpen}
              aria-controls="sidebar"
              onClick={() => setSidebarOpen((open) => !open)}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d={sidebarOpen ? "M15 6 L9 12 L15 18" : "M9 6 L15 12 L9 18"} />
              </svg>
              <span className="sr-only">{sidebarOpen ? "Collapse sidebar" : "Expand sidebar"}</span>
            </button>
            {sidebarOpen ? <span className="wordmark">OERF</span> : null}
          </div>
        </aside>

        <section className="list-pane" aria-label="Incidents">
          <header className="pane-head">
            <div>
              <h1>Incidents</h1>
              <p>{scope === "all" ? `${rows.length} incidents` : `${rows.length} open`}</p>
            </div>
            {typeof live === "boolean" ? (
              <span className={live ? "live on" : "live"}>
                <span className="live-dot" aria-hidden="true" />
                {live ? "Live" : "Offline"}
              </span>
            ) : null}
          </header>
          <div className="filters" role="group" aria-label="Incident filter">
            <button type="button" className={scope === "open" ? "chip on" : "chip"} aria-pressed={scope === "open"} onClick={() => setScope("open")}>Open</button>
            <button type="button" className={scope === "all" ? "chip on" : "chip"} aria-pressed={scope === "all"} onClick={() => setScope("all")}>All</button>
          </div>
          <label className="search">
            <span className="sr-only">Search incidents</span>
            <input value={query} placeholder="Search" onChange={(event) => setQuery(event.target.value)} />
          </label>
          {list.state === "loading" ? <SkeletonRows /> : null}
          {list.state === "error" ? <p className="empty" role="alert">{list.message}</p> : null}
          {list.state !== "loading" && list.state !== "error" && rows.length === 0 ? (
            <p className="empty">{query.trim() ? "No matching incidents" : scope === "all" ? "No incidents" : "No open incidents"}</p>
          ) : null}
          <ul className="rows">
            {rows.map((item) => {
              const row = buildDesk(item, now);
              const stage = stageProgress(item);
              const active = current?.incident_id === item.incident_id;
              return (
                <li key={item.incident_id}>
                  <button
                    type="button"
                    className={active ? "row selected" : "row"}
                    aria-current={active ? "true" : undefined}
                    onClick={() => {
                      setSection("incidents");
                      setSelectedId(item.incident_id);
                      setNotice("");
                      onSelect?.(item.incident_id);
                    }}
                  >
                    <span className="row-top">
                      <strong>{textField(item.shipment_id, textField(item.incident_id, "?"))}</strong>
                      {row.aboveLimit ? (
                        <span className="hot-dot" title="Above the limit">
                          <span className="sr-only">Above the limit</span>
                        </span>
                      ) : null}
                      {row.elapsed ? <time>{row.elapsed}</time> : null}
                    </span>
                    <span className="row-meta">
                      <StatePill state={item.state} />
                      {stage.activeLabel ? <span>{stage.activeLabel}</span> : null}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </section>

        <main className="detail">
          {section === "allowance" && showAllowance ? (
            <div className="allowance-card" id="allowance">
              {selectedTenant === "t2" ? (
                <>
                  <h2>T2 allowance</h2>
                  <label>
                    Minutes
                    <input
                      type="number"
                      value={allowance}
                      aria-label="Allowance minutes"
                      onChange={(event) => setAllowance(event.target.value)}
                    />
                  </label>
                  <button type="button" className="send" onClick={publishAllowance}>Publish</button>
                  {notice ? <p className="note" role="status">{notice}</p> : null}
                </>
              ) : (
                <p>No allowance draft is open for {selectedTenant}.</p>
              )}
            </div>
          ) : null}
          {section !== "allowance" && list.state === "loading" && !current ? <DetailSkeleton /> : null}
          {section !== "allowance" && current && view ? (
            <>
              <header className="detail-head">
                <div className="who">
                  <div>
                    <div className="title-line">
                      <h2>{textField(view.shipmentId, textField(current.incident_id, "?"))}</h2>
                      <StatePill state={current.state} />
                    </div>
                    <p>
                      {[view.profileName, view.rangeLabel].filter(Boolean).join(" · ")}
                      {view.opened ? (
                        <>
                          {(view.profileName || view.rangeLabel) ? " · " : ""}
                          opened <time dateTime={current.opened_at} title={view.opened.title}>{view.opened.label}</time>
                        </>
                      ) : null}
                    </p>
                  </div>
                </div>
                <div className="action-stack" role="group" aria-label="Incident actions">
                  {actionError ? <p className="form-error" role="alert">{actionError}</p> : null}
                  {pendingAction ? <p className="pending" role="status">{PENDING[pendingAction]}</p> : null}
                  {!pendingAction && view.terminalLabel ? <p className="closed-note">{view.terminalLabel}</p> : null}
                  {!pendingAction && !view.terminalLabel && view.actions.pick ? (
                    <button
                      type="button"
                      aria-label="Pick option"
                      disabled={!view.recommendationId}
                      onClick={() => submit("pick_option", view.recommendationId)}
                    >
                      Pick option
                    </button>
                  ) : null}
                  {!pendingAction && !view.terminalLabel && view.actions.confirm ? (
                    <button type="button" aria-label="Confirm transfer" onClick={() => submit("confirm_transfer")}>
                      Confirm transfer
                    </button>
                  ) : null}
                  {!pendingAction && !view.terminalLabel && view.actions.cancel ? (
                    <button type="button" className="danger" aria-label="Cancel incident" onClick={() => setConfirming(true)}>
                      Cancel incident
                    </button>
                  ) : null}
                </div>
              </header>
              <StageStepper progress={progress} />
              <div className="detail-body">
                <div className="kpis">
                  <article className={view.kpis.temperature.hot ? "kpi hot" : "kpi"}>
                    <span>Current temp</span>
                    <strong>{view.kpis.temperature.value}</strong>
                    {view.kpis.temperature.hot ? <em>Outside the band</em> : null}
                  </article>
                  <article className="kpi">
                    <span>{view.kpis.above.title}</span>
                    <strong>{view.kpis.above.value}</strong>
                  </article>
                  <article className="kpi">
                    <span>Allowance remaining{view.kpis.allowance.detail ? ` ${view.kpis.allowance.detail}` : ""}</span>
                    <strong>{view.kpis.allowance.value}</strong>
                    {view.kpis.allowance.ratio != null ? (
                      <span
                        className="meter"
                        role="meter"
                        aria-label="Allowance remaining"
                        aria-valuemin={0}
                        aria-valuemax={100}
                        aria-valuenow={Math.round(view.kpis.allowance.ratio * 100)}
                      >
                        <span style={{ width: `${Math.round(view.kpis.allowance.ratio * 100)}%` }} />
                      </span>
                    ) : null}
                  </article>
                  <article className="kpi">
                    <span>Evidence</span>
                    <strong>{view.kpis.evidence.value}</strong>
                  </article>
                </div>
                <section className="panel chart-panel" aria-label="Temperature">
                  <h3>Temperature</h3>
                  <TemperatureChart plot={view.chart} />
                </section>
                <section className="panel feed-panel" aria-label="Activity">
                  <h3>Activity</h3>
                  <ActivityFeed items={view.feed} />
                </section>
                <section className="panel options-panel">
                  <h3>Options</h3>
                  {view.options.length ? (
                    <ul className="option-list">
                      {view.options.map((option) => (
                        <li key={option.id} className={optionClass(option)}>
                          <div className="option-top">
                            <strong>{option.name}</strong>
                            <span className="badges">
                              {option.recommended ? <span className="badge rec">Recommended</span> : null}
                              {option.selected ? <span className="badge sel">Selected</span> : null}
                              <span className={option.feasible ? "badge ok" : "badge no"}>
                                {option.feasible ? "Feasible" : "Infeasible"}
                              </span>
                            </span>
                          </div>
                          <p>
                            {[option.etaLabel, option.spareLabel].filter(Boolean).join(" · ") || "No spare time reported"}
                          </p>
                          {option.reason ? <p className="reason">{option.reason}</p> : null}
                          {option.bar != null ? (
                            <span className="spare" aria-hidden="true">
                              <span style={{ width: `${Math.round(option.bar * 100)}%` }} />
                            </span>
                          ) : null}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="chart-empty">No options yet</p>
                  )}
                  {view.why.length ? (
                    <details className="why">
                      <summary>Why this recommendation?</summary>
                      <ul>
                        {view.why.map((row) => (
                          <li key={row.id}>
                            <strong>{row.name}</strong>
                            {[
                              row.above ? `${row.above} above the limit` : "",
                              row.projected,
                              row.eta,
                              row.allowance ? `allowance ${row.allowance}` : "",
                              row.spare,
                            ].filter(Boolean).join(" · ")}
                          </li>
                        ))}
                      </ul>
                    </details>
                  ) : null}
                </section>
              </div>
            </>
          ) : null}
          {section !== "allowance" && list.state !== "loading" && !current ? (
            <div className="thread">
              <p className="empty">Select an incident to see the shipment.</p>
            </div>
          ) : null}
        </main>
      </div>

      {confirming && current ? (
        <div className="popup-backdrop" onClick={() => setConfirming(false)}>
          <div
            className="popup"
            role="dialog"
            aria-modal="true"
            aria-labelledby="cancel-title"
            aria-describedby="cancel-copy"
            onClick={(event) => event.stopPropagation()}
          >
            <h2 id="cancel-title">Cancel {shipment}?</h2>
            <p id="cancel-copy">Engagements stop and the shipment lock is released.</p>
            <div className="popup-actions">
              <button type="button" autoFocus onClick={() => setConfirming(false)}>Keep open</button>
              <button type="button" className="danger" onClick={() => submit("cancel")}>Cancel incident</button>
            </div>
          </div>
        </div>
      ) : null}

      {infoOpen ? (
        <div className="popup-backdrop" onClick={() => setInfoOpen(false)}>
          <div
            className="popup"
            role="dialog"
            aria-modal="true"
            aria-labelledby="about-title"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="popup-head">
              <div>
                <p className="kicker">About this site</p>
                <h2 id="about-title">What OERF does</h2>
              </div>
              <button type="button" className="ghost-icon dark" aria-label="Close" onClick={() => setInfoOpen(false)}>
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M7 7 L17 17 M17 7 L7 17" />
                </svg>
              </button>
            </div>
            <p>
              The Operational Exception Resolution Framework watches a cold-chain shipment,
              opens an incident when the temperature stays out of range, and keeps the
              decision trail in one place.
            </p>
            <ul>
              <li>The shipment shows its state, stage, allowance, evidence, and the activity the desk has.</li>
              <li>Pick option, confirm transfer, and cancel are queued. The page updates when the incident changes.</li>
              <li>Cancel asks you to confirm. A closed or cancelled incident has no actions.</li>
              <li>A tenant admin can publish a new allowance for t2. An open incident keeps the allowance it started with.</li>
            </ul>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function allIncidents(incidents, session) {
  if (session.state === "signed-out") {
    return { state: "error", message: "Sign in required" };
  }
  if (incidents == null) {
    return { state: "loading" };
  }
  if (incidents.error) {
    return { state: "error", message: incidents.error };
  }
  if (!Array.isArray(incidents) || incidents.length === 0) {
    return { state: "empty" };
  }
  return { state: "ready", incidents };
}

function optionClass(option) {
  const names = ["option-card"];
  if (!option.feasible) names.push("muted");
  if (option.recommended) names.push("recommended");
  if (option.selected) names.push("selected");
  return names.join(" ");
}

function SkeletonRows() {
  return (
    <div className="skeleton" aria-busy="true" aria-label="Loading incidents">
      {[0, 1, 2, 3].map((key) => <div key={key} className="skeleton-row" />)}
    </div>
  );
}

function DetailSkeleton() {
  return (
    <div className="skeleton detail-skeleton" aria-busy="true" aria-label="Loading incident">
      <div className="skeleton-row wide" />
      <div className="skeleton-row" />
      <div className="skeleton-row tall" />
    </div>
  );
}
