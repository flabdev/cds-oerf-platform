"""Incident ids are unique per tenant across processes, and execution names follow."""

from datetime import UTC, datetime

import pytest

from oerf.platform.inbound.handler import _execution_arn
from oerf.platform.incidents.dynamo import DynamoIncidentStore
from oerf.platform.incidents.repository import IncidentRepository
from oerf.platform.ingest.handler import _execution_name
from tests.unit.support.fake_dynamo import FakeDynamoClient

NOW = datetime(2026, 10, 20, 10, 16, tzinfo=UTC)
MACHINE = "arn:aws:states:us-east-1:000000000000:stateMachine:oerf-cold-chain"


def _store(client: FakeDynamoClient) -> DynamoIncidentStore:
    return DynamoIncidentStore(client, "incident", tenant_table="tenant", clock=lambda: NOW)


@pytest.mark.security
def test_two_store_instances_do_not_reuse_an_id() -> None:
    client = FakeDynamoClient()
    first = _store(client).create(
        tenant_id="t1", shipment_id="PH-1", reason="TEMPERATURE_ABOVE_MAX"
    )
    second = _store(client).create(
        tenant_id="t1", shipment_id="PH-2", reason="TEMPERATURE_ABOVE_MAX"
    )

    assert first.incident_id == "INC-000001"
    assert second.incident_id == "INC-000002"
    assert first.incident_id != second.incident_id


@pytest.mark.security
def test_a_cold_start_continues_the_tenant_counter() -> None:
    client = FakeDynamoClient()
    store = _store(client)
    for shipment in ("PH-1", "PH-2", "PH-3"):
        store.create(tenant_id="t1", shipment_id=shipment, reason="TEMPERATURE_ABOVE_MAX")

    restarted = _store(client).create(
        tenant_id="t1", shipment_id="PH-4", reason="TEMPERATURE_ABOVE_MAX"
    )

    assert restarted.incident_id == "INC-000004"


@pytest.mark.security
def test_two_tenants_start_at_one_and_their_execution_names_differ() -> None:
    client = FakeDynamoClient()
    store = _store(client)
    first = store.create(tenant_id="t1", shipment_id="PH-1", reason="TEMPERATURE_ABOVE_MAX")
    second = store.create(tenant_id="t2", shipment_id="PH-9", reason="TEMPERATURE_ABOVE_MAX")

    assert first.incident_id == "INC-000001"
    assert second.incident_id == "INC-000001"
    left = _execution_name("t1", first.incident_id)
    right = _execution_name("t2", second.incident_id)
    assert left != right
    assert _execution_arn(MACHINE, "t1", first.incident_id).endswith(left)
    assert not _execution_arn(MACHINE, "t1", first.incident_id).endswith(right)

    memory = IncidentRepository(clock=lambda: NOW)
    left = memory.create(tenant_id="t1", shipment_id="PH-1", reason="TEMPERATURE_ABOVE_MAX")
    right = memory.create(tenant_id="t2", shipment_id="PH-9", reason="TEMPERATURE_ABOVE_MAX")
    assert left.incident_id == "INC-000001"
    assert right.incident_id == "INC-000001"


def test_a_long_execution_name_stays_inside_the_step_functions_limit() -> None:
    tenant = "t" * 40
    incident = "i" * 50
    name = _execution_name(tenant, incident)

    assert len(name) <= 80
    assert name.replace("-", "").isalnum()
    assert name != _execution_name("other", incident)
