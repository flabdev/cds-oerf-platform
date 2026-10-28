import assert from "node:assert/strict";
import test from "node:test";

import {
  actionNotice,
  actionRequest,
  allowanceDraft,
  displayName,
  incidentListView,
  incidentView,
  matchesSearch,
  needsWaitingEngagement,
  oneDecimal,
  rankingNote,
  sessionView,
  temperatureChart,
  terminalNote,
} from "./model.js";
import { presentIncident } from "./session.js";

test("search matches states written with spaces and every typed word", () => {
  const row = { shipment_id: "PH-2841", incident_id: "INC-000001", state: "IN_TRANSIT" };
  assert.equal(matchesSearch(row, "in transit"), true);
  assert.equal(matchesSearch(row, "IN TRANSIT"), true);
  assert.equal(matchesSearch(row, "in_transit"), true);
  assert.equal(matchesSearch(row, "transit ph-2841"), true);
  assert.equal(matchesSearch(row, "ph 2841"), true);
  assert.equal(matchesSearch(row, "transit cancelled"), false);
  assert.equal(matchesSearch(row, "   "), true);
});

test("signed-out and tenant-admin sessions", () => {
  assert.equal(displayName(null), "Guest");
  assert.equal(displayName({ authenticated: true, name: "  Shift lead  " }), "Shift lead");
  assert.equal(sessionView(null).state, "signed-out");
  assert.equal(sessionView(null).displayName, "Guest");
  const admin = sessionView({
    authenticated: true,
    tenantId: "t2",
    role: "tenant_admin",
  });
  assert.equal(admin.canAdmin, true);
  assert.equal(admin.canSwitchTenant, false);
  const platform = sessionView({
    authenticated: true,
    tenantId: "t1",
    role: "platform_admin",
  });
  assert.equal(platform.canSwitchTenant, true);
});

test("incident list covers loading, empty, error, and a shipment", () => {
  assert.equal(incidentListView(null).state, "loading");
  assert.equal(incidentListView([]).state, "empty");
  assert.equal(incidentListView({ error: "unavailable" }).state, "error");
  const ready = incidentListView([
    { incident_id: "INC-1", shipment_id: "PH-2841", state: "ASSESSING" },
  ]);
  assert.equal(ready.incidents[0].shipment_id, "PH-2841");
  const open = incidentListView([
    { incident_id: "INC-1", state: "IN_TRANSIT" },
    { incident_id: "INC-2", state: "CANCELLED" },
    { incident_id: "INC-3", state: "CLOSED" },
  ]);
  assert.deepEqual(open.incidents.map((item) => item.incident_id), ["INC-1"]);
  assert.equal(incidentListView([{ incident_id: "INC-2", state: "CANCELLED" }]).state, "empty");
  assert.equal(terminalNote("CANCELLED"), "This incident is cancelled");
  assert.equal(terminalNote("CLOSED"), "This incident is closed");
  assert.equal(terminalNote("IN_TRANSIT"), "");
});

test("an action is a post with the client ids and no task token", () => {
  const request = actionRequest("INC-1", "pick_option", "FACILITY_A", "act-1", "ENG-03");
  assert.equal(request.method, "POST");
  assert.equal(request.path, "/incidents/INC-1/actions");
  assert.equal(request.idempotencyKey, "act-1");
  assert.deepEqual(request.body, {
    action: "pick_option",
    action_id: "act-1",
    engagement_id: "ENG-03",
    option_id: "FACILITY_A",
  });
  assert.equal("task_token" in request.body, false);
});

