"""Tests for the per-shipment open-incident lock."""

import threading

import pytest
from pydantic import ValidationError

from oerf.platform.incidents.lock import OpenIncidentLock


def test_first_acquire_creates_the_lock() -> None:
    lock = OpenIncidentLock()

    result = lock.acquire_lock("t1", "PH-2841", "INC-000001")

    assert result.created is True
    assert result.incident_id == "INC-000001"
    stored = lock.get_lock("t1", "PH-2841")
    assert stored is not None
    assert stored["pk"] == "TENANT#t1#SHIP#PH-2841"
    assert stored["sk"] == "OPEN_INCIDENT"


def test_second_acquire_returns_the_existing_incident() -> None:
    lock = OpenIncidentLock()
    lock.acquire_lock("t1", "PH-2841", "INC-000001")

    result = lock.acquire_lock("t1", "PH-2841", "INC-000002")

    assert result.model_dump() == {
        "created": False,
        "incident_id": "INC-000001",
        "reason": "OPEN_INCIDENT_EXISTS",
    }


def test_release_allows_a_new_lock() -> None:
    lock = OpenIncidentLock()
    lock.acquire_lock("t1", "PH-2841", "INC-000001")
    lock.release_lock("t1", "PH-2841")

    result = lock.acquire_lock("t1", "PH-2841", "INC-000002")

    assert result.created is True
    assert result.incident_id == "INC-000002"


def test_concurrent_acquires_create_one_lock() -> None:
    lock = OpenIncidentLock()
    barrier = threading.Barrier(2)
    results = []

    def attempt(incident_id: str) -> None:
        barrier.wait()
        results.append(lock.acquire_lock("t1", "PH-2841", incident_id))

    threads = [
        threading.Thread(target=attempt, args=("INC-000001",)),
        threading.Thread(target=attempt, args=("INC-000002",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    created = [result for result in results if result.created]
    assert len(created) == 1
    assert lock.get_lock("t1", "PH-2841") is not None


def test_blank_tenant_is_rejected() -> None:
    lock = OpenIncidentLock()

    with pytest.raises(ValidationError):
        lock.acquire_lock("  ", "PH-2841", "INC-000001")
