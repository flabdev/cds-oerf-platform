import assert from "node:assert/strict";
import test from "node:test";

import {
  actionRequest,
  allowanceDraft,
  displayName,
  incidentListView,
  rankingNote,
  sessionView,
} from "./model.js";

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
});

test("an action is a post with an idempotency key and no task token", () => {
  const request = actionRequest("INC-1", "pick_option", "FACILITY_A", "act-1");
  assert.equal(request.method, "POST");
  assert.equal(request.path, "/incidents/INC-1/actions");
  assert.equal(request.idempotencyKey, "act-1");
  assert.deepEqual(request.body, { action: "pick_option", option_id: "FACILITY_A" });
  assert.equal("task_token" in request.body, false);
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
