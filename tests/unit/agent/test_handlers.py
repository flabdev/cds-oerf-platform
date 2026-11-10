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
from oerf.platform.agent.reports import ReportPublishError
from oerf.platform.incidents.repository import IncidentRepository
from oerf.platform.tenancy.config_resolver import get_config


class _Reporter:
    """A report port that records the calls the step made."""

    def __init__(self, *, ok: bool = True) -> None:
        self.calls: list[tuple[str, str]] = []
        self._ok = ok

    def publish(self, *, tenant_id: str, incident_id: str) -> dict[str, Any]:
        self.calls.append((tenant_id, incident_id))
        if not self._ok:
            raise ReportPublishError("bucket is unavailable")
        return {
            "success": True,
            "report_key": f"reports/{tenant_id}/{incident_id}/v1.html",
            "s3_uri": f"s3://reports/reports/{tenant_id}/{incident_id}/v1.html",
            "report_version": 1,
            "sha256": "0" * 64,
            "locked": False,
            "generated_at": "2026-10-20T10:16:00Z",
            "sections": ["incident"],
        }

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


def _dependencies(state: str, *, clock=lambda: NOW) -> StepDependencies:
    config = get_config(TENANT, "cold_chain_excursion")
    incidents = IncidentRepository(clock=clock)
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
        clock=clock,
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
    assert {
        "GatherContext",
        "Assess",
        "Act",
        "Verify",
        "RecordContainment",
        "BeginDisposition",
        "QA",
        "Close",
    } <= HANDLERS.keys()


def test_c0_handlers_are_registered() -> None:
    """C0 wired the last eight states; every one has a handler."""
    assert {
        "OPEN",
        "Approval",
        "SecureFacility",
        "RecordContainment",
        "BeginDisposition",
        "HandleReopen",
        "GenerateReport",
        "NoResponse",
    } <= HANDLERS.keys()


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


def test_qa_reads_the_reviewer_callback_result() -> None:
    dependencies = _dependencies("AWAITING_DISPOSITION")
    envelope = _envelope("QA", qa_review={
        "approved": True,
        "disposition": "release",
        "reviewer_role": "tenant_admin",
        "reviewed_at": "2026-10-20T10:20:00Z",
    })

    result = HANDLERS["QA"](envelope, dependencies)

    assert result.qa is not None
    assert result.qa["disposition"] == "approved"
    assert result.qa["reviewer_role"] == "tenant_admin"


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


def test_verify_uses_latest_persisted_readings_for_continuous_window() -> None:
    dependencies = _dependencies(
        "IN_TRANSIT", clock=lambda: datetime(2026, 10, 20, 10, 20, tzinfo=UTC)
    )
    for event_id, timestamp, temperature, sequence in (
        ("old-out", "2026-10-20T10:09:00Z", 9.0, 1),
        ("start-in", "2026-10-20T10:10:00Z", 5.0, 2),
        ("latest-in", "2026-10-20T10:15:00Z", 5.5, 3),
    ):
        assert dependencies.incidents.record_reading(
            TENANT,
            SHIPMENT,
            event_id=event_id,
            timestamp=timestamp,
            temperature_c=temperature,
            sequence=sequence,
        )
    envelope = _envelope(
        "Verify",
        workflow={
            "context": {
                "range_c": [2, 8],
                "allowance_minutes": 45,
                "ship_time": "2026-10-20T10:00:00Z",
            },
            "action": {"at": "2026-10-20T10:15:00Z"},
            "temperature_c": 99,
        },
    )

    result = HANDLERS["Verify"](envelope, dependencies)

    assert result.containment is not None
    assert result.containment["temperature_c"] == 5.5
    assert result.containment["reading_at"] == "2026-10-20T10:15:00Z"
    assert result.containment["stable_minutes"] == 5
    assert result.containment["status"] == "contained"
    assert result.incident_state == "VERIFYING"
    assert dependencies.incidents.get(TENANT, INCIDENT_ID).state == "VERIFYING"


