"""DynamoDB incident and lock adapters, exercised with a fake client."""

import threading
from datetime import UTC, datetime

import pytest

from oerf.platform.inbound.dedupe import InboundDedupe, MessageDedupePort
from oerf.platform.inbound.dynamo import DynamoMessageDedupe
from oerf.platform.incidents.dynamo import DynamoIncidentStore, DynamoOpenIncidentLock
from oerf.platform.incidents.lock import OpenIncidentLock, OpenIncidentLockPort
from oerf.platform.incidents.repository import (
    IllegalTransition,
    IncidentNotFound,
    IncidentRepository,
    IncidentStore,
    StaleIncidentUpdate,
)
from tests.unit.support.fake_dynamo import FakeDynamoClient


def test_memory_and_dynamo_adapters_match_the_ports() -> None:
    client = FakeDynamoClient()
    assert isinstance(IncidentRepository(), IncidentStore)
    assert isinstance(DynamoIncidentStore(client, "incident"), IncidentStore)
    assert isinstance(OpenIncidentLock(), OpenIncidentLockPort)
    assert isinstance(DynamoOpenIncidentLock(client, "incident"), OpenIncidentLockPort)
    assert isinstance(InboundDedupe(), MessageDedupePort)
    assert isinstance(DynamoMessageDedupe(client, "routing"), MessageDedupePort)


def test_create_is_one_transaction_and_keeps_tenant() -> None:
    client = FakeDynamoClient()
    store = DynamoIncidentStore(client, "incident", clock=_clock)

    created = store.create(tenant_id="t1", shipment_id="PH-2841", reason="TEMPERATURE_ABOVE_MAX")

    assert created.incident_id == "INC-000001"
    assert created.tenant_id == "t1"
    assert created.pk == "TENANT#t1#INC#INC-000001"
    assert client.transactions == [["Put", "Put", "Put"]]
    assert store.lock.get_lock("t1", "PH-2841") == {
        "pk": "TENANT#t1#SHIP#PH-2841",
        "sk": "OPEN_INCIDENT",
        "incident_id": "INC-000001",
        "tenant_id": "t1",
    }
    events = store.events("t1", created.incident_id)
    assert [event.sk for event in events] == ["EVT#2026-10-20T10:16:00Z#0001"]
    assert events[0].state == "OPEN"
    loaded = client.get_item(
        TableName="incident",
        Key={"pk": {"S": created.pk}, "sk": {"S": events[0].sk}},
    )
    assert loaded["Item"]["tenant_id"]["S"] == "t1"


def test_second_create_appends_without_overwriting() -> None:
    client = FakeDynamoClient()
    store = DynamoIncidentStore(client, "incident", clock=_clock)
    first = store.create(tenant_id="t1", shipment_id="PH-2841", reason="TEMPERATURE_ABOVE_MAX")

    second = store.create(
        tenant_id="t1",
        shipment_id="PH-2841",
        reason="TEMPERATURE_STILL_ABOVE_MAX",
    )

    assert second.incident_id == first.incident_id
    assert second.reason == "TEMPERATURE_ABOVE_MAX"
    assert second.opened_at == first.opened_at
    events = store.events("t1", first.incident_id)
    assert [event.sk[-4:] for event in events] == ["0001", "0002"]
    assert events[1].reason == "TEMPERATURE_STILL_ABOVE_MAX"
    assert events[1].state == "OPEN"


def test_concurrent_creates_open_one_incident() -> None:
    store = DynamoIncidentStore(FakeDynamoClient(), "incident", clock=_clock)
    barrier = threading.Barrier(2)
    created: list[str] = []

    def attempt() -> None:
        barrier.wait()
        created.append(
            store.create(
                tenant_id="t1",
                shipment_id="PH-2841",
                reason="TEMPERATURE_ABOVE_MAX",
            ).incident_id
        )

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert created == ["INC-000001", "INC-000001"]
    assert [event.sk[-4:] for event in store.events("t1", "INC-000001")] == ["0001", "0002"]


def test_transition_appends_the_next_sequence() -> None:
    store = DynamoIncidentStore(FakeDynamoClient(), "incident", clock=_clock)
    incident = store.create(tenant_id="t1", shipment_id="PH-2841", reason="TEMPERATURE_ABOVE_MAX")

    result = store.transition("t1", incident.incident_id, from_state="OPEN", to_state="DIAGNOSING")

    assert result.state == "DIAGNOSING"
    assert [event.sk[-4:] for event in store.events("t1", incident.incident_id)] == [
        "0001",
        "0002",
    ]


def test_illegal_transition_does_not_write() -> None:
    client = FakeDynamoClient()
    store = DynamoIncidentStore(client, "incident", clock=_clock)
    incident = store.create(tenant_id="t1", shipment_id="PH-2841", reason="TEMPERATURE_ABOVE_MAX")
    writes = client.writes

    with pytest.raises(IllegalTransition):
        store.transition("t1", incident.incident_id, from_state="OPEN", to_state="CLOSED")

    assert client.writes == writes
    assert store.get("t1", incident.incident_id).state == "OPEN"


def test_stale_transition_does_not_overwrite() -> None:
    store = DynamoIncidentStore(FakeDynamoClient(), "incident", clock=_clock)
    incident = store.create(tenant_id="t1", shipment_id="PH-2841", reason="TEMPERATURE_ABOVE_MAX")
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def attempt() -> None:
        barrier.wait()
        try:
            outcomes.append(
                store.transition(
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

    assert sum(isinstance(item, StaleIncidentUpdate) for item in outcomes) == 1
    assert store.get("t1", incident.incident_id).state == "DIAGNOSING"


def test_missing_incident_is_rejected() -> None:
    store = DynamoIncidentStore(FakeDynamoClient(), "incident", clock=_clock)

    with pytest.raises(IncidentNotFound):
        store.transition("t1", "INC-000099", from_state="OPEN", to_state="DIAGNOSING")


def test_cancel_removes_the_lock_in_the_same_transaction() -> None:
    client = FakeDynamoClient()
    store = DynamoIncidentStore(client, "incident", clock=_clock)
    incident = store.create(tenant_id="t1", shipment_id="PH-2841", reason="TEMPERATURE_ABOVE_MAX")

    store.transition("t1", incident.incident_id, from_state="OPEN", to_state="CANCELLED")

    assert store.get("t1", incident.incident_id).state == "CANCELLED"
    assert store.lock.get_lock("t1", "PH-2841") is None
    assert client.transactions[-1] == ["Update", "Put", "Delete"]


def _clock() -> datetime:
    return datetime(2026, 10, 20, 10, 16, tzinfo=UTC)
