import { useMemo, useState } from "react";

import { actionRequest, allowanceDraft, incidentListView, rankingNote, sessionView } from "./model.js";

const SAMPLE = [
  {
    incident_id: "INC-000001",
    state: "ASSESSING",
    stage: "ASSESSING",
    shipment_id: "PH-2841",
    allowance_remaining: 41.9,
    allowance_minutes: 45,
    time: "10:16",
    timeline: ["10:16 opened", "10:20 diagnosing"],
    temperatures: [5.4, 8.1, 8.6],
    options: [
      { id: "FACILITY_A", projected_minutes: 20.8, feasible: true },
      { id: "FACILITY_B", projected_minutes: 35.8, feasible: true },
    ],
    recommendation: "FACILITY_A",
    approval: "pending",
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
    time: "10:06",
    timeline: ["10:06 opened"],
    options: [],
    recommendation: null,
    evidence: {},
    engagement_status: "WAITING",
    engagement_id: "ENG-02",
  },
  {
    incident_id: "INC-000003",
    state: "DIAGNOSING",
    stage: "DIAGNOSING",
    shipment_id: "SBX-0001",
    allowance_remaining: 38.5,
    time: "09:48",
    timeline: ["09:48 opened"],
    options: [],
    recommendation: null,
    evidence: { fault_code: "E05" },
    engagement_status: "WAITING",
    engagement_id: "ENG-03",
  },
];

