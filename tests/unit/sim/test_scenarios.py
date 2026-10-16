"""Scenario telemetry and the scripted responder."""

import json
from datetime import datetime
from pathlib import Path

from sim.responder import reply, reply_script
from sim.scenarios import EXCLUDED, REQUIRED, SCENARIO_START, main, scenario_events
from sim.temperature import generate_temperature_events


def test_happy_key_points_are_inside_detail() -> None:
    events = scenario_events(tenant_id="t1", shipment_id="PH-2841", scenario="happy")
    by_time = {event["Time"]: event["Detail"]["temperature_c"] for event in events}

    assert by_time["2026-10-20T10:16:00Z"] == 8.1
    assert by_time["2026-10-20T10:33:00Z"] == 8.6
    assert by_time["2026-10-20T10:45:00Z"] == 6.8
    assert events[0]["Source"] == "sim.reefer"
    assert events[0]["Detail"]["tenant_id"] == "t1"
    assert events[0]["Detail"]["shipment_id"] == "PH-2841"
    assert "tenant_id" not in events[0]


def test_debounce_blip_has_one_hot_reading() -> None:
    events = scenario_events(tenant_id="t1", shipment_id="PH-2841", scenario="debounce_blip")
    hot = [event for event in events if event["Detail"]["temperature_c"] > 8]

    assert len(hot) == 1
    assert hot[0]["Time"] == "2026-10-20T10:10:00Z"
    assert hot[0]["Detail"]["temperature_c"] == 8.2


def test_duplicate_readings_repeat_the_first_two_hot_sequences() -> None:
    events = scenario_events(
        tenant_id="t1", shipment_id="PH-2841", scenario="concurrent_duplicate"
    )
    sequences = [event["Detail"]["seq"] for event in events]
    repeated = [seq for seq in sequences if sequences.count(seq) == 2]

    assert len(repeated) == 4
    assert len(set(repeated)) == 2


def test_required_scenarios_are_scripted_or_explicitly_excluded() -> None:
    for name in REQUIRED:
        assert name not in EXCLUDED
        events = scenario_events(tenant_id="t1", shipment_id="PH-2841", scenario=name)
        assert events[0]["Time"] == "2026-10-20T10:06:00Z"
        assert reply_script(name) is not None

    assert "OERF-309" in EXCLUDED["sandbox_join"]


def test_happy_reply_order_and_stable_duplicate() -> None:
    script = reply_script("happy")

    assert [item.body for item in script[:3]] == [
        "BTN_RUNNING",
        "E17 compressor fault",
        "BTN_APPROVE",
    ]
    first = reply("happy", "ops_manager", "ops_approval", 1)
    assert first == reply("happy", "ops_manager", "ops_approval", 1)
    assert first is not None
    assert first.provider_msg_id == "sim-happy-ops_manager-ops_approval-1"


def test_responder_styles() -> None:
    assert reply("driver_silent", "driver", "driver_checkin", 1) is None
    assert reply("facility_a_declines", "facility", "facility_request", 1).body == "BTN_DECLINE"
    assert reply("call_me", "driver", "driver_checkin", 1).intent == "call-me"
    assert reply("concern", "driver", "driver_reroute", 1).intent == "concern"
    assert "e-17" in reply("misspelled_code", "driver", "driver_checkin", 1).body
    assert reply("hinglish", "driver", "driver_checkin", 1).body.startswith("bhai unit")
    assert reply("opt_out", "facility", "facility_request", 1).body == "STOP"
    assert reply("opt_out_freetext", "facility", "facility_request", 1).intent == "unrelated"


def test_cli_writes_eventbridge_json(tmp_path: Path) -> None:
    destination = tmp_path / "events.json"

    exit_code = main(
        [
            "--tenant",
            "t1",
            "--shipment",
            "PH-2841",
            "--scenario",
            "happy",
            "--emit",
            "eventbridge",
            "--out",
            str(destination),
            "--start",
            SCENARIO_START.isoformat(),
        ]
    )

    written = json.loads(destination.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert written[0]["DetailType"] == "TemperatureReading"
    assert written[0]["EventBusName"] == "oerf-events"


def test_generators_do_not_read_the_wall_clock() -> None:
    sources = [
        Path(generate_temperature_events.__code__.co_filename),
        Path(scenario_events.__code__.co_filename),
        Path(reply.__code__.co_filename),
    ]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        assert "datetime.now" not in text


def test_explicit_start_is_used() -> None:
    start = datetime.fromisoformat("2026-10-21T10:06:00+00:00")
    events = scenario_events(
        tenant_id="t1",
        shipment_id="PH-2841",
        scenario="debounce_blip",
        start=start,
    )

    assert events[0]["Time"] == "2026-10-21T10:06:00Z"