test("a live detail maps the response and the chart uses the readings", () => {
  const presented = presentIncident({
    shipment_id: "PH-2841",
    state: "IN_TRANSIT",
    allowance_remaining: null,
    evidence: { fault_codes: ["AL02"] },
    recommendation: { recommended_option: "FACILITY_A" },
    options: [{ id: "FACILITY_A", spare_minutes: 12.44, feasible: true }],
    engagement_status: "",
    timeline: [{ at: "2026-10-20T10:16:00Z", from_state: "OPEN", to_state: "DIAGNOSING" }],
    readings: [
      { ts: "2026-10-20T10:00:00Z", temperature_c: 9.1 },
      { ts: "2026-10-20T10:10:00Z", temperature_c: 7.4 },
    ],
    range_c: [2, 8],
  });
  const view = incidentView(presented);
  assert.equal(view.allowanceLabel, "—");
  assert.equal(incidentView({ allowance_remaining: 41.94 }).allowanceLabel, "41.9 min");
  assert.equal(oneDecimal(45), "45.0");
  assert.equal(oneDecimal(null), null);
  assert.equal(view.evidenceLabel, "AL02");
  assert.equal(view.recommendationLabel, "FACILITY_A");
  assert.equal(
    incidentView({ state: "DIAGNOSING", options: [], recommendation: null }).recommendationLabel,
    "Waiting for the driver's answer",
  );
  assert.equal(incidentView({ state: "ASSESSING" }).recommendationLabel, "—");
  assert.equal(view.options[0].spareLabel, "12.4");
  assert.equal(view.engagementStatus, "");
  const local = presentIncident({
    opened_at: "2026-10-20T10:16:00Z",
    timeline: [{ at: "2026-10-20T10:16:00Z", from_state: "OPEN", to_state: "DIAGNOSING" }],
  }, "America/New_York");
  assert.equal(local.time, "06:16");
  assert.equal(local.timeline[0], "06:16 OPEN → DIAGNOSING");
  assert.equal(view.chart.unit, "°C");
  assert.deepEqual(view.chart.band, [2, 8]);
  assert.deepEqual(view.chart.points.map((point) => point.temperature_c), [9.1, 7.4]);
  assert.equal(view.chart.polyline, view.chart.points.map((point) => `${point.x},${point.y}`).join(" "));
  assert.ok(view.chart.points[1].x - view.chart.points[0].x >= 14);
  assert.ok(view.chart.ticks.some((tick) => tick.label === "2"));
  assert.ok(view.chart.ticks.some((tick) => tick.label === "8"));
  const close = temperatureChart([
    { ts: "2026-10-20T10:00:00Z", temperature_c: 9 },
    { ts: "2026-10-20T10:00:20Z", temperature_c: 9.2 },
  ]);
  assert.equal(close.points.length, 2);
  assert.ok(close.points[1].x - close.points[0].x >= 13.9);
  assert.ok(close.polyline.split(" ").length === 2);
  const empty = temperatureChart([]);
  assert.equal(empty.polyline, "");
  assert.equal(empty.empty, true);
  assert.equal(empty.band, null);
  const ambient = incidentView({
    allowance_remaining: 120,
    range_c: [15, 25],
    readings: [{ ts: "2026-10-20T10:00:00Z", temperature_c: 20 }],
  });
  assert.equal(ambient.allowanceLabel, "120.0 min");
  assert.deepEqual(ambient.chart.band, [15, 25]);
  assert.ok(ambient.chart.ticks.some((tick) => tick.label === "15"));
  assert.ok(ambient.chart.ticks.some((tick) => tick.label === "25"));
  const unknown = incidentView({ allowance_remaining: null });
  assert.equal(unknown.allowanceLabel, "—");
  assert.equal(unknown.chart.band, null);
});

test("cancel does not need a waiting engagement", () => {
  assert.equal(needsWaitingEngagement("cancel"), false);
  assert.equal(needsWaitingEngagement("confirm_transfer"), true);
  assert.equal(actionNotice("cancel", { queued: true }), "Cancel queued");
  assert.equal(
    actionNotice("confirm_transfer", { missingEngagement: true }),
    "Confirm transfer: no waiting engagement for this shipment",
  );
  const request = actionRequest("INC-1", "cancel", null, "act-1", "");
  assert.equal(request.body.action, "cancel");
  assert.equal("engagement_id" in request.body, false);
});

test("the T2 editor publishes allowance 30", () => {
  const published = {
    product_profiles: [{ id: "PP-2-8-45", excursion_allowance_min: 45 }],
  };
  const draft = allowanceDraft(published, 30);
  assert.equal(published.product_profiles[0].excursion_allowance_min, 45);
  assert.equal(draft.product_profiles[0].excursion_allowance_min, 30);
  assert.match(rankingNote(45), /inside/);
  assert.match(rankingNote(30), /outside/);
});
