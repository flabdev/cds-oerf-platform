"""Temporary, deterministic Lambda response for the cold-chain workflow skeleton.

Replace these responses incrementally with context, rules, engagement, reporting,
and incident repository integrations as those components become available.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

from oerf.platform.observability.logging import get_logger, log

logger = get_logger(__name__)

STAGE_BY_STEP = {
    "OPEN": "OPEN",
    "GatherContext": "DIAGNOSING",
    "Diagnose": "ASSESSING",
    "Assess": "ASSESSING",
    "Approval": "AWAITING_APPROVAL",
    "SecureFacility": "SECURING_FACILITY",
    "Act": "IN_TRANSIT",
    "Verify": "VERIFYING",
    "RecordContainment": "CONTAINED",
    "BeginDisposition": "AWAITING_DISPOSITION",
    "HandleReopen": "REOPENED",
    "GenerateReport": "AWAITING_DISPOSITION",
    "QA": "AWAITING_DISPOSITION",
    "Close": "CLOSED",
    "Cancel": "CANCELLED",
    "NoResponse": "AWAITING_APPROVAL",
    "RecordFailure": "ESCALATED",
    "Escalate": "ESCALATED",
}


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Return predictable state output without performing business side effects."""
    step_name = event["step_name"]
    workflow = deepcopy(event.get("workflow", {}))
    if step_name not in STAGE_BY_STEP:
        log(logger, logging.INFO, f"unknown workflow step {step_name}", outcome="stubbed")
        return workflow
    workflow["current_stage"] = STAGE_BY_STEP[step_name]
    workflow["last_completed_step"] = step_name

    if step_name == "OPEN":
        workflow["incident_state"] = "OPEN"
    elif step_name == "GatherContext":
        workflow["incident_state"] = "DIAGNOSING"
        workflow["context"] = {"status": "stubbed"}
    elif step_name == "Diagnose":
        workflow["incident_state"] = "ASSESSING"
        # diagnosis is not a StepEnvelope field. Keep it inside workflow so Assess
        # can parse this state. The root stays free of unknown keys.
        nested = workflow.get("workflow")
        diagnosis = {"status": "stubbed"}
        if isinstance(nested, dict):
            nested["diagnosis"] = diagnosis
        else:
            workflow["workflow"] = {"diagnosis": diagnosis}
    elif step_name == "Assess":
        needs_approval = bool(workflow.get("decision", {}).get("needs_approval", False))
        workflow["decision"] = {
            "has_feasible_options": True,
            "needs_approval": needs_approval,
        }
        workflow["incident_state"] = "AWAITING_APPROVAL" if needs_approval else "SECURING_FACILITY"
    elif step_name == "Approval":
        workflow["approval"] = {"status": "approved", "stubbed": True}
    elif step_name == "SecureFacility":
        if workflow.get("incident_state") == "AWAITING_APPROVAL":
            workflow["incident_state"] = "SECURING_FACILITY"
        workflow["facility"] = {"accepted": True, "stubbed": True}
    elif step_name == "Act":
        workflow["incident_state"] = "IN_TRANSIT"
        workflow["action"] = {"status": "completed", "stubbed": True}
    elif step_name == "Verify":
        workflow["incident_state"] = "VERIFYING"
        workflow["containment"] = {"status": "contained", "stubbed": True}
    elif step_name == "RecordContainment":
        workflow["incident_state"] = "CONTAINED"
    elif step_name == "BeginDisposition":
        workflow["incident_state"] = "AWAITING_DISPOSITION"
    elif step_name == "HandleReopen":
        workflow["incident_state"] = "REOPENED"
        workflow["containment"] = {"status": "reopened", "stubbed": True}
    elif step_name == "GenerateReport":
        workflow["report"] = {"status": "generated", "stubbed": True}
    elif step_name == "QA":
        workflow["qa"] = {"disposition": "approved", "stubbed": True}
    elif step_name == "Close":
        workflow["incident_state"] = "CLOSED"
        workflow["closed"] = True
    elif step_name == "Cancel":
        workflow["incident_state"] = "CANCELLED"
        workflow["cancelled"] = True
    elif step_name == "NoResponse":
        workflow["no_response"] = True
    elif step_name == "RecordFailure":
        workflow["failure_recorded"] = True
    elif step_name == "Escalate":
        workflow["incident_state"] = "ESCALATED"
        workflow["escalated"] = True

    return workflow
