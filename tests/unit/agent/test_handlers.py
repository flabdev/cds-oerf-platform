"""Handler contracts for cold-chain workflow steps."""

from datetime import UTC, datetime
from typing import Any

import pytest

from oerf.platform.agent.carriers import decision
from oerf.platform.agent.context import (
    MissingStepInput,
    StepDependencies,
    StepError,
    pinned_config,
)
from oerf.platform.agent.envelope import StepEnvelope
from oerf.platform.agent.handlers import HANDLERS
from oerf.platform.agent.idempotency import MemoryStepResultStore
from oerf.platform.incidents.repository import IncidentRepository
from oerf.platform.tenancy.config_resolver import get_config

TENANT = "t1"
SHIPMENT = "PH-2841"
INCIDENT_ID = "INC-000001"
NOW = datetime(2026, 10, 20, 10, 16, tzinfo=UTC)
_PATH = (
    "DIAGNOSING",
    "ASSESSING",
    "SECURING_FACILITY",
    "IN_TRANSIT",
    "VERIFYING",
    "CONTAINED",
    "AWAITING_DISPOSITION",
)


def _dependencies(state: str) -> StepDependencies:
    config = get_config(TENANT, "cold_chain_excursion")
    incidents = IncidentRepository(clock=lambda: NOW)
    incident = incidents.create(
        tenant_id=TENANT,
        shipment_id=SHIPMENT,
        reason="temperature excursion",
        config_version=config.version,
        config_snapshot=config.model_dump(),
    )
    previous = "OPEN"
    for next_state in _PATH[: _PATH.index(state) + 1] if state != "OPEN" else ():
        incidents.transition(TENANT, incident.incident_id, from_state=previous, to_state=next_state)
        previous = next_state
    return StepDependencies(
        incidents=incidents,
        results=MemoryStepResultStore(clock=lambda: NOW),
        clock=lambda: NOW,
    )


def _envelope(step: str, **values: Any) -> StepEnvelope:
    return StepEnvelope(
        step=step,
        tenant_id=TENANT,
        incident_id=INCIDENT_ID,
        shipment_id=SHIPMENT,
        **values,
    )


def test_six_ticket_handlers_and_act_are_registered() -> None:
    assert {"GatherContext", "Assess", "Act", "Verify", "QA", "Close"} <= HANDLERS.keys()


def test_gather_context_uses_pinned_profile_and_advances_incident() -> None:
    dependencies = _dependencies("OPEN")
    envelope = _envelope(
        "GatherContext",
        workflow={
            "trigger": {"temperature_c": 9.2},
            "shipment": {"product_profile_id": "PP-2-8-45"},
            "facilities": [{"id": "facility-1", "drive_min": 12}],
        },
    )

    result = HANDLERS["GatherContext"](envelope, dependencies)

    assert result.workflow["context"]["range_c"] == [2.0, 8.0]
    assert result.workflow["context"]["allowance_minutes"] == 45.0
    assert result.workflow["context"]["facilities"] == [{"id": "facility-1", "drive_min": 12}]
    assert dependencies.incidents.get(TENANT, INCIDENT_ID).state == "DIAGNOSING"


def test_assess_escalates_when_no_option_is_feasible() -> None:
    dependencies = _dependencies("DIAGNOSING")
    envelope = _envelope(
        "Assess",
        workflow={
            "ship_time": "2026-10-20T09:00:00Z",
            "context": {
                "range_c": [2, 8],
                "allowance_minutes": 45,
                "trigger": {"temperature_c": 9.2},
                "facilities": [],
            },
        },
    )

    result = HANDLERS["Assess"](envelope, dependencies)

    assert result.decision is not None
    assert result.decision["route"] == "escalate"
    assert result.workflow["escalation"]["reason"] == "NO_FEASIBLE_OPTION"
    assert dependencies.incidents.get(TENANT, INCIDENT_ID).state == "ESCALATED"


def test_qa_records_gate_without_state_change_and_replays() -> None:
    dependencies = _dependencies("AWAITING_DISPOSITION")
    envelope = _envelope(
        "QA",
        workflow={
            "review": {"approved": True, "disposition": "release", "reviewer_role": "qa_lead"}
        },
    )

    result = HANDLERS["QA"](envelope, dependencies)
    retry = HANDLERS["QA"](_envelope("QA"), dependencies)

    assert result.workflow["qa"]["disposition"] == "approved"
    assert result.qa == result.workflow["qa"]
    assert result.incident_state == "AWAITING_DISPOSITION"
    assert dependencies.incidents.get(TENANT, INCIDENT_ID).state == "AWAITING_DISPOSITION"
    assert retry == result


def test_qa_rejection_is_recorded_for_escalation() -> None:
    dependencies = _dependencies("AWAITING_DISPOSITION")
    result = HANDLERS["QA"](
        _envelope("QA", workflow={"qa_review": {"approved": False, "disposition": "quarantine"}}),
        dependencies,
    )

    assert result.qa is not None
    assert result.qa["disposition"] == "rejected"
    assert result.workflow["escalation"]["reason"] == "QA_REJECTED"
    assert dependencies.incidents.get(TENANT, INCIDENT_ID).state == "AWAITING_DISPOSITION"


