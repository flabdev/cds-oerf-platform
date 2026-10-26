"""Failure-path steps write the incident the execution already decided."""

from datetime import UTC, datetime

import pytest

from domains.cold_chain_excursion.steps.cancel import handler as cancel_handler
from domains.cold_chain_excursion.steps.record_failure import handler as record_handler
from oerf.platform.agent.context import StepDependencies, StepError
from oerf.platform.agent.handlers import cancel, escalate, record_failure
from oerf.platform.agent.idempotency import MemoryStepResultStore
from oerf.platform.incidents.repository import IncidentRepository

NOW = datetime(2026, 10, 20, 10, 16, tzinfo=UTC)
TENANT = "t1"
SHIPMENT = "PH-2841"


def _ports(incidents: IncidentRepository) -> StepDependencies:
    return StepDependencies(
        incidents=incidents,
        results=MemoryStepResultStore(clock=lambda: NOW),
        clock=lambda: NOW,
    )


def _open(incidents: IncidentRepository, shipment: str = SHIPMENT):
    return incidents.create(tenant_id=TENANT, shipment_id=shipment, reason="TEMPERATURE_ABOVE_MAX")


def _event(step: str, incident_id: str, *, state: str | None, **extra: object) -> dict:
    workflow = {
        "tenant_id": TENANT,
        "incident_id": incident_id,
        "shipment_id": SHIPMENT,
        "workflow": {"trigger": {"temperature_c": 12}},
    }
    if state is not None:
        workflow["incident_state"] = state
    workflow.update(extra)
    return {"step_name": step, "workflow": workflow}


def _walk(incidents: IncidentRepository, incident_id: str, *states: str) -> None:
    previous = "OPEN"
    for state in states:
        incidents.transition(TENANT, incident_id, from_state=previous, to_state=state)
        previous = state


def test_record_failure_keeps_the_state_and_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    incidents = IncidentRepository(clock=lambda: NOW)
    ports = _ports(incidents)
    _install(monkeypatch, ports)
    opened = _open(incidents)
    _walk(incidents, opened.incident_id, "DIAGNOSING", "ASSESSING")
    event = _event(
        "RecordFailure",
        opened.incident_id,
        state="ASSESSING",
        failure={"Error": "States.Timeout"},
        no_response=True,
    )

    first = record_handler(event, None)
    second = record_handler(event, None)

    assert first["incident_state"] == "ASSESSING"
    assert second == first
    assert incidents.get(TENANT, opened.incident_id).state == "ASSESSING"
    assert incidents.lock.get_lock(TENANT, SHIPMENT)["incident_id"] == opened.incident_id
    assert [item.state for item in incidents.events(TENANT, opened.incident_id)] == [
        "DIAGNOSING",
        "ASSESSING",
    ]


def test_failed_assessing_run_escalates_and_blocks_a_second_incident() -> None:
    incidents = IncidentRepository(clock=lambda: NOW)
    ports = _ports(incidents)
    opened = _open(incidents)
    _walk(incidents, opened.incident_id, "DIAGNOSING", "ASSESSING")
    recorded = record_failure(
        _envelope("RecordFailure", opened.incident_id, "ASSESSING"),
        ports,
    )
    escalated = escalate(_envelope("Escalate", opened.incident_id, recorded.incident_state), ports)
    again = escalate(_envelope("Escalate", opened.incident_id, "ESCALATED"), ports)

    assert recorded.incident_state == "ASSESSING"
    assert escalated.incident_state == "ESCALATED"
    assert again.incident_state == "ESCALATED"
    assert incidents.get(TENANT, opened.incident_id).state == "ESCALATED"
    assert incidents.lock.get_lock(TENANT, SHIPMENT) is not None
    assert [item.state for item in incidents.events(TENANT, opened.incident_id)].count(
        "ESCALATED"
    ) == 1
    reused = incidents.create(
        tenant_id=TENANT, shipment_id=SHIPMENT, reason="TEMPERATURE_ABOVE_MAX"
    )
    assert reused.incident_id == opened.incident_id


