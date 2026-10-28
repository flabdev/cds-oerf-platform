import { useEffect, useMemo, useRef, useState } from "react";

import {
  actionNotice,
  actionRequest,
  allowanceDraft,
  allowanceMeter,
  incidentListView,
  incidentView,
  needsWaitingEngagement,
  oneDecimal,
  rankingNote,
  readableLabel,
  sessionView,
  terminalNote,
  textField,
} from "./model.js";

const SAMPLE = [
  {
    incident_id: "INC-000001",
    state: "ASSESSING",
    stage: "ASSESSING",
    shipment_id: "PH-2841",
    allowance_remaining: 41.9,
    allowance_minutes: 45,
    time: "10:16",
    timeline: ["10:16 opened", "10:20 diagnosing", "10:24 assessing"],
    temperatures: [4.6, 5.4, 6.8, 8.1, 8.6, 8.4],
    options: [
      { id: "FACILITY_A", projected_minutes: 20.8, feasible: true },
      { id: "FACILITY_B", projected_minutes: 35.8, feasible: true },
    ],
    recommendation: "FACILITY_A",
    approval: "pending",
    range_c: [2, 8],
    evidence: { fault_code: "E17" },
    engagement_status: "WAITING",
    engagement_id: "ENG-01",
  },
  {
    incident_id: "INC-000002",
    state: "OPEN",
    stage: "OPEN",
    shipment_id: "VX-3101",
    allowance_remaining: 45,
    allowance_minutes: 45,
    time: "10:06",
    timeline: ["10:06 opened"],
    temperatures: [5.1, 6.2, 8.3],
    options: [],
    recommendation: null,
    range_c: [2, 8],
    evidence: {},
    engagement_status: "WAITING",
    engagement_id: "ENG-02",
  },
  {
    incident_id: "INC-000003",
    state: "DIAGNOSING",
    stage: "DIAGNOSING",
    shipment_id: "SBX-0001",
    allowance_remaining: 12.4,
    allowance_minutes: 45,
    time: "09:48",
    timeline: ["09:48 opened", "09:55 diagnosing"],
    temperatures: [6.9, 8.4, 9.1, 9.6, 10.2],
    options: [],
    recommendation: null,
    range_c: [2, 8],
    evidence: { fault_code: "E05" },
    engagement_status: "WAITING",
    engagement_id: "ENG-03",
  },
];

const SECTIONS = [
  { id: "incidents", label: "Incidents", icon: "thermo" },
  { id: "allowance", label: "Allowance", icon: "timer" },
  { id: "actions", label: "Actions", icon: "queue" },
];

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
  queue: <path d="M5 7h14M5 12h14M5 17h8" />,
  out: <path d="M14 7.5V5.5a1.5 1.5 0 0 0-1.5-1.5h-6A1.5 1.5 0 0 0 5 5.5v13A1.5 1.5 0 0 0 6.5 20h6a1.5 1.5 0 0 0 1.5-1.5v-2M10 12h10M17 9l3 3-3 3" />,
  info: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 11v5.5" />
      <circle className="fill" cx="12" cy="7.8" r="1.1" />
    </>
  ),
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

function Meter({ meter, size = "thin" }) {
  const unknown = meter.share == null;
  const style = unknown ? undefined : { "--share": meter.share };
  return (
    <span className={`meter ${size} ${unknown ? "unknown" : meter.level}`} style={style} aria-hidden="true">
      <i />
    </span>
  );
}

function stateClass(state) {
  return `state state-${String(state || "unknown").toLowerCase()}`;
}

function timelineEntry(entry) {
  const match = /^(\d{1,2}:\d{2})\s+(.*)$/.exec(entry);
  return match ? { time: match[1], text: match[2] } : { time: "", text: entry };
}

function queuedNote(action, optionId) {
  if (action === "pick_option" && optionId) {
    return `Pick ${readableLabel(optionId)} queued`;
  }
  if (action === "cancel") {
    return "Cancel incident queued";
  }
  return actionNotice(action, { queued: true });
}

const HERO_NOTES = {
  steady: "Inside the allowance. Choose where the shipment goes before it runs out.",
  low: "Allowance is running low. Act on this shipment first.",
  spent: "Allowance is used up. The product is outside its excursion limit.",
  unknown: "Allowance time appears once readings arrive for this shipment.",
};

