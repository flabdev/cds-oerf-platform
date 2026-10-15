"""Temporary, deterministic Lambda response for the cold-chain workflow skeleton.

Replace these responses incrementally with context, rules, engagement, reporting,
and incident repository integrations as those components become available.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

STAGE_BY_STEP = {
    "OPEN": "OPEN",
    "GatherContext": "DIAGNOSING",
    "Diagnose": "DIAGNOSING",
    "Assess": "ASSESSING",
    "Approval": "AWAITING_APPROVAL",
    "SecureFacility": "SECURING_FACILITY",
    "Act": "IN_TRANSIT",
    "Verify": "VERIFYING",
    "HandleReopen": "REOPENED",
    "GenerateReport": "AWAITING_DISPOSITION",
    "QA": "AWAITING_DISPOSITION",
    "Close": "CLOSED",
    "RecordFailure": "ESCALATED",
    "Escalate": "ESCALATED",
}


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Return predictable state output without performing business side effects."""
    step_name = event["step_name"]
    workflow = deepcopy(event.get("workflow", {}))
    workflow["current_stage"] = STAGE_BY_STEP[step_name]
    workflow["last_completed_step"] = step_name

    if step_name == "OPEN":
        workflow["incident_state"] = "OPEN"
    elif step_name == "GatherContext":
        workflow["context"] = {"status": "stubbed"}
    elif step_name == "Diagnose":
        workflow["diagnosis"] = {"status": "stubbed"}
    elif step_name == "Assess":
        workflow["decision"] = {
            "has_feasible_options": True,
            "needs_approval": False,
        }
    elif step_name == "Approval":
        workflow["approval"] = {"status": "approved", "stubbed": True}
    elif step_name == "SecureFacility":
        workflow["facility"] = {"accepted": True, "stubbed": True}
    elif step_name == "Act":
        workflow["action"] = {"status": "completed", "stubbed": True}
    elif step_name == "Verify":
        workflow["containment"] = {"status": "contained", "stubbed": True}
    elif step_name == "HandleReopen":
        workflow["incident_state"] = "REOPENED"
    elif step_name == "GenerateReport":
        workflow["report"] = {"status": "generated", "stubbed": True}
    elif step_name == "QA":
        workflow["qa"] = {"disposition": "approved", "stubbed": True}
    elif step_name == "Close":
        workflow["incident_state"] = "CLOSED"
        workflow["closed"] = True
    elif step_name == "RecordFailure":
        workflow["failure_recorded"] = True
    elif step_name == "Escalate":
        workflow["incident_state"] = "ESCALATED"
        workflow["escalated"] = True

    return workflow
