"""Tests for the temperature-reading simulator."""

import json
from datetime import UTC, datetime

import pytest

from sim.temperature import generate_temperature_events, main, temperature_event

BUS = "oerf-events"


def test_events_use_putevents_shape() -> None:
    events = generate_temperature_events(
        tenant_id="t1",
        shipment_id="PH-2841",
        scenario="excursion",
        event_bus=BUS,
    )

    event = events[0]
    assert event["Source"] == "sim.reefer"
    assert event["DetailType"] == "TemperatureReading"
    assert event["EventBusName"] == "oerf-events"
    assert event["Time"] == event["Detail"]["ts"]
    detail = event["Detail"]
    assert detail["tenant_id"] == "t1"
    assert detail["shipment_id"] == "PH-2841"
    assert detail["device_id"] == "RF-7781"
    assert detail["seq"] == 1
    assert "tenant_id" not in {key for key in event if key != "Detail"}


def test_excursion_rises_above_the_safe_maximum() -> None:
    events = generate_temperature_events(
        tenant_id="t1",
        shipment_id="PH-2841",
        scenario="excursion",
        event_bus=BUS,
    )
    temperatures = [event["Detail"]["temperature_c"] for event in events]

    assert max(temperatures) > 8
    assert temperatures[-1] == 8.1


def test_normal_stays_near_set_point() -> None:
    events = generate_temperature_events(
        tenant_id="t1", shipment_id="PH-2841", scenario="normal", event_bus=BUS
    )
    temperatures = [event["Detail"]["temperature_c"] for event in events]

    assert temperatures
    assert set(temperatures) == {5.2}


def test_recovery_returns_through_the_upper_limit() -> None:
    events = generate_temperature_events(
        tenant_id="t1", shipment_id="PH-2841", scenario="recovery", event_bus=BUS
    )
    temperatures = [event["Detail"]["temperature_c"] for event in events]

    assert temperatures[0] > 8
    assert temperatures[-1] < 8


def test_generation_is_deterministic() -> None:
    kwargs = {
        "tenant_id": "t1",
        "shipment_id": "PH-2841",
        "scenario": "excursion",
        "event_bus": BUS,
    }

    assert generate_temperature_events(**kwargs) == generate_temperature_events(**kwargs)


def test_cli_prints_json(capsys, monkeypatch) -> None:
    monkeypatch.setenv("ALLOW_MAIN", "1")
    exit_code = main(
        [
            "--tenant",
            "t1",
            "--shipment",
            "PH-2841",
            "--scenario",
            "normal",
            "--dry-run",
            "--stage",
            "main",
        ]
    )

    captured = capsys.readouterr()
    printed = json.loads(captured.out)
    assert exit_code == 0
    assert "stage=main" in captured.err
    assert "bus=oerf-events" in captured.err
    assert printed[0]["Detail"]["shipment_id"] == "PH-2841"
    assert printed[0]["EventBusName"] == "oerf-events"


def test_reading_without_a_bus_or_stage_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STAGE", raising=False)
    monkeypatch.delenv("EVENT_BUS_NAME", raising=False)
    moment = datetime(2026, 10, 20, 10, 8, tzinfo=UTC)
    fields = {
        "tenant_id": "t1",
        "shipment_id": "PH-2841",
        "device_id": "RF-7781",
        "moment": moment,
        "temperature_c": 5.2,
        "seq": 1,
        "lat": 17.44,
        "lon": 78.38,
    }
    with pytest.raises(ValueError, match="stage or event_bus is required"):
        temperature_event(**fields)
    with pytest.raises(ValueError, match="stage or event_bus is required"):
        generate_temperature_events(tenant_id="t1", shipment_id="PH-2841", scenario="normal")

    monkeypatch.setenv("STAGE", "dev")
    event = temperature_event(**fields)
    assert event["EventBusName"] == "oerf-dev-events"
    monkeypatch.setenv("EVENT_BUS_NAME", "oerf-events")
    assert temperature_event(**fields)["EventBusName"] == "oerf-dev-events"
