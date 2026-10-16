"""Debounce streaks gate the cold-chain trigger."""

from datetime import UTC, datetime, timedelta

import pytest

from oerf.platform.incidents.debounce import TTL, DynamoDebounce, MemoryDebounce, observe
from oerf.platform.rules.trigger import TemperatureEvent, evaluate_trigger
from oerf.platform.tenancy import get_config
from tests.unit.support.fake_dynamo import FakeDynamoClient


@pytest.mark.parametrize("kind", ["memory", "dynamo"])
def test_two_breaches_fire_and_a_safe_reading_clears(kind: str) -> None:
    now = {"at": datetime(2026, 10, 20, 10, 0, tzinfo=UTC)}
    store = _store(kind, lambda: now["at"])
    config = get_config("t1", "cold_chain_excursion")
    reading = TemperatureEvent(
        tenant_id="t1",
        shipment_id="PH-2841",
        temperature_c=8.1,
        timestamp="2026-10-20T10:16:00Z",
    )

    first = store.record_breach(tenant_id="t1", shipment_id="PH-2841", device_id="RF-7781")
    assert evaluate_trigger(
        reading,
        config,
        profile_id="PP-2-8-45",
        consecutive_breaches=first,
    ).triggered is False

    second = store.record_breach(tenant_id="t1", shipment_id="PH-2841", device_id="RF-7781")
    assert second == config.trigger.debounce_readings
    assert evaluate_trigger(
        reading,
        config,
        profile_id="PP-2-8-45",
        consecutive_breaches=second,
    ).triggered is True

    cleared = observe(
        store,
        tenant_id="t1",
        shipment_id="PH-2841",
        device_id="RF-7781",
        in_breach=False,
    )
    assert cleared == 0
    again = store.record_breach(tenant_id="t1", shipment_id="PH-2841", device_id="RF-7781")
    assert again == 1
    assert evaluate_trigger(
        reading,
        config,
        profile_id="PP-2-8-45",
        consecutive_breaches=again,
    ).triggered is False


@pytest.mark.parametrize("kind", ["memory", "dynamo"])
def test_streak_is_scoped_and_expires(kind: str) -> None:
    now = {"at": datetime(2026, 10, 20, 10, 0, tzinfo=UTC)}
    store = _store(kind, lambda: now["at"])

    assert store.record_breach(tenant_id="t1", shipment_id="PH-1", device_id="RF-1") == 1
    assert store.record_breach(tenant_id="t1", shipment_id="PH-2", device_id="RF-1") == 1
    assert store.record_breach(tenant_id="t2", shipment_id="PH-1", device_id="RF-1") == 1
    assert store.record_breach(tenant_id="t1", shipment_id="PH-1", device_id="RF-1") == 2

    now["at"] = now["at"] + TTL
    assert store.record_breach(tenant_id="t1", shipment_id="PH-1", device_id="RF-1") == 1
    assert TTL == timedelta(minutes=10)


def _store(kind: str, clock):
    if kind == "memory":
        return MemoryDebounce(clock=clock)
    return DynamoDebounce(FakeDynamoClient(), "platform", clock=clock)