export function App({ principal, incidents = SAMPLE, onSubmit, onSelect, onSignOut }) {
  const session = sessionView(principal);
  const [selectedTenant, setSelectedTenant] = useState(session.tenantId || "t1");
  const [allowance, setAllowance] = useState(45);
  const [notice, setNotice] = useState(null);
  const [infoOpen, setInfoOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [query, setQuery] = useState("");
  const [section, setSection] = useState("incidents");
  const [selectedId, setSelectedId] = useState("INC-000001");
  const [choice, setChoice] = useState(null);
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState("");
  const searchRef = useRef(null);
  const listRef = useRef(null);
  const closeRef = useRef(null);

  const visible = useMemo(() => {
    if (!Array.isArray(incidents)) {
      return incidents;
    }
    return incidents.filter((item) => !item.tenant_id || item.tenant_id === selectedTenant);
  }, [incidents, selectedTenant]);
  const list = incidentListView(session.state === "signed-out" ? { error: "Sign in required" } : visible);
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
  const selected = Array.isArray(visible)
    ? visible.find((item) => item.incident_id === selectedId) || null
    : null;
  const current = selected || rows[0] || null;
  const view = incidentView(current);
  const meter = allowanceMeter(current);
  const locked = terminalNote(current?.state);
  const initial = session.displayName.slice(0, 1).toUpperCase();
  const recommended = view.recommendationLabel === "—" ? "" : view.recommendationLabel;
  const optionIds = view.options.map((option) => option.id);
  const picked = optionIds.includes(choice) ? choice : (recommended || optionIds[0] || "");
  const canEditAllowance = session.canAdmin && selectedTenant === "t2";

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
        setArmed(false);
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
    if (!armed) {
      return undefined;
    }
    const timer = setTimeout(() => setArmed(false), 5000);
    return () => clearTimeout(timer);
  }, [armed]);

  function say(text, tone = "ok") {
    setNotice({ text, tone, stamp: Date.now() });
  }

  function choose(item) {
    setSelectedId(item.incident_id);
    setChoice(null);
    setArmed(false);
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
    if (!current || terminalNote(current.state)) {
      return;
    }
    const engagement = typeof current.engagement_id === "string" ? current.engagement_id.trim() : "";
    if (needsWaitingEngagement(action) && !engagement) {
      say(actionNotice(action, { missingEngagement: true }), "warn");
      return;
    }
    const actionId = globalThis.crypto?.randomUUID?.() || `${action}-${Date.now()}`;
    const request = actionRequest(
      current.incident_id,
      action,
      optionId,
      actionId,
      engagement,
    );
    setBusy(action);
    try {
      await onSubmit?.(request);
      say(queuedNote(action, optionId));
    } catch (error) {
      say(error instanceof Error ? error.message : actionNotice(action), "warn");
    } finally {
      setBusy("");
      setArmed(false);
    }
  }

  function cancelIncident() {
    if (!armed) {
      setArmed(true);
      return;
    }
    submit("cancel");
  }

  function signOut() {
    if (onSignOut) {
      onSignOut();
      return;
    }
    const url = new URL(window.location.href);
    url.searchParams.set("signedOut", "1");
    window.location.assign(url);
  }

  function publishAllowance() {
    const draft = allowanceDraft(
      { product_profiles: [{ id: "PP-2-8-45", excursion_allowance_min: 45 }] },
      allowance,
    );
    const minutes = draft.product_profiles[0].excursion_allowance_min;
    say(`Allowance published at ${minutes} min. ${rankingNote(minutes)}`);
    onSubmit?.({
      method: "PUT",
      path: `/admin/tenants/${selectedTenant}/domains/cold_chain_excursion/config`,
      body: draft,
    });
  }

  const actionButtons = (
    <>
      <button
        type="button"
        className="btn primary"
        disabled={Boolean(locked) || !current || Boolean(busy) || !picked}
        title={picked ? undefined : "No facility options yet"}
        onClick={() => submit("pick_option", picked)}
      >
        {busy === "pick_option" ? "Queuing" : picked ? `Pick ${readableLabel(picked)}` : "Pick option"}
      </button>
      <button
        type="button"
        className="btn"
        disabled={Boolean(locked) || !current || Boolean(busy)}
        onClick={() => submit("confirm_transfer")}
      >
        {busy === "confirm_transfer" ? "Queuing" : "Confirm transfer"}
      </button>
      <button
        type="button"
        className={armed ? "btn danger armed" : "btn danger"}
        disabled={Boolean(locked) || !current || Boolean(busy)}
        onClick={cancelIncident}
      >
        {busy === "cancel" ? "Queuing" : armed ? "Yes, cancel incident" : "Cancel incident"}
      </button>
    </>
  );

  return (
    <div className={sidebarOpen ? "desk" : "desk rail-closed"}>
      <aside className="rail" id="sidebar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">O</span>
          {sidebarOpen ? (
            <span className="brand-name">
              OERF
              <small>Excursion desk</small>
            </span>
          ) : null}
        </div>

        <nav aria-label="Desk" className="nav">
          {SECTIONS.map((item) => (
            <button
              key={item.id}
              type="button"
              className={section === item.id ? "nav-item active" : "nav-item"}
              aria-current={section === item.id ? "page" : undefined}
              title={sidebarOpen ? undefined : item.label}
              onClick={() => setSection(item.id)}
            >
              <Icon name={item.icon} />
              {sidebarOpen ? <span>{item.label}</span> : <span className="sr-only">{item.label}</span>}
              {sidebarOpen && item.id === "incidents" && list.state === "ready" ? (
                <em className="count">{list.incidents.length}</em>
              ) : null}
            </button>
          ))}
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
          <div className="profile">
            <span className="avatar" aria-hidden="true">{initial}</span>
            {sidebarOpen ? (
              <span className="profile-copy">
                <strong>{session.displayName}</strong>
                <span>Tenant {selectedTenant}</span>
              </span>
            ) : null}
          </div>
          {session.state === "ready" ? (
            <button type="button" className="nav-item quiet" onClick={signOut} title={sidebarOpen ? undefined : "Sign out"}>
              <Icon name="out" />
              {sidebarOpen ? <span>Sign out</span> : <span className="sr-only">Sign out</span>}
            </button>
          ) : null}
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

      <section className="queue" aria-label={SECTIONS.find((item) => item.id === section)?.label}>
        {section === "incidents" ? (
          <>
            <header className="queue-head">
              <h1>
                Incidents
                {list.state === "ready" ? <span className="head-count">{rows.length}</span> : null}
              </h1>
              <p>Open excursions for tenant {selectedTenant}</p>
            </header>
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
                {[0, 1, 2].map((index) => (
                  <li key={index} className="skeleton" />
                ))}
              </ul>
            ) : null}
            {list.state === "error" ? (
              <div className="empty" role="alert">
                <strong>{list.message}</strong>
                <p>Sign out and sign in again. If it keeps happening, your account may be missing a tenant.</p>
              </div>
            ) : null}
            {list.state === "empty" ? (
              <div className="empty">
                <strong>No open excursions</strong>
                <p>A new incident shows up here as soon as a reefer leaves its safe range.</p>
              </div>
            ) : null}
            {list.state === "ready" && rows.length === 0 ? (
              <div className="empty">
                <strong>Nothing matches “{query.trim()}”</strong>
                <p>Try a shipment id such as {textField(list.incidents[0]?.shipment_id, list.incidents[0]?.incident_id)}.</p>
              </div>
            ) : null}

            {rows.length ? (
              <ul className="rows" ref={listRef} onKeyDown={moveSelection}>
                {rows.map((item) => {
                  const rowMeter = allowanceMeter(item);
                  const minutes = oneDecimal(item.allowance_remaining);
                  const active = current?.incident_id === item.incident_id;
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
                          {item.time ? <time>{item.time}</time> : null}
                        </span>
                        <span className="row-mid">
                          <span className={stateClass(item.state)}>{readableLabel(item.state) || "Unknown"}</span>
                          {minutes != null ? (
                            <span className={`row-min ${rowMeter.level}`}>{minutes} min left</span>
                          ) : null}
                        </span>
                        <Meter meter={rowMeter} />
                      </button>
                    </li>
                  );
                })}
              </ul>
            ) : null}
          </>
        ) : null}

        {section === "actions" ? (
          <>
            <header className="queue-head">
              <h1>Actions</h1>
              <p>{current ? `For ${textField(current.shipment_id, current.incident_id)}` : "No shipment selected"}</p>
            </header>
            <div className="side-panel">
              {current ? (
                <p>
                  Actions go into a queue. The incident updates once the workflow takes them,
                  so the state here may lag by a few seconds.
                </p>
              ) : (
                <p>Open Incidents and choose a shipment first.</p>
              )}
              <div className="stack">{actionButtons}</div>
              {locked ? <p className="locked">{locked}</p> : null}
            </div>
          </>
        ) : null}

        {section === "allowance" ? (
          <>
            <header className="queue-head">
              <h1>Allowance</h1>
              <p>Tenant {selectedTenant}</p>
            </header>
            <div className="side-panel">
              {canEditAllowance ? (
                <>
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
                        onChange={(event) => setAllowance(event.target.value)}
                        aria-label="Minutes"
                      />
                      <span>min</span>
                    </label>
                  </div>
                  <p className="preview">{rankingNote(allowance)}</p>
                  <button type="button" className="btn primary" onClick={publishAllowance}>Publish allowance</button>
                </>
              ) : (
                <p>Only a tenant admin on t2 can change the allowance.</p>
              )}
            </div>
          </>
        ) : null}
      </section>

      <main className="detail">
        {current ? (
          <>
            <header className="detail-head">
              <div>
                <p className="incident-id">{textField(current.incident_id)}</p>
                <h2 className="shipment-id">{textField(current.shipment_id, textField(current.incident_id))}</h2>
              </div>
              <div className="chips">
                <span className={stateClass(current.state)}>{readableLabel(current.state) || "Unknown"}</span>
                {view.engagementStatus ? (
                  <span className="chip">Reply {readableLabel(view.engagementStatus).toLowerCase()}</span>
                ) : null}
              </div>
            </header>

            <div className="detail-body">
              <section className={`hero ${meter.level}`} aria-label="Allowance left">
                <div className="readout">
                  <span className="figure">{meter.remaining == null ? "—" : oneDecimal(meter.remaining)}</span>
                  <span className="figure-label">
                    <strong>min of allowance left</strong>
                    {meter.total ? <span>out of {meter.total} min</span> : null}
                  </span>
                </div>
                <div
                  role="meter"
                  aria-label="Allowance left"
                  aria-valuemin={0}
                  aria-valuemax={meter.total || undefined}
                  aria-valuenow={meter.remaining ?? undefined}
                  className="hero-meter"
                  key={current.incident_id}
                  style={meter.total ? { "--ticks": Math.max(Math.round(meter.total / 5), 1) } : undefined}
                >
                  <Meter meter={meter} size="big" />
                </div>
                <p className="hero-note">{HERO_NOTES[meter.level]}</p>
              </section>

              <div className="grid">
                <section className="block temps" aria-labelledby="temp-title">
                  <div className="block-head">
                    <h3 id="temp-title">Temperature</h3>
                    <p>
                      {view.chart.band
                        ? `Safe range ${view.chart.band[0]} to ${view.chart.band[1]} ${view.chart.unit}`
                        : "No safe range on this profile"}
                    </p>
                  </div>
                  {view.chart.empty ? (
                    <p className="quiet">No readings yet for this shipment.</p>
                  ) : (
                    <>
                      <svg className="chart" viewBox="0 0 128 50" role="img" aria-label={`Temperature in ${view.chart.unit}`}>
                        {view.chart.band ? (
                          <rect className="band" x="20" y={view.chart.bandY} width="94" height={view.chart.bandHeight} />
                        ) : null}
                        {view.chart.ticks.map((tick) => (
                          <g key={`${tick.label}-${tick.y}`}>
                            <line className="grid-line" x1="20" x2="114" y1={tick.y} y2={tick.y} />
                            <text x="2" y={tick.y + 1.1}>{tick.label}</text>
                          </g>
                        ))}
                        {view.chart.polyline ? <polyline className="line" points={view.chart.polyline} /> : null}
                        {view.chart.points.map((point) => {
                          const band = view.chart.band;
                          const out = band && (point.temperature_c > band[1] || point.temperature_c < band[0]);
                          return (
                            <circle
                              key={`${point.ts}-${point.x}`}
                              className={out ? "dot out" : "dot"}
                              cx={point.x}
                              cy={point.y}
                              r={out ? 1.7 : 1.3}
                            >
                              <title>{`${point.temperature_c} ${view.chart.unit}`}</title>
                            </circle>
                          );
                        })}
                      </svg>
                      <p className="last-reading">
                        Last reading{" "}
                        <strong>
                          {view.chart.points[view.chart.points.length - 1].temperature_c} {view.chart.unit}
                        </strong>
                      </p>
                    </>
                  )}
                </section>

                <section className="block" aria-labelledby="options-title">
                  <div className="block-head">
                    <h3 id="options-title">Where it can go</h3>
                    <p>Travel time against the allowance left</p>
                  </div>
                  {view.options.length ? (
                    <div className="options" role="radiogroup" aria-labelledby="options-title">
                      {view.options.map((option) => {
                        const projected = Number(option.projected_minutes);
                        const spare = Number(option.spare_minutes);
                        const base = meter.remaining || meter.total || 0;
                        const use = Number.isFinite(projected)
                          ? projected
                          : (Number.isFinite(spare) && base ? base - spare : null);
                        const ratio = use != null && base ? Math.min(Math.max(use / base, 0), 1) : null;
                        return (
                          <label
                            key={option.id}
                            className={[
                              "option",
                              picked === option.id ? "picked" : "",
                              option.feasible ? "" : "blocked",
                            ].join(" ").trim()}
                          >
                            <input
                              type="radio"
                              name="facility"
                              value={option.id}
                              checked={picked === option.id}
                              disabled={Boolean(locked)}
                              onChange={() => setChoice(option.id)}
                            />
                            <span className="option-name">
                              {readableLabel(option.id)}
                              {option.id === recommended ? <em>Recommended</em> : null}
                            </span>
                            <span className="option-figure">
                              {option.projectedLabel != null
                                ? `${option.projectedLabel} min away`
                                : option.spareLabel != null
                                  ? `${option.spareLabel} min to spare`
                                  : ""}
                            </span>
                            <span className="option-bar" aria-hidden="true">
                              {ratio != null ? <i style={{ "--use": ratio }} /> : null}
                            </span>
                            <span className="option-fit">{option.feasible ? "Arrives in time" : "Arrives too late"}</span>
                          </label>
                        );
                      })}
                    </div>
                  ) : (
                    <p className="quiet">Facility options appear once the incident is assessed.</p>
                  )}
                </section>

                <section className="block" aria-labelledby="facts-title">
                  <div className="block-head">
                    <h3 id="facts-title">Evidence</h3>
                  </div>
                  <dl className="facts">
                    <div>
                      <dt>Fault code</dt>
                      <dd>{view.evidenceLabel === "none" ? "None reported" : view.evidenceLabel}</dd>
                    </div>
                    <div>
                      <dt>Recommendation</dt>
                      <dd>{recommended ? readableLabel(recommended) : "Not ready yet"}</dd>
                    </div>
                  </dl>
                </section>

                <section className="block" aria-labelledby="timeline-title">
                  <div className="block-head">
                    <h3 id="timeline-title">Timeline</h3>
                  </div>
                  {view.timeline.length ? (
                    <ol className="timeline">
                      {view.timeline.map((entry) => {
                        const line = timelineEntry(entry);
                        return (
                          <li key={entry}>
                            {line.time ? <time>{line.time}</time> : null}
                            <span>{line.text}</span>
                          </li>
                        );
                      })}
                    </ol>
                  ) : (
                    <p className="quiet">No steps recorded yet.</p>
                  )}
                </section>
              </div>
            </div>

            <footer className="action-bar">
              {locked ? <p className="locked">{locked}. Actions are off.</p> : actionButtons}
            </footer>
          </>
        ) : (
          <div className="detail-empty">
            <strong>No shipment open</strong>
            <p>Pick an incident on the left to see its temperature, allowance, and options.</p>
          </div>
        )}
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
              <li>The allowance bar shows how many minutes the product can still spend out of range.</li>
              <li>Pick a facility, confirm a transfer, or cancel. Each action is queued and the incident updates when the workflow takes it.</li>
              <li>A tenant admin can publish a new allowance. Open incidents keep the one they started with.</li>
              <li>Press / to search and the arrow keys to move through incidents.</li>
            </ul>
          </div>
        </div>
      ) : null}
    </div>
  );
}
