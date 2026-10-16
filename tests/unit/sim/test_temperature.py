"""Tests for the temperature-reading simulator."""

import json

from sim.temperature import generate_temperature_events, main


def test_events_use_putevents_shape() -> None:
    events = generate_temperature_events(
        tenant_id="t1",
        shipment_id="PH-2841",
        scenario="excursion",
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
    )
    temperatures = [event["Detail"]["temperature_c"] for event in events]

    assert max(temperatures) > 8
    assert temperatures[-1] == 8.1


def test_normal_stays_near_set_point() -> None:
    events = generate_temperature_events(tenant_id="t1", shipment_id="PH-2841", scenario="normal")
    temperatures = [event["Detail"]["temperature_c"] for event in events]

    assert temperatures
    assert set(temperatures) == {5.2}


def test_recovery_returns_through_the_upper_limit() -> None:
    events = generate_temperature_events(tenant_id="t1", shipment_id="PH-2841", scenario="recovery")
    temperatures = [event["Detail"]["temperature_c"] for event in events]

    assert temperatures[0] > 8
    assert temperatures[-1] < 8


def test_generation_is_deterministic() -> None:
    kwargs = {"tenant_id": "t1", "shipment_id": "PH-2841", "scenario": "excursion"}

    assert generate_temperature_events(**kwargs) == generate_temperature_events(**kwargs)


def test_cli_prints_json(capsys) -> None:
    exit_code = main(
        ["--tenant", "t1", "--shipment", "PH-2841", "--scenario", "normal", "--dry-run"]
    )

    printed = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert printed[0]["Detail"]["shipment_id"] == "PH-2841"
