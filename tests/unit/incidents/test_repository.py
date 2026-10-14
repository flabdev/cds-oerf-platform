"""Tests for incident creation and state transitions."""

import threading
from datetime import UTC, datetime

import pytest

from oerf.platform.incidents.lock import OpenIncidentLock
from oerf.platform.incidents.repository import (
    TRANSITIONS,
    IllegalTransition,
    IncidentNotFound,
    IncidentRepository,
    StaleIncidentUpdate,
)

PATH_TO_CLOSED = (
    "DIAGNOSING",
    "ASSESSING",
    "AWAITING_APPROVAL",
    "SECURING_FACILITY",
    "IN_TRANSIT",
    "VERIFYING",
    "CONTAINED",
    "AWAITING_DISPOSITION",
    "CLOSED",
)


def test_create_and_get_incident() -> None:
    repository = IncidentRepository(clock=_clock)

    created = _open(repository)
    loaded = repository.get("t1", created.incident_id)

    assert created.incident_id == "INC-000001"
    assert loaded.tenant_id == "t1"
    assert loaded.shipment_id == "PH-2841"
    assert loaded.state == "OPEN"
    assert loaded.pk == "TENANT#t1#INC#INC-000001"
    assert loaded.sk == "META"
    assert repository.lock.get_lock("t1", "PH-2841") == {
        "pk": "TENANT#t1#SHIP#PH-2841",
        "sk": "OPEN_INCIDENT",
        "incident_id": "INC-000001",
    }


def test_valid_transition_appends_a_timeline_event() -> None:
    repository = IncidentRepository(clock=_clock)
    incident = _open(repository)

    result = repository.transition(
        incident.tenant_id,
        incident.incident_id,
        from_state="OPEN",
        to_state="DIAGNOSING",
    )

    assert result.previous_state == "OPEN"
    assert result.state == "DIAGNOSING"
    events = repository.events("t1", incident.incident_id)
    assert len(events) == 1
    assert events[0].sk.startswith("EVT#")


def test_illegal_transition_is_rejected() -> None:
    repository = IncidentRepository(clock=_clock)
    incident = _open(repository)

    with pytest.raises(IllegalTransition):
        repository.transition("t1", incident.incident_id, from_state="OPEN", to_state="CLOSED")

    assert repository.get("t1", incident.incident_id).state == "OPEN"
    assert repository.events("t1", incident.incident_id) == []


def test_stale_transition_does_not_overwrite() -> None:
    repository = IncidentRepository(clock=_clock)
    incident = _open(repository)
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def attempt() -> None:
        barrier.wait()
        try:
            outcomes.append(
                repository.transition(
                    "t1",
                    incident.incident_id,
                    from_state="OPEN",
                    to_state="DIAGNOSING",
                )
            )
        except StaleIncidentUpdate as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(outcomes) == 2
    assert sum(isinstance(item, StaleIncidentUpdate) for item in outcomes) == 1
    assert repository.get("t1", incident.incident_id).state == "DIAGNOSING"


def test_second_create_appends_to_the_open_incident() -> None:
    repository = IncidentRepository(clock=_clock)
    first = _open(repository)

    second = repository.create(
        tenant_id="t1",
        shipment_id="PH-2841",
        reason="TEMPERATURE_STILL_ABOVE_MAX",
    )

    assert second.incident_id == first.incident_id
    events = repository.events("t1", first.incident_id)
    assert len(events) == 1
    assert events[0].state == "OPEN"
    assert events[0].previous_state == "OPEN"
    assert events[0].reason == "TEMPERATURE_STILL_ABOVE_MAX"
    stored = repository.lock.get_lock("t1", "PH-2841")
    assert stored is not None
    assert stored["incident_id"] == first.incident_id


def test_concurrent_creates_open_one_incident() -> None:
    repository = IncidentRepository(clock=_clock)
    barrier = threading.Barrier(2)
    created: list[str] = []

    def attempt() -> None:
        barrier.wait()
        created.append(_open(repository).incident_id)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert created == ["INC-000001", "INC-000001"]
    assert len(repository.events("t1", "INC-000001")) == 1
    stored = repository.lock.get_lock("t1", "PH-2841")
    assert stored is not None
    assert stored["incident_id"] == "INC-000001"


def test_close_releases_the_shipment_lock() -> None:
    lock = OpenIncidentLock()
    repository = IncidentRepository(lock, clock=_clock)
    incident = _open(repository)
    _walk(repository, incident.incident_id, PATH_TO_CLOSED)

    assert repository.get("t1", incident.incident_id).state == "CLOSED"
    assert lock.get_lock("t1", "PH-2841") is None


def test_cancel_releases_the_shipment_lock() -> None:
    lock = OpenIncidentLock()
    repository = IncidentRepository(lock, clock=_clock)
    incident = _open(repository)

    repository.transition("t1", incident.incident_id, from_state="OPEN", to_state="CANCELLED")

    assert lock.get_lock("t1", "PH-2841") is None
    assert "CANCELLED" in TRANSITIONS["OPEN"]


def test_missing_incident_is_rejected() -> None:
    repository = IncidentRepository()

    with pytest.raises(IncidentNotFound):
        repository.get("t1", "INC-000099")


def _open(repository: IncidentRepository):
    return repository.create(
        tenant_id="t1",
        shipment_id="PH-2841",
        reason="TEMPERATURE_ABOVE_MAX",
    )


def _walk(repository: IncidentRepository, incident_id: str, states: tuple[str, ...]) -> None:
    current = "OPEN"
    for state in states:
        repository.transition("t1", incident_id, from_state=current, to_state=state)
        current = state


def _clock() -> datetime:
    return datetime(2026, 10, 20, 10, 16, tzinfo=UTC)