export function App({ principal, incidents = SAMPLE, onSubmit, onSelect }) {
  const session = sessionView(principal);
  const [selectedTenant, setSelectedTenant] = useState(session.tenantId || "t1");
  const [allowance, setAllowance] = useState(45);
  const [notice, setNotice] = useState("");
  const [infoOpen, setInfoOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [query, setQuery] = useState("");
  const [section, setSection] = useState("incidents");
  const [selectedId, setSelectedId] = useState("INC-000001");
  const visible = useMemo(() => {
    if (!Array.isArray(incidents)) {
      return incidents;
    }
    return incidents.filter((item) => !item.tenant_id || item.tenant_id === selectedTenant);
  }, [incidents, selectedTenant]);
  const list = incidentListView(session.state === "signed-out" ? { error: "Sign in required" } : visible);
  const rows = list.state === "ready"
    ? list.incidents.filter((item) => {
        const haystack = `${item.shipment_id} ${item.incident_id} ${item.state}`.toLowerCase();
        return haystack.includes(query.trim().toLowerCase());
      })
    : [];
  const current = rows.find((item) => item.incident_id === selectedId) || rows[0] || null;
  const initial = session.displayName.slice(0, 1).toUpperCase();

  async function submit(action, optionId) {
    if (!current) {
      return;
    }
    if ((action === "confirm_transfer" || action === "pick_option") && !current.engagement_id) {
      setNotice("This shipment has no waiting engagement");
      return;
    }
    const actionId = globalThis.crypto?.randomUUID?.() || `${action}-${Date.now()}`;
    const request = actionRequest(
      current.incident_id,
      action,
      optionId,
      actionId,
      current.engagement_id,
    );
    try {
      await onSubmit?.(request);
      setNotice(`Queued ${request.body.action.replaceAll("_", " ")}`);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "The action was not queued");
    }
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

  return (
    <div className="shell">
      <div className={sidebarOpen ? "window" : "window collapsed"}>
        <aside className={sidebarOpen ? "rail" : "rail collapsed"} id="sidebar">
          <div className="profile">
            <span className="avatar" aria-hidden="true">{initial}</span>
            {sidebarOpen ? (
              <div>
                <strong>{session.displayName}</strong>
                <span>{selectedTenant}</span>
              </div>
            ) : null}
          </div>

          <nav aria-label="Desk">
            <p className="nav-label">Desk</p>
            <button
              type="button"
              className={section === "incidents" ? "nav-item active" : "nav-item"}
              onClick={() => setSection("incidents")}
            >
              <span className="dot teal" aria-hidden="true" />
              {sidebarOpen ? <span>Incidents</span> : <span className="sr-only">Incidents</span>}
              {sidebarOpen && list.state === "ready" ? <em>{list.incidents.length}</em> : null}
            </button>
            <button
              type="button"
              className={section === "allowance" ? "nav-item active" : "nav-item"}
              onClick={() => setSection("allowance")}
            >
              <span className="dot amber" aria-hidden="true" />
              {sidebarOpen ? <span>Allowance</span> : <span className="sr-only">Allowance</span>}
            </button>
            <button
              type="button"
              className={section === "actions" ? "nav-item active" : "nav-item"}
              onClick={() => setSection("actions")}
            >
              <span className="dot rose" aria-hidden="true" />
              {sidebarOpen ? <span>Actions</span> : <span className="sr-only">Actions</span>}
            </button>
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
            <button
              type="button"
              className="ghost-icon"
              aria-label="About this site"
              onClick={() => setInfoOpen(true)}
            >
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

        <section className="list-pane">
          <header className="pane-head">
            <div>
              <h1>{section === "allowance" ? "Allowance" : section === "actions" ? "Actions" : "Incidents"}</h1>
              <p>
                {section === "allowance"
                  ? selectedTenant
                  : section === "actions"
                    ? (current?.shipment_id || "No shipment selected")
                    : `${rows.length} open`}
              </p>
            </div>
          </header>
          {section === "incidents" ? (
            <label className="search">
              <span className="sr-only">Search incidents</span>
              <input
                value={query}
                placeholder="Search"
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
          ) : null}
          {section === "incidents" && list.state === "loading" ? <p className="empty">Loading incidents</p> : null}
          {section === "incidents" && list.state === "error" ? <p className="empty" role="alert">{list.message}</p> : null}
          {section === "incidents" && (list.state === "empty" || rows.length === 0) ? (
            <p className="empty">No open incidents</p>
          ) : null}
          {section === "incidents" ? (
            <ul className="rows">
              {rows.map((item) => (
                <li key={item.incident_id}>
                  <button
                    type="button"
                    className={current?.incident_id === item.incident_id ? "row selected" : "row"}
                    onClick={() => {
                      setSelectedId(item.incident_id);
                      onSelect?.(item.incident_id);
                    }}
                  >
                    <span className="avatar small" aria-hidden="true">
                      {(item.shipment_id || item.incident_id).slice(0, 1)}
                    </span>
                    <span className="row-copy">
                      <strong>{item.shipment_id || item.incident_id}</strong>
                      <span>
                        {item.allowance_remaining == null
                          ? item.state
                          : `${item.state} · ${item.allowance_remaining} min left`}
                      </span>
                    </span>
                    <time>{item.time}</time>
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          {section === "actions" ? (
            <div className="allowance-card" id="actions">
              <h2>Queue an action</h2>
              {current ? (
                <p>These apply to {current.shipment_id}. They are queued and do not change the incident here.</p>
              ) : (
                <p>Open Incidents and choose a shipment first.</p>
              )}
              <button type="button" disabled={!current} onClick={() => submit("confirm_transfer")}>Confirm transfer</button>
              <button type="button" disabled={!current} onClick={() => submit("pick_option", current?.recommendation)}>Pick option</button>
              <button type="button" className="send" disabled={!current} onClick={() => submit("cancel")}>Cancel</button>
              {notice ? <p className="note">{notice}</p> : null}
            </div>
          ) : null}
          {section === "allowance" ? (
            <div className="allowance-card" id="allowance">
              {session.canAdmin && selectedTenant === "t2" ? (
                <>
                  <h2>T2 allowance</h2>
                  <label>
                    Minutes
                    <input
                      type="number"
                      value={allowance}
                      onChange={(event) => setAllowance(event.target.value)}
                    />
                  </label>
                  <button type="button" className="send" onClick={publishAllowance}>Publish</button>
                </>
              ) : (
                <p>Allowance editing is available to a tenant admin on t2.</p>
              )}
              {notice ? <p className="note">{notice}</p> : null}
            </div>
          ) : null}
        </section>

        <main className="detail">
          {current ? (
            <>
              <header className="detail-head">
                <div className="who">
                  <span className="avatar small" aria-hidden="true">{current.shipment_id.slice(0, 1)}</span>
                  <div>
                    <h2>{current.shipment_id}</h2>
                    <p><span className="online" />{current.state}</p>
                  </div>
                </div>
                <div className="detail-tools">
                  <span className="status-pill">{current.engagement_status}</span>
                </div>
              </header>
              <div className="thread" id="shipment">
                {notice ? <p className="bubble mine">{notice}</p> : null}
                <div className="stats">
                  <div>
                    <span>Allowance left</span>
                    <strong>{current.allowance_remaining} min</strong>
                  </div>
                  <div>
                    <span>Evidence</span>
                    <strong>{current.evidence?.fault_code || "none"}</strong>
                  </div>
                  <div>
                    <span>Recommendation</span>
                    <strong>{current.recommendation || "Pending"}</strong>
                  </div>
                </div>
                <svg className="chart" viewBox="0 0 120 40" role="img" aria-label="Temperature">
                  <polyline fill="none" stroke="currentColor" points="0,30 40,12 80,8 120,18" />
                </svg>
                <ul className="options">
                  {(current.options || []).map((option) => (
                    <li key={option.id}>
                      {option.id}
                      <span>
                        {option.projected_minutes != null ? `${option.projected_minutes} min · ` : ""}
                        {option.feasible ? "feasible" : "infeasible"}
                      </span>
                    </li>
                  ))}
                </ul>
                <p className="timeline">Timeline {(current.timeline || []).join(" · ")}</p>
              </div>
              <form className="composer" id="actions" onSubmit={(event) => event.preventDefault()}>
                <button type="button" onClick={() => submit("confirm_transfer")}>Confirm transfer</button>
                <button type="button" onClick={() => submit("pick_option", current.recommendation)}>Pick option</button>
                <button type="button" className="send" onClick={() => submit("cancel")}>Cancel</button>
              </form>
            </>
          ) : (
            <div className="thread">
              <p className="empty">Select an incident to see the shipment.</p>
            </div>
          )}
        </main>
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
              <li>The shipment card shows state, allowance time, evidence, and who is waiting to reply.</li>
              <li>Confirm transfer, pick option, and cancel are queued. They do not change the incident on this page.</li>
              <li>A tenant admin can publish a new allowance. An open incident keeps the allowance it started with.</li>
              <li>Guest means there is no signed-in session. A signed-in name is the person or role on the token.</li>
            </ul>
          </div>
        </div>
      ) : null}
    </div>
  );
}
