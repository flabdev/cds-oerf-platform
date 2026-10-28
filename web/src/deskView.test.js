import assert from "node:assert/strict";
import test from "node:test";

import { buildDesk, temperaturePlot } from "./deskView.js";
import { stageProgress, stateMeta, visibleActions } from "./states.js";

const now = Date.parse("2026-10-28T14:12:00Z");

test("cancelled is grey, never green, and freezes at the last reached stage", () => {
  assert.equal(stateMeta("CANCELLED").tone, "grey");
  assert.notEqual(stateMeta("CANCELLED").tone, "green");
  assert.equal(stateMeta("CLOSED").tone, "green");
  assert.equal(stateMeta("ESCALATED").tone, "red");
  assert.equal(stateMeta("OPEN").tone, "neutral");
  assert.equal(stateMeta("IN_TRANSIT").tone, "amber");
  const item = {
    state: "CANCELLED",
    shipment_id: "PH-2841",
    opened_at: "2026-10-28T13:40:00Z",
    timeline: [
      { at: "2026-10-28T13:50:00Z", from_state: "SECURING_FACILITY", to_state: "IN_TRANSIT" },
      { at: "2026-10-28T14:00:00Z", from_state: "IN_TRANSIT", to_state: "CANCELLED" },
    ],
  };
  const progress = stageProgress(item);
  assert.equal(progress.banner, "Cancelled at Act");
  assert.equal(progress.stages.find((stage) => stage.id === "act").status, "cancelled");
  assert.equal(progress.stages.find((stage) => stage.id === "verify").status, "future");
  assert.equal(progress.stages.filter((stage) => stage.status === "done").length, 4);
  const desk = buildDesk(item, now);
  assert.equal(desk.actions.pick, false);
  assert.equal(desk.actions.confirm, false);
  assert.equal(desk.actions.cancel, false);
  assert.match(desk.terminalLabel, /^Cancelled at \d{2}:\d{2} .+/);
  assert.equal(desk.feed.some((entry) => /whatsapp/i.test(entry.text)), false);
});

test("actions follow the state, and escalation freezes on the timeline", () => {
  assert.deepEqual(visibleActions({ state: "ASSESSING", engagement_id: "ENG-1", engagement_status: "WAITING" }), {
    pick: true,
    confirm: false,
    cancel: true,
  });
  assert.equal(visibleActions({ state: "IN_TRANSIT", engagement_id: "ENG-1", engagement_status: "WAITING" }).confirm, true);
  assert.equal(visibleActions({ state: "IN_TRANSIT", engagement_id: "ENG-1", engagement_status: "ANSWERED" }).confirm, false);
  assert.equal(visibleActions({ state: "VERIFYING" }).confirm, false);
  assert.equal(visibleActions({ state: "CLOSED" }).cancel, false);
  const progress = stageProgress({
    state: "ESCALATED",
    timeline: [
      { at: "2026-10-28T13:00:00Z", from_state: "ASSESSING", to_state: "SECURING_FACILITY" },
      { at: "2026-10-28T13:05:00Z", from_state: "SECURING_FACILITY", to_state: "ESCALATED" },
    ],
  });
  assert.equal(progress.banner, "Escalated at Coordinate");
  assert.equal(progress.stages.find((stage) => stage.id === "coordinate").status, "escalated");
});

test("the chart draws the band, the upper limit, and red points above it", () => {
  assert.equal(temperaturePlot([], [2, 8]).empty, true);
  const plot = temperaturePlot([
    { ts: "2026-10-28T13:00:00Z", temperature_c: 7.4 },
    { ts: "2026-10-28T13:10:00Z", temperature_c: 9.1 },
  ], [2, 8], { openedAt: "2026-10-28T12:58:00Z" }, now);
  assert.equal(plot.yLabel, "°C");
  assert.equal(plot.yMin, 1);
  assert.equal(plot.yMax, 10.1);
  assert.deepEqual(plot.ticks.map((tick) => tick.label), ["2", "4", "6", "8", "10"]);
  assert.equal(plot.points[0].hot, false);
  assert.equal(plot.points[1].hot, true);
  assert.ok(plot.band);
  assert.ok(plot.threshold);
  assert.equal(plot.markers[0].label, "Opened");
  assert.equal(plot.line.split(" ").length, 2);
});

test("options keep the rules order and name continue without inventing a facility", () => {
  const desk = buildDesk({
    state: "ASSESSING",
    product_profile_id: "PP-2-8-45",
    config_snapshot: {
      product_profiles: [{ id: "PP-2-8-45", name: "Biologic 2-8", excursion_allowance_min: 45, range_c: [2, 8] }],
    },
    range_c: [2, 8],
    allowance_remaining: 33,
    recommendation: { recommended_option: "FAC-A" },
    options: [
      { id: "FAC-B", spare_minutes: 9, projected_minutes: 36, feasible: false },
      { id: "continue", spare_minutes: 41, projected_minutes: 4, feasible: true },
      { id: "FAC-A", spare_minutes: 24, projected_minutes: 21, feasible: true },
    ],
    readings: [{ ts: "2026-10-28T14:00:00Z", temperature_c: 8.4 }],
  }, now);
  assert.deepEqual(desk.options.map((option) => option.id), ["continue", "FAC-A", "FAC-B"]);
  assert.equal(desk.options[0].name, "Continue to destination");
  assert.equal(desk.options[2].name, "FAC-B");
  assert.equal(desk.options[2].feasible, false);
  assert.equal(desk.options.find((option) => option.id === "FAC-A").recommended, true);
  assert.equal(desk.profileName, "Biologic 2-8");
  assert.equal(desk.kpis.temperature.value, "8.4 °C");
  assert.equal(desk.kpis.temperature.hot, true);
  assert.equal(desk.kpis.evidence.value, "None");
  assert.equal(buildDesk({ evidence: { fault_codes: ["E17"] } }, now).kpis.evidence.value, "E17");
  assert.equal(buildDesk({
    evidence: { fault_codes: ["E17", "AL02"], labels: { E17: "compressor fault" } },
  }, now).kpis.evidence.value, "E17 — compressor fault, AL02");
  assert.equal(buildDesk({
    evidence: { photos: ["shot.jpg"], fault_codes: ["E17"] },
  }, now).kpis.evidence.value, "1 photo");
  assert.match(desk.kpis.allowance.value, /33 \/ 45 min left/);
  assert.equal(desk.kpis.allowance.estimated, false);
  assert.equal(desk.kpis.allowance.remaining, 33);
  assert.equal(desk.kpis.allowance.total, 45);
  assert.equal(desk.kpis.allowance.level, "steady");
  assert.equal(desk.feed.some((entry) => entry.text.startsWith("Rules recommended FAC-A")), true);
});

test("the allowance level warms at a third left and when it is used up", () => {
  const profile = {
    product_profile_id: "PP-2-8-45",
    config_snapshot: { product_profiles: [{ id: "PP-2-8-45", excursion_allowance_min: 45, range_c: [2, 8] }] },
  };
  assert.equal(buildDesk({ ...profile, allowance_remaining: 15 }, now).kpis.allowance.level, "low");
  assert.equal(buildDesk({ ...profile, allowance_remaining: 0 }, now).kpis.allowance.level, "spent");
  assert.equal(buildDesk({ ...profile, allowance_remaining: 0 }, now).kpis.allowance.ratio, 0);
  assert.equal(buildDesk({}, now).kpis.allowance.level, "unknown");
});
