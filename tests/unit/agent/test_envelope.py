"""The envelope carries identity and accumulated state across every step."""

import pytest
from pydantic import ValidationError

from oerf.platform.agent.envelope import (
    ACT,
    ASSESS,
    CLOSE,
    DIAGNOSE,
    ESCALATE,
    OPEN,
    EnvelopeError,
    StepEnvelope,
    parse_envelope,
)


def _start_input() -> dict[str, object]:
    return {"tenant_id": "t1", "incident_id": "INC-1", "shipment_id": "PH-2841"}


def test_first_task_lifts_the_start_input_onto_the_envelope() -> None:
    """The definition passes the execution start input under ``workflow``."""
    envelope = parse_envelope({"step_name": "OPEN", "workflow": _start_input()})

    assert envelope.step == OPEN
    assert envelope.tenant_id == "t1"
    assert envelope.incident_id == "INC-1"
    assert envelope.shipment_id == "PH-2841"
    assert envelope.workflow == {}
    assert envelope.context == {}
    assert envelope.incident_state is None


def test_later_task_keeps_identity_and_lets_the_event_name_the_step() -> None:
    previous = StepEnvelope(
        step=OPEN,
        tenant_id="t1",
        incident_id="INC-1",
        shipment_id="PH-2841",
        workflow={"reason": "excursion"},
        context={"contacts": ["driver"]},
        incident_state="OPEN",
        current_stage="OPEN",
        last_completed_step=OPEN,
    )

    envelope = parse_envelope({"step": "Assess", "workflow": previous.to_event()})

    assert envelope.step == ASSESS
    assert envelope.tenant_id == "t1"
    assert envelope.incident_id == "INC-1"
    assert envelope.workflow == {"reason": "excursion"}
    assert envelope.context == {"contacts": ["driver"]}
    assert envelope.incident_state == "OPEN"
    assert envelope.last_completed_step == OPEN


def test_flat_event_is_accepted_for_direct_invocation() -> None:
    envelope = parse_envelope(
        {"step": "Assess", **_start_input(), "workflow": {"reason": "excursion"}}
    )

    assert envelope.step == ASSESS
    assert envelope.workflow == {"reason": "excursion"}
    assert envelope.shipment_id == "PH-2841"


def test_steps_without_a_handler_yet_still_validate() -> None:
    """The envelope accepts every step the definition declares, not only ours."""
    for step in (ACT, DIAGNOSE, ESCALATE):
        assert parse_envelope({"step": step, **_start_input()}).step == step


def test_decision_is_reported_at_the_root_for_the_choice_states() -> None:
    envelope = StepEnvelope(
        step=ASSESS,
        tenant_id="t1",
        incident_id="INC-1",
        shipment_id="PH-2841",
        decision={"has_feasible_options": True, "needs_approval": False},
    )

    assert envelope.to_event()["decision"] == {
        "has_feasible_options": True,
        "needs_approval": False,
    }


def test_to_event_drops_unset_optionals_but_keeps_workflow() -> None:
    envelope = StepEnvelope(
        step=CLOSE,
        tenant_id="t1",
        incident_id="INC-1",
        shipment_id="PH-2841",
        incident_state="CLOSED",
        current_stage="CLOSED",
        last_completed_step=CLOSE,
    )

    body = envelope.to_event()

    assert body["incident_state"] == "CLOSED"
    assert body["workflow"] == {}
    assert "decision" not in body
    assert "task_token" not in body


def test_task_token_round_trips_when_a_wait_state_supplies_one() -> None:
    envelope = parse_envelope({"step": "Assess", **_start_input(), "task_token": "tok-1"})

    assert envelope.task_token == "tok-1"
    assert envelope.to_event()["task_token"] == "tok-1"


def test_missing_step_is_rejected() -> None:
    with pytest.raises(EnvelopeError, match="no 'step' key"):
        parse_envelope({"workflow": _start_input()})


def test_unknown_step_is_rejected() -> None:
    with pytest.raises(EnvelopeError, match="unknown workflow step"):
        parse_envelope({"step": "Teleport", **_start_input()})


def test_missing_identifier_is_rejected() -> None:
    with pytest.raises(EnvelopeError, match="tenant_id"):
        parse_envelope(
            {"step": "OPEN", "workflow": {"incident_id": "INC-1", "shipment_id": "PH-2841"}}
        )


def test_blank_identifier_is_rejected() -> None:
    with pytest.raises(EnvelopeError, match="non-empty"):
        parse_envelope(
            {
                "step": "OPEN",
                "workflow": {
                    "tenant_id": "   ",
                    "incident_id": "INC-1",
                    "shipment_id": "PH-2841",
                },
            }
        )


def test_unexpected_field_is_rejected() -> None:
    with pytest.raises(EnvelopeError, match="invalid workflow envelope"):
        parse_envelope({"step": "OPEN", **_start_input(), "surprise": True})


def test_non_mapping_event_is_rejected() -> None:
    with pytest.raises(EnvelopeError, match="must be a mapping"):
        parse_envelope(["not", "a", "mapping"])


def test_identifiers_are_stripped() -> None:
    envelope = parse_envelope(
        {
            "step": "OPEN",
            "workflow": {
                "tenant_id": " t1 ",
                "incident_id": " INC-1 ",
                "shipment_id": " PH-2841 ",
            },
        }
    )

    assert (envelope.tenant_id, envelope.incident_id, envelope.shipment_id) == (
        "t1",
        "INC-1",
        "PH-2841",
    )


def test_envelope_is_immutable() -> None:
    envelope = parse_envelope({"step": "OPEN", **_start_input()})

    with pytest.raises(ValidationError):
        envelope.tenant_id = "t2"
