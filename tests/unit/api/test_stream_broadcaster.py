"""Broadcaster envelopes match the desk parser and stay on one tenant and incident."""

import json
import os
import subprocess
from pathlib import Path

from oerf.platform.api.stream_broadcaster import dashboard_envelopes, subscription_targets

WEB = Path(__file__).resolve().parents[3] / "web"
EMITTED = "2026-10-18T12:00:00Z"


def test_subscriptions_stay_on_the_bound_scope_and_incident() -> None:
    targets = subscription_targets(
        [
            {"connection_id": "tenant", "scope": "tenant"},
            {"connection_id": "match", "scope": "incident", "incident_id": "INC-1"},
            {"connection_id": "other", "scope": "incident", "incident_id": "INC-2"},
            {"connection_id": "all", "scope": "incident", "incident_id": "ALL"},
            {"connection_id": "star", "scope": "incident", "incident_id": "*"},
            {"connection_id": "wide", "scope": "tenant", "incident_id": "INC-1"},
        ],
        "INC-1",
    )

    assert targets == [("tenant", "tenant"), ("match", "incident")]


def test_the_desk_accepts_the_broadcaster_event_and_rejects_private_fields() -> None:
    envelopes = dashboard_envelopes(
        {
            "tenant_id": "t1",
            "incident_id": "INC-1",
            "state": "VERIFYING",
            "stage": "verify",
            "version": 4,
            "shipment_id": "PH-2841",
            "sk": "META",
            "evidence_analysis": {"phone": "+910000000101"},
        },
        emitted_at=EMITTED,
    )
    assert envelopes is not None
    summary, update = envelopes
    encoded = json.dumps({"summary": summary, "update": update})
    assert "evidence_analysis" not in encoded
    assert "+910000000101" not in encoded

    script = """
import { acceptDeskEvent } from "./src/deskEvents.js";
const payload = JSON.parse(process.env.OERF_EVENT);
const incident = { tenantId: "t1", scope: "incident", incidentId: "INC-1" };
const accepted = acceptDeskEvent(payload.update, incident);
const summary = acceptDeskEvent(payload.summary, {
  tenantId: "t1",
  scope: "tenant",
  incidentId: "",
});
const otherTenant = acceptDeskEvent(payload.update, { ...incident, tenantId: "t2" });
const otherIncident = acceptDeskEvent(payload.update, { ...incident, incidentId: "INC-2" });
const leaked = acceptDeskEvent(
  { ...payload.update, data: { ...payload.update.data, evidence_analysis: { fault: "E17" } } },
  incident,
);
if (!accepted || accepted.incident.state !== "VERIFYING") process.exit(2);
if (!summary || summary.kind !== "summary") process.exit(3);
if (otherTenant || otherIncident || leaked) process.exit(4);
"""
    env = os.environ.copy()
    env["OERF_EVENT"] = encoded
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=WEB,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
