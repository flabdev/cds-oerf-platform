import { useEffect, useMemo, useRef, useState } from "react";

import { ActivityFeed } from "./components/ActivityFeed.jsx";
import { StageStepper } from "./components/StageStepper.jsx";
import { StatePill } from "./components/StatePill.jsx";
import { TemperatureChart } from "./components/TemperatureChart.jsx";
import { buildDesk, heroNote } from "./deskView.js";
import {
  actionNotice,
  actionRequest,
  allowanceDraft,
  incidentListView,
  isTerminal,
  matchesSearch,
  needsWaitingEngagement,
  rankingNote,
  sessionView,
  textField,
} from "./model.js";
import { stageProgress } from "./states.js";
import { publishAllowance as publishTenantAllowance } from "./session.js";
import { formatMinutes } from "./utils/format.js";

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
  cancel: "Cancel requested. Waiting for the workflow.",
  confirm_transfer: "Transfer confirmation requested. Waiting for the workflow.",
  pick_option: "Option requested. Waiting for the workflow.",
};

const ICONS = {
  thermo: (
    <>
      <path d="M10 13.6V5a2 2 0 1 1 4 0v8.6a4 4 0 1 1-4 0Z" />
      <path d="M12 9.5v6" />
    </>
  ),
  timer: (
    <>
      <circle cx="12" cy="13.5" r="7" />
      <path d="M12 13.5V10M9.5 3.5h5" />
    </>
  ),
  info: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 11v5.5" />
      <circle className="fill" cx="12" cy="7.8" r="1.1" />
    </>
  ),
  out: <path d="M14 7.5V5.5a1.5 1.5 0 0 0-1.5-1.5h-6A1.5 1.5 0 0 0 5 5.5v13A1.5 1.5 0 0 0 6.5 20h6a1.5 1.5 0 0 0 1.5-1.5v-2M10 12h10M17 9l3 3-3 3" />,
  close: <path d="M7 7l10 10M17 7 7 17" />,
  left: <path d="M14.5 6 8.5 12l6 6" />,
  right: <path d="M9.5 6l6 6-6 6" />,
};

function Icon({ name }) {
  return (
    <svg className="icon" viewBox="0 0 24 24" aria-hidden="true">
      {ICONS[name]}
    </svg>
  );
}

function Meter({ allowance, size = "thin" }) {
  const unknown = allowance.ratio == null;
  return (
    <span
      className={`meter ${size} ${unknown ? "unknown" : allowance.level}`}
      style={unknown ? undefined : { "--share": allowance.ratio }}
      aria-hidden="true"
    >
      <i />
    </span>
  );
}