def test_verify_attempt_cap_routes_to_escalation() -> None:
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
            "containment": {"status": "stabilizing", "attempt": 19},
        },
    )

    result = HANDLERS["Verify"](envelope, dependencies)

    assert result.containment is not None
    assert result.containment["status"] == "escalate"
    assert result.containment["attempt"] == 20
    assert result.containment["attempt_cap_reached"] is True
    assert result.incident_state == "VERIFYING"
    assert dependencies.incidents.get(TENANT, INCIDENT_ID).state == "VERIFYING"
    assert (
        dependencies.results.get(
            tenant_id=TENANT, incident_id=INCIDENT_ID, step="Verify"
        )
        is None
    )
    escalated = HANDLERS["Escalate"](
        _envelope("Escalate", workflow=result.workflow), dependencies
    )
    assert escalated.incident_state == "ESCALATED"
    assert dependencies.incidents.get(TENANT, INCIDENT_ID).state == "ESCALATED"


def test_record_containment_and_begin_disposition_replay_idempotently() -> None:
    dependencies = _dependencies("VERIFYING")
    containment = {"status": "contained", "temperature_c": 5.0, "attempt": 2}
    envelope = _envelope("RecordContainment", workflow={"containment": containment})

    recorded = HANDLERS["RecordContainment"](envelope, dependencies)
    replay = HANDLERS["RecordContainment"](_envelope("RecordContainment"), dependencies)
    assert recorded.workflow["containment"]["recorded_at"] == "2026-10-20T10:16:00Z"
    assert replay == recorded
    assert dependencies.incidents.get(TENANT, INCIDENT_ID).state == "CONTAINED"
    assert [event.state for event in dependencies.incidents.events(TENANT, INCIDENT_ID)][-1] == (
        "CONTAINED"
    )

    begin = _envelope(
        "BeginDisposition",
        workflow=recorded.workflow,
        containment=recorded.containment,
    )
    started = HANDLERS["BeginDisposition"](begin, dependencies)
    begin_replay = HANDLERS["BeginDisposition"](_envelope("BeginDisposition"), dependencies)
    assert started.workflow["disposition_started_at"] == "2026-10-20T10:16:00Z"
    assert begin_replay == started
    assert dependencies.incidents.get(TENANT, INCIDENT_ID).state == "AWAITING_DISPOSITION"
    assert [event.state for event in dependencies.incidents.events(TENANT, INCIDENT_ID)][-1] == (
        "AWAITING_DISPOSITION"
    )


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
    assert dependencies.incidents.get(TENANT, INCIDENT_ID).state == "VERIFYING"

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
    assert dependencies.incidents.get(TENANT, INCIDENT_ID).state == "VERIFYING"
    recorded = HANDLERS["RecordContainment"](
        _envelope("RecordContainment", workflow=later.workflow, containment=later.containment),
        dependencies,
    )
    assert recorded.incident_state == "CONTAINED"
    assert dependencies.incidents.get(TENANT, INCIDENT_ID).state == "CONTAINED"
    assert HANDLERS["RecordContainment"](_envelope("RecordContainment"), dependencies) == recorded


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


def test_stopped_unit_uses_a_pinned_faster_rise() -> None:
    dependencies = _dependencies("DIAGNOSING")
    incident = dependencies.incidents.get(TENANT, INCIDENT_ID)
    incident.config_snapshot["assessment"]["stopped_rise_rate_c_per_min"] = 0.5
    result = HANDLERS["Assess"](
        _envelope(
            "Assess",
            workflow={
                "ship_time": NOW.isoformat(),
                "diagnosis": {
                    "engagement_result": {"status": "answered", "intent": "unit_stopped"}
                },
                "context": {
                    "range_c": [2, 8],
                    "allowance_minutes": 45,
                    "trigger": {"temperature_c": 8.1},
                    "facilities": [
                        {"id": "FAC-A", "drive_min": 9, "capacity_available": True}
                    ],
                },
            },
        ),
        dependencies,
    )

    assert result.decision is not None
    assert result.decision["provenance"]["inputs"]["rise_rate_c_per_min"] == 0.5
    assert result.decision["route"] == "escalate"
    assert result.decision["recommended_option"] is None


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


def _approval_workflow(**values: Any) -> dict[str, Any]:
    workflow: dict[str, Any] = {
        "context": {"range_c": [2, 8], "allowance_minutes": 45},
        "assessment": {
            "route": "approval",
            "needs_approval": True,
            "recommended_option": "FAC-A",
            "options": [
                {"id": "FAC-A", "kind": "facility", "feasible": True, "rank": 1, "drive_min": 9},
                {"id": "FAC-B", "kind": "facility", "feasible": True, "rank": 2, "drive_min": 21},
            ],
        },
    }
    workflow.update(values)
    return workflow


