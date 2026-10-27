import assert from "node:assert/strict";
import test from "node:test";

import { absorbDeskEvent, acceptDeskEvent, mergeDeskRows, subscribeBody } from "./deskEvents.js";
import { shipmentMark } from "./model.js";

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

test("a broadcaster envelope is accepted and a private field is not", () => {
  const binding = { tenantId: "t1", scope: "incident", incidentId: "INC-1" };
  const update = {
    event_type: "INCIDENT_UPDATE",
    tenant_id: "t1",
    incident_id: "INC-1",
    version: 4,
    emitted_at: "2026-10-18T12:00:00Z",
    scope: "incident",
    data: {
      incident_id: "INC-1",
      state: "VERIFYING",
      stage: "verify",
      version: 4,
      shipment_id: "PH-2841",
    },
  };
  assert.equal(acceptDeskEvent(update, binding).incident.state, "VERIFYING");
  assert.equal(
    acceptDeskEvent(
      { ...update, data: { ...update.data, evidence_analysis: { fault: "E17" } } },
      binding,
    ),
    null,
  );
  assert.equal(acceptDeskEvent(update, { ...binding, tenantId: "t2" }), null);
  assert.equal(acceptDeskEvent(update, { ...binding, incidentId: "INC-2" }), null);
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

test("an unknown incident summary fetches once and inserts the fetched row", async () => {
  const accepted = acceptDeskEvent(summary, { tenantId: "t1", scope: "tenant", incidentId: "" });
  let calls = 0;
  const next = await absorbDeskEvent([], accepted, async (incidentId) => {
    calls += 1;
    assert.equal(incidentId, "INC-1");
    return {
      incident_id: "INC-1",
      shipment_id: "PH-2841",
      state: "OPEN",
      stage: "open",
      version: 4,
    };
  });
  assert.equal(calls, 1);
  assert.equal(next.length, 1);
  assert.equal(next[0].shipment_id, "PH-2841");
  assert.equal(next[0].incident_id, "INC-1");
});

test("a summary missing shipment_id never throws", async () => {
  const rows = [{ incident_id: "INC-1", state: "OPEN", shipment_id: "PH-2841", version: 3 }];
  const accepted = acceptDeskEvent(summary, { tenantId: "t1", scope: "tenant", incidentId: "" });
  assert.equal(accepted.incident.shipment_id, undefined);
  const next = await absorbDeskEvent(rows, accepted, async () => {
    throw new Error("a known incident is not fetched");
  });
  assert.equal(next[0].state, "VERIFYING");
  assert.equal(next[0].shipment_id, "PH-2841");
  assert.equal(shipmentMark(accepted.incident.shipment_id), "?");
  assert.equal(shipmentMark(next[0].shipment_id), "P");
});

test("an older version does not overwrite a newer row", () => {
  const rows = [{
    incident_id: "INC-1",
    state: "RESOLVED",
    stage: "closed",
    version: 8,
    shipment_id: "PH-2841",
  }];
  const accepted = acceptDeskEvent(summary, { tenantId: "t1", scope: "tenant", incidentId: "" });
  const next = mergeDeskRows(rows, accepted);
  assert.equal(next, rows);
  assert.equal(next[0].state, "RESOLVED");
  assert.equal(next[0].version, 8);
});
