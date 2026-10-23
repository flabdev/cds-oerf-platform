"""Device registry rows that ingest is allowed to trust."""

from oerf.platform.channels.devices import register_device, seed_demo_devices, seed_handler
from oerf.platform.ingest.handler import DynamoDeviceResolver
from oerf.platform.storage.dynamo import device_pk, key
from tests.unit.support.fake_dynamo import FakeDynamoClient


def test_a_registered_device_resolves_to_its_tenant_and_shipment() -> None:
    client = FakeDynamoClient()
    register_device(
        client,
        "routing",
        device_id="RF-1",
        tenant_id="t1",
        shipment_id="PH-1",
        asset_id="TRK-01",
    )

    resolved = DynamoDeviceResolver(client, "routing").resolve("RF-1")

    assert resolved == ("t1", "PH-1")
    assert DynamoDeviceResolver(client, "routing").resolve("RF-missing") is None


def test_demo_seed_registers_the_shipment_devices() -> None:
    client = FakeDynamoClient()

    written = seed_demo_devices(client, "routing")
    resolver = DynamoDeviceResolver(client, "routing")

    assert "RF-7781" in written
    assert resolver.resolve("RF-7781") == ("t1", "PH-2841")
    assert resolver.resolve("RF-7782") == ("t2", "VX-3101")
    assert resolver.resolve("RF-SBX-0001") == ("sbx", "SBX-0001")
    assert resolver.resolve("RF-missing") is None
    stored = client.get_item(TableName="routing", Key=key(device_pk("RF-7781"), "META"))
    assert stored["Item"]["asset_id"]["S"] == "TRK-01"
    assert stored["Item"]["tenant_id"]["S"] == "t1"
    assert stored["Item"]["shipment_id"]["S"] == "PH-2841"
    assert seed_demo_devices(client, "routing") == written


def test_custom_resource_seeds_on_create_and_is_a_noop_on_delete(monkeypatch) -> None:
    client = FakeDynamoClient()
    monkeypatch.setenv("ROUTING_TABLE_NAME", "routing")
    monkeypatch.setattr("boto3.client", lambda _service: client)

    created = seed_handler({"RequestType": "Create"}, None)
    again = seed_handler({"RequestType": "Update", "PhysicalResourceId": "seed-1"}, None)

    assert created == {"PhysicalResourceId": "oerf-demo-device-seed"}
    assert again == {"PhysicalResourceId": "seed-1"}
    assert DynamoDeviceResolver(client, "routing").resolve("RF-7781") == ("t1", "PH-2841")
    assert seed_handler({"RequestType": "Delete", "PhysicalResourceId": "seed-1"}, None) == {
        "PhysicalResourceId": "seed-1"
    }
    assert DynamoDeviceResolver(client, "routing").resolve("RF-7781") == ("t1", "PH-2841")