def _at_awaiting_approval() -> StepDependencies:
    """Dependencies for an incident that has reached ``AWAITING_APPROVAL``."""

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
    for next_state in ("DIAGNOSING", "ASSESSING", "AWAITING_APPROVAL"):
        incidents.transition(TENANT, incident.incident_id, from_state=previous, to_state=next_state)
        previous = next_state
    return StepDependencies(
        incidents=incidents,
        results=MemoryStepResultStore(clock=lambda: NOW),
        clock=lambda: NOW,
    )


def test_open_records_the_pinned_config_and_replays() -> None:
    """C8: OPEN was the last cold-chain stub; the handler pins config and is idempotent."""

    dependencies = _dependencies("OPEN")
    envelope = _envelope("OPEN", workflow={"trigger": {"temperature_c": 9.2}})

    result = HANDLERS["OPEN"](envelope, dependencies)
    retry = HANDLERS["OPEN"](_envelope("OPEN"), dependencies)

    incident = dependencies.incidents.get(TENANT, INCIDENT_ID)
    assert result.incident_state == "OPEN"
    assert result.current_stage == "OPEN"
    assert result.workflow["config_version"] == incident.config_version
    assert result.workflow["opened_at"] == incident.opened_at
    assert result.decision is not None and result.decision["rule"] == "open.record_incident"
    assert retry == result
    assert incident.state == "OPEN"


def test_open_refuses_when_the_incident_already_moved() -> None:
    dependencies = _dependencies("DIAGNOSING")

    with pytest.raises(StepError, match="must run while it is OPEN"):
        HANDLERS["OPEN"](_envelope("OPEN", workflow={}), dependencies)


def test_approval_records_the_approvers_decision() -> None:
    """C5: the ops-manager reply names the outcome ApprovalDecision reads."""

    dependencies = _at_awaiting_approval()
    envelope = _envelope(
        "Approval",
        workflow=_approval_workflow(
            approval_response={"status": "approved", "option_id": "FAC-B"}
        ),
    )

    result = HANDLERS["Approval"](envelope, dependencies)

    assert result.approval is not None
    assert result.approval["status"] == "approved"
    assert result.approval["option_id"] == "FAC-B"
    assert result.approval["approver_role"] == "ops_manager"
    assert result.approval["policy_id"] == "approval_fast"
    assert result.workflow["approval_request"]["engagement_id"].endswith("-ops_manager-approve")
    assert result.incident_state == "AWAITING_APPROVAL"


def test_approval_auto_proceeds_within_limits_without_a_reply() -> None:
    dependencies = _at_awaiting_approval()
    envelope = _envelope(
        "Approval",
        workflow=_approval_workflow(
            assessment={
                "route": "auto_proceed",
                "needs_approval": False,
                "recommended_option": "FAC-A",
                "options": [{"id": "FAC-A", "kind": "facility", "feasible": True}],
            }
        ),
    )

    result = HANDLERS["Approval"](envelope, dependencies)

    assert result.approval is not None
    assert result.approval["status"] == "auto_proceed"
    assert result.approval["option_id"] == "FAC-A"


def test_approval_without_a_decision_or_auto_proceed_fails() -> None:
    dependencies = _at_awaiting_approval()

    with pytest.raises(MissingStepInput, match="approver's decision"):
        HANDLERS["Approval"](_envelope("Approval", workflow=_approval_workflow()), dependencies)


def _facility_context() -> dict[str, Any]:
    return {
        "range_c": [2, 8],
        "allowance_minutes": 45,
        "facilities": [
            {"id": "FAC-A", "drive_min": 9, "capacity_available": True, "approved": True},
            {"id": "FAC-B", "drive_min": 21, "capacity_available": True, "approved": True},
        ],
    }