export function App({ principal, incidents = SAMPLE, onSubmit, onSelect, onSignOut, onRefresh, live }) {
  const session = sessionView(principal);
  const [selectedTenant, setSelectedTenant] = useState(session.tenantId || "t1");
  const [allowance, setAllowance] = useState(45);
  const [notice, setNotice] = useState(null);
  const [actionError, setActionError] = useState("");
  const [pendingAction, setPendingAction] = useState("");
  const [busy, setBusy] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [infoOpen, setInfoOpen] = useState(false);
  const [accountOpen, setAccountOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const accountRef = useRef(null);
  const searchRef = useRef(null);
  const listRef = useRef(null);
  const closeRef = useRef(null);
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState("open");
  const [section, setSection] = useState("incidents");
  const [selectedId, setSelectedId] = useState("INC-000001");
  const [choice, setChoice] = useState("");
  const [now, setNow] = useState(() => Date.now());
  const showAllowance = session.role === "tenant_admin";

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 30000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    function onKey(event) {
      const tag = event.target?.tagName;
      const typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
      if (event.key === "/" && !typing) {
        event.preventDefault();
        setSection("incidents");
        requestAnimationFrame(() => searchRef.current?.focus());
      }
      if (event.key === "Escape") {
        setInfoOpen(false);
        setConfirming(false);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (infoOpen) {
      closeRef.current?.focus();
    }
  }, [infoOpen]);

  useEffect(() => {
    if (!notice) {
      return undefined;
    }
    const timer = setTimeout(() => setNotice(null), 6000);
    return () => clearTimeout(timer);
  }, [notice]);

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
    setChoice("");
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
    ? list.incidents.filter((item) => matchesSearch(item, query))
    : [];
  const current = rows.find((item) => item.incident_id === selectedId) || rows[0] || null;
  const view = current ? buildDesk(current, now) : null;
  const progress = current ? stageProgress(current) : null;
  const openCount = Array.isArray(visible) ? visible.filter((item) => !isTerminal(item.state)).length : 0;
  const canPick = Boolean(view?.actions.pick) && !view?.terminalLabel && !pendingAction;
  const pickable = view ? view.options.filter((option) => option.feasible).map((option) => option.id) : [];
  const picked = pickable.includes(choice) ? choice : (view?.recommendationId || "");
  const pickedName = view?.options.find((option) => option.id === picked)?.name || picked;

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

  function say(text, tone = "ok") {
    setNotice({ text, tone, stamp: Date.now() });
  }

  function choose(item) {
    setSection("incidents");
    setSelectedId(item.incident_id);
    onSelect?.(item.incident_id);
  }

  function moveSelection(event) {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") {
      return;
    }
    event.preventDefault();
    const index = rows.findIndex((item) => item.incident_id === current?.incident_id);
    const step = event.key === "ArrowDown" ? 1 : -1;
    const next = rows[Math.min(Math.max(index + step, 0), rows.length - 1)];
    if (!next) {
      return;
    }
    choose(next);
    listRef.current?.querySelector(`[data-id="${next.incident_id}"]`)?.focus();
  }

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
      say(queuedNote(action, action === "pick_option" ? pickedName : ""));
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

  async function publishAllowance() {
    const draft = allowanceDraft(
      { product_profiles: [{ id: "PP-2-8-45", excursion_allowance_min: 45 }] },
      allowance,
    );
    const minutes = draft.product_profiles[0].excursion_allowance_min;
    if (!onSubmit) {
      say(`Sample allowance set to ${minutes} min. ${rankingNote(minutes)}`);
      return;
    }
    setBusy("allowance");
    try {
      // A PUT only saves a draft. Validation and publish follow before the note claims success.
      await publishTenantAllowance(onSubmit, selectedTenant, draft);
      say(`Allowance published at ${minutes} min. ${rankingNote(minutes)}`);
    } catch (error) {
      say(error instanceof Error ? error.message : "Allowance could not be published", "warn");
    } finally {
      setBusy("");
    }
  }

  const shipment = textField(view?.shipmentId, textField(current?.incident_id, "this incident"));
  const initial = session.displayName.slice(0, 1).toUpperCase();
  const kpiAllowance = view?.kpis.allowance;
  const showActions = view && !pendingAction && !view.terminalLabel;
  const noActionYet = showActions && !view.actions.pick && !view.actions.confirm;

  return (
    <div className={sidebarOpen ? "desk" : "desk rail-closed"}>
      <aside className="rail" id="sidebar">
        <div className="brand">
          <img className="brand-mark" src="/logo.png" alt={sidebarOpen ? "" : "OERF"} />
          {sidebarOpen ? (
            <span className="brand-name">
              OERF
              <small>Excursion desk</small>
            </span>
          ) : null}
        </div>

        <nav aria-label="Desk" className="nav">
          <button
            type="button"
            className={section === "incidents" ? "nav-item active" : "nav-item"}
            aria-current={section === "incidents" ? "page" : undefined}
            title={sidebarOpen ? undefined : "Incidents"}
            onClick={() => setSection("incidents")}
          >
            <Icon name="thermo" />
            {sidebarOpen ? <span>Incidents</span> : <span className="sr-only">Incidents</span>}
            {sidebarOpen ? <em className="count">{openCount}</em> : null}
          </button>
          {showAllowance ? (
            <button
              type="button"
              className={section === "allowance" ? "nav-item active" : "nav-item"}
              aria-current={section === "allowance" ? "page" : undefined}
              title={sidebarOpen ? undefined : "Allowance"}
              onClick={() => setSection("allowance")}
            >
              <Icon name="timer" />
              {sidebarOpen ? <span>Allowance</span> : <span className="sr-only">Allowance</span>}
            </button>
          ) : null}
        </nav>

        {sidebarOpen && session.canSwitchTenant ? (
          <label className="tenant-field">
            <span>Tenant</span>
            <select value={selectedTenant} onChange={(event) => setSelectedTenant(event.target.value)}>
              <option value="t1">t1</option>
              <option value="t2">t2</option>
            </select>
          </label>
        ) : null}

        <div className="rail-foot">
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
                  <span className="profile-copy">
                    <strong>{session.displayName}</strong>
                    <span>Tenant {selectedTenant}</span>
                  </span>
                ) : null}
              </button>
              {accountOpen ? (
                <div id="account-menu" className="account-menu" role="menu" aria-label="Account">
                  <dl>
                    <div>
                      <dt>Signed in as</dt>
                      <dd>{session.displayName}</dd>
                    </div>
                    <div>
                      <dt>Tenant</dt>
                      <dd>
                        {session.canSwitchTenant ? (
                          <select
                            aria-label="Tenant"
                            value={selectedTenant}
                            onChange={(event) => setSelectedTenant(event.target.value)}
                          >
                            <option value="t1">t1</option>
                            <option value="t2">t2</option>
                          </select>
                        ) : (session.tenantId || "None")}
                      </dd>
                    </div>
                    <div>
                      <dt>Role</dt>
                      <dd>{roleLabel(session.role)}</dd>
                    </div>
                  </dl>
                  <button type="button" role="menuitem" className="menu-item" onClick={signOut}>
                    <Icon name="out" />
                    Sign out
                  </button>
                </div>
              ) : null}
            </div>
          ) : (
            <div className="profile">
              <span className="avatar" aria-hidden="true">{initial}</span>
              {sidebarOpen ? (
                <span className="profile-copy">
                  <strong>{session.displayName}</strong>
                  <span>Tenant {selectedTenant}</span>
                </span>
              ) : null}
            </div>
          )}
          <div className="rail-tools">
            <button type="button" className="icon-btn" aria-label="About this desk" onClick={() => setInfoOpen(true)}>
              <Icon name="info" />
            </button>
            <button
              type="button"
              className="icon-btn"
              aria-expanded={sidebarOpen}
              aria-controls="sidebar"
              onClick={() => setSidebarOpen((open) => !open)}
            >
              <Icon name={sidebarOpen ? "left" : "right"} />
              <span className="sr-only">{sidebarOpen ? "Collapse sidebar" : "Expand sidebar"}</span>
            </button>
          </div>
        </div>
      </aside>

      <section className="queue" aria-label="Incidents">
        <header className="queue-head">
          <div className="queue-title">
            <h1>
              Incidents
              {list.state === "ready" ? <span className="head-count">{rows.length}</span> : null}
            </h1>
            {typeof live === "boolean" ? (
              <span className={live ? "live on" : "live"}>
                <span className="live-dot" aria-hidden="true" />
                {live ? "Live" : "Offline"}
              </span>
            ) : null}
          </div>
          <p>{scope === "all" ? `Every incident for tenant ${selectedTenant}` : `Open excursions for tenant ${selectedTenant}`}</p>
        </header>
        <div className="filters" role="group" aria-label="Incident filter">
          <button type="button" className={scope === "open" ? "seg on" : "seg"} aria-pressed={scope === "open"} onClick={() => setScope("open")}>Open</button>
          <button type="button" className={scope === "all" ? "seg on" : "seg"} aria-pressed={scope === "all"} onClick={() => setScope("all")}>All</button>
        </div>
        <label className="search">
          <span className="sr-only">Search incidents</span>
          <input
            ref={searchRef}
            type="search"
            value={query}
            placeholder="Search shipment or state"
            onChange={(event) => setQuery(event.target.value)}
          />
          <kbd aria-hidden="true">/</kbd>
        </label>

        {list.state === "loading" ? (
          <ul className="rows" aria-busy="true" aria-label="Loading incidents">
            {[0, 1, 2].map((key) => <li key={key} className="skeleton" />)}
          </ul>
        ) : null}
        {list.state === "error" ? (
          <div className="empty" role="alert">
            <strong>{list.message}</strong>
            <p>Sign out and sign in again. If it keeps happening, your account may be missing a tenant.</p>
          </div>
        ) : null}
        {list.state !== "loading" && list.state !== "error" && rows.length === 0 ? (
          <div className="empty">
            {query.trim() ? (
              <>
                <strong>Nothing matches “{query.trim()}”</strong>
                <p>Search by shipment, incident, or state, such as “in transit”.</p>
              </>
            ) : (
              <>
                <strong>{scope === "all" ? "No incidents yet" : "No open excursions"}</strong>
                <p>A new incident shows up here as soon as a reefer leaves its safe range.</p>
              </>
            )}
          </div>
        ) : null}

        {rows.length ? (
          <ul className="rows" ref={listRef} onKeyDown={moveSelection}>
            {rows.map((item) => {
              const row = buildDesk(item, now);
              const stage = stageProgress(item);
              const active = current?.incident_id === item.incident_id;
              const minutes = formatMinutes(row.kpis.allowance.remaining);
              return (
                <li key={item.incident_id}>
                  <button
                    type="button"
                    data-id={item.incident_id}
                    className={active ? "row selected" : "row"}
                    aria-current={active ? "true" : undefined}
                    onClick={() => choose(item)}
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
                    <span className="row-mid">
                      <StatePill state={item.state} />
                      {stage.activeLabel ? <span className="row-stage">{stage.activeLabel}</span> : null}
                      {minutes != null && !isTerminal(item.state) ? (
                        <span className={`row-min ${row.kpis.allowance.level}`}>{minutes} min left</span>
                      ) : null}
                    </span>
                    {!isTerminal(item.state) ? <Meter allowance={row.kpis.allowance} /> : null}
                  </button>
                </li>
              );
            })}
          </ul>
        ) : null}
      </section>

      <main className="detail">
        {section === "allowance" && showAllowance ? (
          <div className="allowance-page">
            <header className="detail-head">
              <div>
                <p className="incident-id">Tenant {selectedTenant}</p>
                <h2 className="shipment-id">Allowance</h2>
              </div>
            </header>
            <div className="detail-body">
              {selectedTenant === "t2" ? (
                <section className="side-panel">
                  <p>
                    Minutes a 2 to 8 °C shipment may spend outside its range. New incidents use the
                    published value. Open incidents keep the one they started with.
                  </p>
                  <div className="allowance-edit">
                    <label className="sr-only" htmlFor="allowance-range">Allowance in minutes</label>
                    <input
                      id="allowance-range"
                      type="range"
                      min="10"
                      max="90"
                      step="5"
                      value={allowance}
                      style={{ "--fill": (Number(allowance) - 10) / 80 }}
                      onChange={(event) => setAllowance(event.target.value)}
                    />
                    <label className="minutes">
                      <input
                        type="number"
                        min="1"
                        value={allowance}
                        aria-label="Allowance minutes"
                        onChange={(event) => setAllowance(event.target.value)}
                      />
                      <span>min</span>
                    </label>
                  </div>
                  <p className="preview">{rankingNote(allowance)}</p>
                  <button
                    type="button"
                    className="btn primary"
                    disabled={Boolean(busy)}
                    onClick={publishAllowance}
                  >
                    {busy === "allowance" ? "Publishing…" : "Publish allowance"}
                  </button>
                </section>
              ) : (
                <p className="quiet">No allowance draft is open for {selectedTenant}.</p>
              )}
            </div>
          </div>
        ) : null}

        {section !== "allowance" && list.state === "loading" && !current ? (
          <div className="detail-body" aria-busy="true" aria-label="Loading incident">
            <div className="skeleton wide" />
            <div className="skeleton" />
            <div className="skeleton tall" />
          </div>
        ) : null}

        {section !== "allowance" && current && view ? (
          <>
            <header className="detail-head">
              <div>
                <p className="incident-id">{textField(current.incident_id)}</p>
                <div className="title-line">
                  <h2 className="shipment-id">{textField(view.shipmentId, textField(current.incident_id, "?"))}</h2>
                  <StatePill state={current.state} />
                </div>
                <p className="head-meta">
                  {view.profileName ? <span>{view.profileName}</span> : null}
                  {view.rangeLabel ? <span>Safe range {view.rangeLabel}</span> : null}
                  {view.opened ? (
                    <span>
                      Opened <time dateTime={current.opened_at} title={view.opened.title}>{view.opened.label}</time>
                    </span>
                  ) : null}
                </p>
              </div>
            </header>

            <StageStepper progress={progress} />

            <div className="detail-body">
              <div className="hero-row">
                <section className={`hero ${kpiAllowance.level}`} aria-label="Allowance left">
                  <div className="readout">
                    <span className="figure">
                      {kpiAllowance.remaining == null ? "—" : formatMinutes(kpiAllowance.remaining)}
                    </span>
                    <span className="figure-label">
                      <strong>min of allowance left</strong>
                      {kpiAllowance.total != null ? (
                        <span>
                          out of {formatMinutes(kpiAllowance.total)} min
                          {kpiAllowance.estimated ? ", estimated from readings" : ""}
                        </span>
                      ) : null}
                    </span>
                  </div>
                  <div
                    role="meter"
                    aria-label="Allowance left"
                    aria-valuemin={0}
                    aria-valuemax={kpiAllowance.total ?? undefined}
                    aria-valuenow={kpiAllowance.remaining ?? undefined}
                    className="hero-meter"
                    key={current.incident_id}
                    style={kpiAllowance.total ? { "--ticks": Math.max(Math.round(kpiAllowance.total / 5), 1) } : undefined}
                  >
                    <Meter allowance={kpiAllowance} size="big" />
                  </div>
                  <p className="hero-note">{heroNote(view)}</p>
                </section>

                <dl className="kpis">
                  <div className={view.kpis.temperature.hot ? "kpi hot" : "kpi"}>
                    <dt>Current temp</dt>
                    <dd>
                      {view.kpis.temperature.value}
                      {view.kpis.temperature.hot ? <em>Outside the band</em> : null}
                    </dd>
                  </div>
                  <div className="kpi">
                    <dt>{view.kpis.above.title}</dt>
                    <dd>{view.kpis.above.value}</dd>
                  </div>
                  <div className="kpi">
                    <dt>Evidence</dt>
                    <dd>{view.kpis.evidence.value}</dd>
                  </div>
                </dl>
              </div>

              <div className="grid">
                <section className="block temps" aria-labelledby="temp-title">
                  <div className="block-head">
                    <h3 id="temp-title">Temperature</h3>
                    {view.rangeLabel ? <p>Safe range {view.rangeLabel}</p> : null}
                  </div>
                  <TemperatureChart plot={view.chart} />
                </section>

                <section className="block feed" aria-labelledby="feed-title">
                  <div className="block-head">
                    <h3 id="feed-title">Activity</h3>
                  </div>
                  <ActivityFeed items={view.feed} />
                </section>

                <section className="block options-block" aria-labelledby="options-title">
                  <div className="block-head">
                    <h3 id="options-title">Where it can go</h3>
                    <p>{canPick ? "Choose an option, then pick it below" : "Spare time against the allowance"}</p>
                  </div>
                  {view.options.length ? (
                    <div
                      className={canPick ? "options" : "options static"}
                      role={canPick ? "radiogroup" : undefined}
                      aria-labelledby="options-title"
                    >
                      {view.options.map((option) => (
                        <label key={option.id} className={optionClass(option, canPick && picked === option.id)}>
                          {canPick ? (
                            <input
                              type="radio"
                              name="facility"
                              value={option.id}
                              checked={picked === option.id}
                              disabled={!option.feasible}
                              onChange={() => setChoice(option.id)}
                            />
                          ) : null}
                          <span className="option-name">
                            {option.name}
                            {option.recommended ? <em className="badge rec">Recommended</em> : null}
                            {option.selected ? <em className="badge sel">Selected</em> : null}
                          </span>
                          <span className="option-figure">{option.etaLabel || option.projectedLabel}</span>
                          <span className="option-bar" aria-hidden="true">
                            {option.bar != null ? <i style={{ "--use": option.bar }} /> : null}
                          </span>
                          <span className="option-fit">
                            {option.feasible ? "Feasible" : "Infeasible"}
                            {option.spareLabel ? `, ${option.spareLabel}` : ""}
                            {option.reason ? `. ${option.reason}` : ""}
                          </span>
                        </label>
                      ))}
                    </div>
                  ) : (
                    <p className="quiet">Facility options appear once the incident is assessed.</p>
                  )}
                  {view.why.length ? (
                    <details className="why">
                      <summary>Why this recommendation?</summary>
                      <table>
                        <thead>
                          <tr>
                            <th scope="col">Option</th>
                            <th scope="col">Projected</th>
                            <th scope="col">Spare</th>
                          </tr>
                        </thead>
                        <tbody>
                          {view.why.map((row) => (
                            <tr key={row.id}>
                              <th scope="row">{row.name}</th>
                              <td>{[row.projected, row.eta].filter(Boolean).join(", ") || "—"}</td>
                              <td>{row.spare || "—"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                      {view.why[0].above || view.why[0].allowance ? (
                        <p>
                          {view.why[0].above ? `${view.why[0].above} above the limit so far. ` : ""}
                          {view.why[0].allowance ? `The allowance is ${view.why[0].allowance}.` : ""}
                        </p>
                      ) : null}
                    </details>
                  ) : null}
                </section>
              </div>
            </div>

            <footer className="action-bar" role="group" aria-label="Incident actions">
              {actionError ? <p className="form-error" role="alert">{actionError}</p> : null}
              {pendingAction ? <p className="pending" role="status">{PENDING[pendingAction]}</p> : null}
              {!pendingAction && view.terminalLabel ? <p className="locked">{view.terminalLabel}. Actions are off.</p> : null}
              {noActionYet ? <p className="locked">Pick and confirm appear when the workflow needs them.</p> : null}
              {showActions && view.actions.pick ? (
                <button
                  type="button"
                  className="btn primary"
                  disabled={!picked}
                  onClick={() => submit("pick_option", picked)}
                >
                  {picked ? `Pick ${pickedName}` : "Pick option"}
                </button>
              ) : null}
              {showActions && view.actions.confirm ? (
                <button type="button" className="btn primary" onClick={() => submit("confirm_transfer")}>
                  Confirm transfer
                </button>
              ) : null}
              {showActions && view.actions.cancel ? (
                <button type="button" className="btn danger" onClick={() => setConfirming(true)}>
                  Cancel incident
                </button>
              ) : null}
            </footer>
          </>
        ) : null}

        {section !== "allowance" && list.state !== "loading" && !current ? (
          <div className="detail-empty">
            <strong>No shipment open</strong>
            <p>Pick an incident on the left to see its stage, temperature, allowance, and options.</p>
          </div>
        ) : null}
      </main>

      <div className="toasts" role="status" aria-live="polite">
        {notice ? (
          <p className={`toast ${notice.tone}`} key={notice.stamp}>
            <span>{notice.text}</span>
            <button type="button" className="icon-btn" aria-label="Dismiss" onClick={() => setNotice(null)}>
              <Icon name="close" />
            </button>
          </p>
        ) : null}
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
              <button type="button" className="btn" autoFocus onClick={() => setConfirming(false)}>Keep open</button>
              <button type="button" className="btn danger solid" onClick={() => submit("cancel")}>Cancel incident</button>
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
              <h2 id="about-title">How this desk works</h2>
              <button ref={closeRef} type="button" className="icon-btn" aria-label="Close" onClick={() => setInfoOpen(false)}>
                <Icon name="close" />
              </button>
            </div>
            <p>
              OERF watches cold-chain shipments. When a reefer stays outside its safe range, it opens
              an incident and keeps every decision in one place.
            </p>
            <ul>
              <li>The roadmap shows where the incident is, from Detect to Close.</li>
              <li>The allowance bar shows how many minutes the product can still spend out of range.</li>
              <li>Pick option, confirm transfer, and cancel are queued. The page updates when the incident changes. Cancel asks you to confirm.</li>
              <li>A tenant admin can publish a new allowance for t2. Open incidents keep the one they started with.</li>
              <li>Press / to search and the arrow keys to move through incidents.</li>
            </ul>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function queuedNote(action, optionName) {
  if (action === "pick_option" && optionName) {
    return `Pick ${optionName} requested`;
  }
  if (action === "cancel") {
    return "Cancel incident requested";
  }
  if (action === "confirm_transfer") {
    return "Confirm transfer requested";
  }
  return "Action requested";
}

function roleLabel(role) {
  if (role === "platform_admin") return "Platform admin";
  if (role === "tenant_admin") return "Tenant admin";
  return "Ops user";
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

function optionClass(option, picked) {
  const names = ["option"];
  if (!option.feasible) names.push("blocked");
  if (option.recommended) names.push("recommended");
  if (picked) names.push("picked");
  return names.join(" ");
}