def test_escalate_from_a_state_without_that_edge_is_an_error() -> None:
    incidents = IncidentRepository(clock=lambda: NOW)
    ports = _ports(incidents)
    opened = _open(incidents)
    _walk(incidents, opened.incident_id, "DIAGNOSING")

    with pytest.raises(StepError):
        escalate(_envelope("Escalate", opened.incident_id, "DIAGNOSING"), ports)

    assert incidents.get(TENANT, opened.incident_id).state == "DIAGNOSING"
    assert incidents.lock.get_lock(TENANT, SHIPMENT) is not None


def test_escalate_of_a_closed_incident_writes_nothing() -> None:
    incidents = IncidentRepository(clock=lambda: NOW)
    ports = _ports(incidents)
    opened = _open(incidents)
    _walk(
        incidents,
        opened.incident_id,
        "DIAGNOSING",
        "ASSESSING",
        "SECURING_FACILITY",
        "IN_TRANSIT",
        "VERIFYING",
        "CONTAINED",
        "AWAITING_DISPOSITION",
        "CLOSED",
    )
    before = len(incidents.events(TENANT, opened.incident_id))

    result = escalate(_envelope("Escalate", opened.incident_id, "CLOSED"), ports)

    assert result.incident_state == "CLOSED"
    assert len(incidents.events(TENANT, opened.incident_id)) == before
    assert incidents.lock.get_lock(TENANT, SHIPMENT) is None


def test_escalate_of_a_cancelled_incident_writes_nothing() -> None:
    incidents = IncidentRepository(clock=lambda: NOW)
    ports = _ports(incidents)
    opened = _open(incidents)
    incidents.transition(TENANT, opened.incident_id, from_state="OPEN", to_state="CANCELLED")
    before = len(incidents.events(TENANT, opened.incident_id))

    result = escalate(_envelope("Escalate", opened.incident_id, "CANCELLED"), ports)

    assert result.incident_state == "CANCELLED"
    assert len(incidents.events(TENANT, opened.incident_id)) == before
    assert ports.results.get(
        tenant_id=TENANT, incident_id=opened.incident_id, step="Escalate"
    ) is None


def test_cancel_from_the_failure_default_releases_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incidents = IncidentRepository(clock=lambda: NOW)
    ports = _ports(incidents)
    _install(monkeypatch, ports)
    opened = _open(incidents)
    event = _event("Cancel", opened.incident_id, state="OPEN", cancel_requested=True)

    cancelled = cancel_handler(event, None)
    again = cancel_handler(event, None)
    opened_again = incidents.create(
        tenant_id=TENANT, shipment_id=SHIPMENT, reason="TEMPERATURE_ABOVE_MAX"
    )

    assert cancelled["incident_state"] == "CANCELLED"
    assert again["incident_state"] == "CANCELLED"
    assert incidents.lock.get_lock(TENANT, SHIPMENT)["incident_id"] == opened_again.incident_id
    assert opened_again.incident_id != opened.incident_id
    assert opened_again.state == "OPEN"


def test_cancel_of_an_escalated_incident_releases_the_lock() -> None:
    incidents = IncidentRepository(clock=lambda: NOW)
    ports = _ports(incidents)
    opened = _open(incidents)
    _walk(incidents, opened.incident_id, "DIAGNOSING", "ASSESSING", "ESCALATED")

    cancelled = cancel(_envelope("Cancel", opened.incident_id, "ESCALATED"), ports)

    assert cancelled.incident_state == "CANCELLED"
    assert incidents.lock.get_lock(TENANT, SHIPMENT) is None
    opened_again = incidents.create(
        tenant_id=TENANT, shipment_id=SHIPMENT, reason="TEMPERATURE_ABOVE_MAX"
    )
    assert opened_again.state == "OPEN"
    assert opened_again.incident_id != opened.incident_id


def _install(monkeypatch: pytest.MonkeyPatch, ports: StepDependencies) -> None:
    monkeypatch.setattr("oerf.platform.agent.runtime.build_dependencies", lambda **_kwargs: ports)


def _envelope(step: str, incident_id: str, state: str | None):
    from oerf.platform.agent.envelope import StepEnvelope

    return StepEnvelope(
        step=step,
        tenant_id=TENANT,
        incident_id=incident_id,
        shipment_id=SHIPMENT,
        incident_state=state,
        workflow={"trigger": {"temperature_c": 12}},
    )
