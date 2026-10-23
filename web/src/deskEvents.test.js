import assert from "node:assert/strict";
import test from "node:test";

import { acceptDeskEvent, mergeDeskRows, subscribeBody } from "./deskEvents.js";

const summary = {
  event_type: "INCIDENT_SUMMARY",
  tenant_id: "t1",
  incident_id: "INC-1",
  version: 4,
  scope: "tenant",
  data: { incident_id: "INC-1", state: "VERIFYING", stage: "verify", version: 4 },
};

const update = {
  event_type: "INCIDENT_UPDATE",
  tenant_id: "t1",
  incident_id: "INC-1",
  version: 4,
  scope: "incident",
  data: {
    incident_id: "INC-1",
    state: "VERIFYING",
    stage: "verify",
    version: 4,
    shipment_id: "PH-2841",
  },
};

test("subscribe bodies are the two frozen scopes", () => {
  assert.deepEqual(subscribeBody("tenant"), { scope: "tenant" });
  assert.deepEqual(subscribeBody("incident", "INC-1"), {
    scope: "incident",
    incident_id: "INC-1",
  });
});

test("a tenant socket accepts only that tenant's summary", () => {
  const binding = { tenantId: "t1", scope: "tenant", incidentId: "" };
  assert.equal(acceptDeskEvent(summary, binding).kind, "summary");
  assert.equal(acceptDeskEvent({ ...summary, tenant_id: "t2" }, binding), null);
  assert.equal(acceptDeskEvent(update, binding), null);
  assert.equal(
    acceptDeskEvent({ ...summary, data: { ...summary.data, phone: "+910000000101" } }, binding),
    null,
  );
});

test("an incident socket accepts only that incident's update", () => {
  const binding = { tenantId: "t1", scope: "incident", incidentId: "INC-1" };
  assert.equal(acceptDeskEvent(update, binding).incident.shipment_id, "PH-2841");
  assert.equal(acceptDeskEvent({ ...update, incident_id: "INC-2" }, binding), null);
  assert.equal(acceptDeskEvent(summary, binding), null);
  assert.equal(
    acceptDeskEvent(
      { type: "INCIDENT_UPDATE", tenant_id: "t1", incident_id: "INC-1", evidence_analysis: {} },
      binding,
    ),
    null,
  );
});

test("an accepted summary updates the matching row and leaves the other tenant's row", () => {
  const rows = [
    { incident_id: "INC-1", state: "OPEN", shipment_id: "PH-2841" },
    { incident_id: "INC-2", state: "OPEN", shipment_id: "VX-3101" },
  ];
  const accepted = acceptDeskEvent(summary, { tenantId: "t1", scope: "tenant", incidentId: "" });
  const next = mergeDeskRows(rows, accepted);
  assert.equal(next[0].state, "VERIFYING");
  assert.equal(next[0].shipment_id, "PH-2841");
  assert.equal(next[1].shipment_id, "VX-3101");
});