def test_secure_facility_decline_moves_to_the_next_feasible_facility() -> None:
    """C6: facility_a_declines continues to FAC-B instead of escalating."""

    dependencies = _dependencies("SECURING_FACILITY")
    envelope = _envelope(
        "SecureFacility",
        workflow=_approval_workflow(
            facility_response={"status": "declined"}, context=_facility_context()
        ),
    )

    result = HANDLERS["SecureFacility"](envelope, dependencies)

    assert result.facility is not None
    assert result.facility["accepted"] is False
    assert result.facility["facility_id"] == "FAC-A"
    assert result.facility["next_option_id"] == "FAC-B"
    assert result.facility["next_facility_id"] == "FAC-B"
    assert result.incident_state == "SECURING_FACILITY"


def test_secure_facility_fallback_uses_next_option_under_a_distinct_idempotency_key() -> None:
    dependencies = _dependencies("SECURING_FACILITY")
    initial = HANDLERS["SecureFacility"](
        _envelope(
            "SecureFacility",
            workflow=_approval_workflow(
                facility_response={"status": "declined"}, context=_facility_context()
            ),
        ),
        dependencies,
    )

    fallback_workflow = {
        **initial.workflow,
        "facility_response": {"status": "accepted"},
    }
    result = HANDLERS["SecureFacilityFallback"](
        _envelope("SecureFacilityFallback", workflow=fallback_workflow), dependencies
    )
    replay = HANDLERS["SecureFacilityFallback"](
        _envelope("SecureFacilityFallback"), dependencies
    )

    assert result.facility is not None
    assert result.facility["accepted"] is True
    assert result.facility["facility_id"] == "FAC-B"
    assert result.workflow["assessment"]["recommended_option"] == "FAC-B"
    assert result.last_completed_step == "SecureFacilityFallback"
    assert replay == result
    assert dependencies.results.get(
        tenant_id=TENANT, incident_id=INCIDENT_ID, step="SecureFacility"
    ) is not None
    assert dependencies.results.get(
        tenant_id=TENANT, incident_id=INCIDENT_ID, step="SecureFacilityFallback"
    ) is not None


def test_secure_facility_fallback_requires_a_recorded_next_option() -> None:
    dependencies = _dependencies("SECURING_FACILITY")
    workflow = _approval_workflow(
        facility={"accepted": False, "kind": "facility", "next_option_id": None},
        context=_facility_context(),
    )

    with pytest.raises(MissingStepInput, match="next feasible facility"):
        HANDLERS["SecureFacilityFallback"](
            _envelope("SecureFacilityFallback", workflow=workflow), dependencies
        )


    dependencies = _dependencies("SECURING_FACILITY")
    envelope = _envelope(
        "SecureFacility",
        workflow=_approval_workflow(
            facility_response={"status": "declined"},
            assessment={
                "route": "approval",
                "recommended_option": "FAC-A",
                "options": [{"id": "FAC-A", "kind": "facility", "feasible": True, "rank": 1}],
            },
            context=_facility_context(),
        ),
    )

    result = HANDLERS["SecureFacility"](envelope, dependencies)

    assert result.facility is not None
    assert result.facility["accepted"] is False
    assert result.facility["next_option_id"] is None


def test_secure_facility_accepts_a_pre_approved_facility() -> None:
    dependencies = _dependencies("SECURING_FACILITY")
    envelope = _envelope(
        "SecureFacility",
        workflow=_approval_workflow(context=_facility_context()),
    )

    result = HANDLERS["SecureFacility"](envelope, dependencies)

    assert result.facility is not None
    assert result.facility["accepted"] is True
    assert result.facility["facility_id"] == "FAC-A"


def test_open_pins_the_config_snapshot_and_replays() -> None:
    """C0: OPEN was the last stub; the handler pins config and is idempotent."""
    dependencies = _dependencies("OPEN")
    envelope = _envelope("OPEN", workflow={"trigger": {"temperature_c": 9.2}})

    result = HANDLERS["OPEN"](envelope, dependencies)
    retry = HANDLERS["OPEN"](_envelope("OPEN"), dependencies)

    incident = dependencies.incidents.get(TENANT, INCIDENT_ID)
    assert result.incident_state == "OPEN"
    assert result.current_stage == "OPEN"
    assert result.workflow["config_version"] == incident.config_version
    assert result.workflow["opened_at"] == incident.opened_at
    assert result.decision is not None
    assert result.decision["status"] == "open"
    assert retry == result
    assert dependencies.incidents.get(TENANT, INCIDENT_ID).state == "OPEN"


