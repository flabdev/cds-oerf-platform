"""Device registry rows that ingest is allowed to trust."""

from oerf.platform.channels.devices import register_device, seed_demo_devices
from oerf.platform.ingest.handler import DynamoDeviceResolver
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
