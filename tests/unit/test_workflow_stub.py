"""Unit tests for the deterministic cold-chain workflow stub."""

from domains.cold_chain_excursion.steps.workflow_stub import handler


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


def test_failure_handlers_escalate_without_side_effects() -> None:
    recorded = handler(_event("RecordFailure"), None)
    escalated = handler(_event("Escalate"), None)

    assert recorded["failure_recorded"] is True
    assert escalated["incident_state"] == "ESCALATED"
    assert escalated["escalated"] is True


def test_handler_does_not_mutate_the_incoming_workflow() -> None:
    event = _event("Act", nested={"kept": True})

    handler(event, None)

    assert event["workflow"] == {"tenant_id": "T1", "nested": {"kept": True}}