def test_open_refuses_an_incident_that_already_moved() -> None:
    dependencies = _dependencies("DIAGNOSING")

    with pytest.raises(StepError, match="must run while it is OPEN"):
        HANDLERS["OPEN"](_envelope("OPEN"), dependencies)


def test_handle_reopen_marks_the_containment_reopened_and_replays() -> None:
    dependencies = _dependencies("AWAITING_DISPOSITION")
    containment = {"status": "reopened", "temperature_c": 9.1, "attempt": 3}
    envelope = _envelope("HandleReopen", workflow={"containment": containment})

    result = HANDLERS["HandleReopen"](envelope, dependencies)
    retry = HANDLERS["HandleReopen"](_envelope("HandleReopen"), dependencies)

    assert result.incident_state == "REOPENED"
    assert result.containment is not None
    assert result.containment["status"] == "reopened"
    assert result.containment["reopened_at"] == "2026-10-20T10:16:00Z"
    assert retry == result
    assert dependencies.incidents.get(TENANT, INCIDENT_ID).state == "REOPENED"


def test_handle_reopen_needs_a_reopened_verdict() -> None:
    dependencies = _dependencies("AWAITING_DISPOSITION")

    with pytest.raises(MissingStepInput, match="reopened containment verdict"):
        HANDLERS["HandleReopen"](_envelope("HandleReopen"), dependencies)


def test_no_response_records_the_timeout_without_moving_the_incident() -> None:
    dependencies = _dependencies("AWAITING_DISPOSITION")
    envelope = _envelope(
        "NoResponse",
        workflow={"approval": {"status": "pending", "approver_role": "ops_manager"}},
    )

    result = HANDLERS["NoResponse"](envelope, dependencies)
    retry = HANDLERS["NoResponse"](_envelope("NoResponse"), dependencies)

    assert result.approval is not None
    assert result.approval["status"] == "no_response"
    assert result.approval["timed_out"] is True
    assert result.approval["timed_out_at"] == "2026-10-20T10:16:00Z"
    assert result.approval["approver_role"] == "ops_manager"
    # FailureRoute reads incident_state, so the timeout must not move it.
    assert result.incident_state == "AWAITING_DISPOSITION"
    assert result.current_stage == "AWAITING_APPROVAL"
    assert retry == result
    assert dependencies.incidents.get(TENANT, INCIDENT_ID).state == "AWAITING_DISPOSITION"


def test_generate_report_publishes_once_and_records_the_receipt() -> None:
    dependencies = _dependencies("AWAITING_DISPOSITION")
    reporter = _Reporter()
    dependencies = StepDependencies(
        incidents=dependencies.incidents,
        results=dependencies.results,
        reports=reporter,
        clock=lambda: NOW,
    )

    result = HANDLERS["GenerateReport"](_envelope("GenerateReport"), dependencies)
    retry = HANDLERS["GenerateReport"](_envelope("GenerateReport"), dependencies)

    assert reporter.calls == [(TENANT, INCIDENT_ID)]
    assert result.workflow["report"]["report_version"] == 1
    assert result.workflow["report"]["s3_uri"].endswith("v1.html")
    assert result.current_stage == "AWAITING_DISPOSITION"
    # The step does not move the incident; the CancelAfter gate is the only edge.
    assert result.incident_state == "AWAITING_DISPOSITION"
    assert retry == result
    assert dependencies.incidents.get(TENANT, INCIDENT_ID).state == "AWAITING_DISPOSITION"


def test_generate_report_fails_loudly_without_a_publisher() -> None:
    dependencies = _dependencies("AWAITING_DISPOSITION")

    with pytest.raises(StepError, match="no report publisher configured"):
        HANDLERS["GenerateReport"](_envelope("GenerateReport"), dependencies)


def test_generate_report_propagates_a_publish_failure() -> None:
    """A failed publish must fail the step so Step Functions routes RecordFailure."""
    dependencies = _dependencies("AWAITING_DISPOSITION")
    dependencies = StepDependencies(
        incidents=dependencies.incidents,
        results=dependencies.results,
        reports=_Reporter(ok=False),
        clock=lambda: NOW,
    )

    with pytest.raises(ReportPublishError, match="bucket is unavailable"):
        HANDLERS["GenerateReport"](_envelope("GenerateReport"), dependencies)