def test_close_requires_approved_qa_gate() -> None:
    dependencies = _dependencies("AWAITING_DISPOSITION")

    with pytest.raises(StepError, match="cannot close while QA is rejected"):
        HANDLERS["Close"](
            _envelope("Close", workflow={"qa": {"disposition": "rejected"}}), dependencies
        )
    with pytest.raises(MissingStepInput, match="QA verdict"):
        HANDLERS["Close"](_envelope("Close"), dependencies)
    assert dependencies.incidents.get(TENANT, INCIDENT_ID).state == "AWAITING_DISPOSITION"


def test_close_closes_once_and_replays_stored_envelope() -> None:
    dependencies = _dependencies("AWAITING_DISPOSITION")
    envelope = _envelope(
        "Close",
        workflow={
            "qa": {
                "disposition": "approved",
                "closure_disposition": "release",
                "reviewer_role": "qa_lead",
            }
        },
    )

    result = HANDLERS["Close"](envelope, dependencies)
    retry = HANDLERS["Close"](_envelope("Close"), dependencies)

    assert result.workflow["closed"] is True
    assert result.workflow["closure"]["disposition"] == "release"
    assert dependencies.incidents.get(TENANT, INCIDENT_ID).state == "CLOSED"
    assert retry == result


def test_close_rejects_unknown_configured_disposition() -> None:
    dependencies = _dependencies("AWAITING_DISPOSITION")
    with pytest.raises(StepError, match="not one of the configured"):
        HANDLERS["Close"](
            _envelope(
                "Close",
                workflow={"qa": {"disposition": "approved", "closure_disposition": "unknown"}},
            ),
            dependencies,
        )


def test_act_walks_both_legal_hops_with_missing_approval_gate() -> None:
    dependencies = _dependencies("ASSESSING")
    envelope = _envelope(
        "Act",
        workflow={
            "context": {"facilities": []},
            "assessment": {
                "options": [
                    {"id": "continue", "kind": "continue", "feasible": True, "drive_min": 0}
                ],
                "recommended_option": "continue",
            },
        },
    )

    result = HANDLERS["Act"](envelope, dependencies)

    assert result.incident_state == "IN_TRANSIT"
    assert result.approval is None
    assert [event.state for event in dependencies.incidents.events(TENANT, INCIDENT_ID)][-2:] == [
        "SECURING_FACILITY",
        "IN_TRANSIT",
    ]
    assert result.workflow["action"]["kind"] == "continue"


def test_verify_stabilizing_does_not_store_stale_result() -> None:
    dependencies = _dependencies("IN_TRANSIT")
    envelope = _envelope(
        "Verify",
        workflow={
            "context": {
                "range_c": [2, 8],
                "allowance_minutes": 45,
                "ship_time": "2026-10-20T10:00:00Z",
            },
            "action": {"at": "2026-10-20T10:15:00Z"},
            "temperature_c": 5,
        },
    )

    result = HANDLERS["Verify"](envelope, dependencies)
    assert result.containment is not None
    assert result.containment["status"] == "stabilizing"
    assert (
        dependencies.results.get(tenant_id=TENANT, incident_id=INCIDENT_ID, step="Verify") is None
    )
    assert dependencies.incidents.get(TENANT, INCIDENT_ID).state == "IN_TRANSIT"

    later = HANDLERS["Verify"](
        _envelope(
            "Verify",
            workflow={
                **envelope.workflow,
                "action": {"at": "2026-10-20T10:00:00Z"},
            },
        ),
        dependencies,
    )
    assert later.containment is not None
    assert later.containment["status"] == "contained"
    assert dependencies.incidents.get(TENANT, INCIDENT_ID).state == "CONTAINED"
    assert HANDLERS["Verify"](_envelope("Verify"), dependencies) == later


def test_assess_decision_records_auditable_provenance() -> None:
    """A decision names the deterministic rule and the pinned config, never AI."""
    dependencies = _dependencies("DIAGNOSING")
    envelope = _envelope(
        "Assess",
        workflow={
            "ship_time": "2026-10-20T09:00:00Z",
            "context": {
                "range_c": [2, 8],
                "allowance_minutes": 45,
                "trigger": {"temperature_c": 9.2},
                "facilities": [],
            },
        },
    )

    result = HANDLERS["Assess"](envelope, dependencies)
    assert result.decision is not None
    assert result.decision["rule"] == "assess.rank_options"
    provenance = result.decision["provenance"]
    incident = dependencies.incidents.get(TENANT, INCIDENT_ID)

    assert provenance["source"] == "deterministic_rules"
    assert provenance["rule"] == "assess.rank_options"
    assert provenance["ai_assisted"] is False
    assert "suggestion" not in provenance
    assert provenance["config_source"] == "pinned_snapshot"
    assert provenance["config_version"] == incident.config_version
    assert provenance["inputs"]["temperature_c"] == 9.2


def test_a_suggestion_is_never_authoritative_over_the_rule() -> None:
    """AI output is recorded beside the decision, not instead of it."""
    dependencies = _dependencies("DIAGNOSING")
    incident = dependencies.incidents.get(TENANT, INCIDENT_ID)
    config = pinned_config(incident, dependencies)
    suggestion = {"recommended_option": "continue", "confidence": 0.4}

    record = decision(
        "assess.rank_options",
        config=config,
        inputs={"temperature_c": 9.2},
        ai=suggestion,
        has_feasible_options=False,
        route="escalate",
    )
    provenance = record["provenance"]

    assert record["route"] == "escalate"
    assert provenance["source"] == "deterministic_rules"
    assert provenance["ai_assisted"] is True
    assert provenance["suggestion"] == suggestion
