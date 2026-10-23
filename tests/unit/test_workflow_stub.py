"""Unit tests for the deterministic cold-chain workflow stub."""

from domains.cold_chain_excursion.steps.workflow_stub import handler
from oerf.platform.agent.envelope import parse_envelope
from oerf.platform.agent.handlers import assess
from tests.unit.agent.test_handlers import _dependencies


def _event(step_name: str, **workflow: object) -> dict[str, object]:
    return {"step_name": step_name, "workflow": {"tenant_id": "T1", **workflow}}


def test_assess_stub_returns_choice_input_and_preserves_workflow_context() -> None:
    result = handler(_event("Assess", incident_id="INC-001"), None)

    assert result["tenant_id"] == "T1"
    assert result["incident_id"] == "INC-001"
    assert result["decision"] == {
        "has_feasible_options": True,
        "needs_approval": False,
    }
    assert result["current_stage"] == "ASSESSING"
    assert result["last_completed_step"] == "Assess"


def test_happy_path_handlers_return_expected_choice_shapes() -> None:
    expected = {
        "SecureFacility": ("facility", "accepted", True),
        "Verify": ("containment", "status", "contained"),
        "QA": ("qa", "disposition", "approved"),
        "Approval": ("approval", "status", "approved"),
    }

    for step_name, (field, key, value) in expected.items():
        result = handler(_event(step_name), None)
        assert result[field][key] == value, step_name


def test_close_stub_marks_incident_closed() -> None:
    result = handler(_event("Close", incident_id="INC-001"), None)

    assert result["incident_id"] == "INC-001"
    assert result["incident_state"] == "CLOSED"
    assert result["closed"] is True


def test_cancel_and_disposition_transitions_are_canonical() -> None:
    opened = handler(_event("OPEN", incident_state="OPEN"), None)
    cancelled = handler(_event("Cancel", incident_state="OPEN"), None)
    contained = handler(_event("RecordContainment", incident_state="VERIFYING"), None)
    disposition = handler(_event("BeginDisposition", incident_state="CONTAINED"), None)

    assert opened["incident_state"] == "OPEN"
    assert cancelled["incident_state"] == "CANCELLED"
    assert cancelled["cancelled"] is True
    assert contained["incident_state"] == "CONTAINED"
    assert disposition["incident_state"] == "AWAITING_DISPOSITION"


def test_failure_handlers_escalate_without_side_effects() -> None:
    recorded = handler(_event("RecordFailure"), None)
    escalated = handler(_event("Escalate"), None)

    assert recorded["failure_recorded"] is True
    assert escalated["incident_state"] == "ESCALATED"
    assert escalated["escalated"] is True


def test_diagnose_stub_is_an_envelope_assess_accepts() -> None:
    dependencies = _dependencies("DIAGNOSING")
    incident = dependencies.incidents.get("t1", "INC-000001")
    gathered = {
        "step": "GatherContext",
        "tenant_id": "t1",
        "incident_id": incident.incident_id,
        "shipment_id": "PH-2841",
        "workflow": {
            "trigger": {"temperature_c": 9.2},
            "shipment": {"product_profile_id": "PP-2-8-45"},
            "context": {
                "trigger": {"temperature_c": 9.2},
                "range_c": [2, 8],
                "allowance_minutes": 45,
                "facilities": [{"id": "facility-1", "drive_min": 12, "capacity_available": True}],
            },
            "ship_time": "2026-10-20T10:00:00Z",
        },
        "context": {},
        "incident_state": "DIAGNOSING",
        "current_stage": "DIAGNOSING",
        "last_completed_step": "GatherContext",
    }
    diagnosed = handler({"step_name": "Diagnose", "workflow": gathered}, None)

    assert "diagnosis" not in diagnosed
    assert diagnosed["workflow"]["diagnosis"] == {"status": "stubbed"}
    envelope = parse_envelope({"step_name": "Assess", "workflow": diagnosed})
    assert envelope.step == "Assess"
    result = assess(envelope, dependencies)
    assert result.decision is not None
    assert "has_feasible_options" in result.decision


def test_handler_does_not_mutate_the_incoming_workflow() -> None:
    event = _event("Act", nested={"kept": True})

    handler(event, None)

    assert event["workflow"] == {"tenant_id": "T1", "nested": {"kept": True}}
