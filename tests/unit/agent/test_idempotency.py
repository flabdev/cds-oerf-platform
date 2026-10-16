"""A step that already finished replays its stored result instead of repeating work."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from oerf.platform.agent.idempotency import MemoryStepResultStore, StepRecord

FIXED = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)


def _store() -> MemoryStepResultStore:
    return MemoryStepResultStore(clock=lambda: FIXED)


def test_first_write_is_stored_with_the_step_identifiers() -> None:
    store = _store()

    record = store.put(
        tenant_id="t1",
        incident_id="INC-1",
        step="Assess",
        decision={"needs_approval": True},
        result={"ranked": ["facility-a"]},
    )

    assert isinstance(record, StepRecord)
    assert (record.tenant_id, record.incident_id, record.step) == ("t1", "INC-1", "Assess")
    assert record.decision == {"needs_approval": True}
    assert record.result == {"ranked": ["facility-a"]}
    assert record.completed_at == "2026-10-20T10:00:00Z"
    assert store.get(tenant_id="t1", incident_id="INC-1", step="Assess") == record


def test_repeat_of_a_finished_step_keeps_the_first_record() -> None:
    store = _store()
    first = store.put(
        tenant_id="t1",
        incident_id="INC-1",
        step="Assess",
        decision={"needs_approval": True},
    )

    repeat = store.put(
        tenant_id="t1",
        incident_id="INC-1",
        step="Assess",
        decision={"needs_approval": False},
    )

    assert repeat == first
    stored = store.get(tenant_id="t1", incident_id="INC-1", step="Assess")
    assert stored is not None
    assert stored.decision == {"needs_approval": True}


def test_another_step_of_the_same_incident_is_a_separate_key() -> None:
    store = _store()
    store.put(tenant_id="t1", incident_id="INC-1", step="Assess")

    assert store.get(tenant_id="t1", incident_id="INC-1", step="Act") is None
    assert store.get(tenant_id="t1", incident_id="INC-1", step="Assess") is not None


def test_the_same_incident_id_under_another_tenant_is_a_separate_key() -> None:
    store = _store()
    store.put(
        tenant_id="t1",
        incident_id="INC-1",
        step="Assess",
        decision={"needs_approval": True},
    )

    assert store.get(tenant_id="t2", incident_id="INC-1", step="Assess") is None

    other = store.put(
        tenant_id="t2",
        incident_id="INC-1",
        step="Assess",
        decision={"needs_approval": False},
    )
    assert other.decision == {"needs_approval": False}


def test_an_unfinished_step_has_no_record() -> None:
    assert _store().get(tenant_id="t1", incident_id="INC-1", step="Assess") is None


def test_result_defaults_to_an_empty_mapping() -> None:
    record = _store().put(tenant_id="t1", incident_id="INC-1", step="Act")

    assert record.result == {}
    assert record.decision is None


def test_identifiers_are_stripped() -> None:
    store = _store()
    record = store.put(tenant_id=" t1 ", incident_id=" INC-1 ", step=" Assess ")

    assert (record.tenant_id, record.incident_id, record.step) == ("t1", "INC-1", "Assess")
    assert store.get(tenant_id="t1", incident_id="INC-1", step="Assess") == record


@pytest.mark.parametrize("field", ["incident_id", "step"])
def test_blank_incident_id_or_step_is_rejected(field: str) -> None:
    store = _store()
    arguments = {"tenant_id": "t1", "incident_id": "INC-1", "step": "Assess", field: "   "}

    with pytest.raises(ValueError, match="non-empty"):
        store.put(**arguments)


def test_blank_tenant_is_rejected() -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        _store().get(tenant_id="   ", incident_id="INC-1", step="Assess")


def test_stored_record_cannot_be_mutated() -> None:
    record = _store().put(tenant_id="t1", incident_id="INC-1", step="Assess")

    with pytest.raises(ValidationError):
        record.step = "Act"
