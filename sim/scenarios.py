"""Ordered scenario telemetry. The clock is the caller's; nothing here reads the wall clock."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from sim.temperature import temperature_event

SCENARIO_START = datetime.fromisoformat("2026-10-20T10:06:00+00:00")

REQUIRED = (
    "happy",
    "facility_a_declines",
    "driver_silent",
    "approver_silent_within",
    "approver_silent_outside",
    "keeps_rising",
    "concurrent_duplicate",
    "photo_evidence",
    "reroute_unacked",
    "transfer_unconfirmed",
    "rise_during_qa",
    "false_alarm_cancel",
    "debounce_blip",
)

# These S-tests are not telemetry scenarios. sandbox_join's lifecycle is a later ticket.
EXCLUDED = {
    "sandbox_join": (
        "JOIN creation, the daily cap, and 24-hour expiry belong to OERF-309. "
        "The JOIN reply text is scripted in the responder only."
    ),
    "S11": "Channel fault injection belongs to the router. No telemetry.",
    "S14": "Covered by the cadence calendar test. No telemetry.",
    "S28": "Admin API validation. No telemetry.",
    "S30": "Admin permission check. No telemetry.",
    "S40": "Retention check on stored items. No telemetry.",
    "S41": "Template catalog audit. No telemetry.",
    "S49": "Retention expiry. No telemetry.",
    "S51": "Admin permission check. No telemetry.",
}

_HAPPY = (
    (0, 5.2),
    (2, 5.1),
    (10, 8.1),
    (27, 8.6),
    (30, 8.3),
    (34, 7.9),
    (37, 7.4),
    (39, 6.8),
    (49, 5.0),
    (59, 5.2),
)
_FACILITY_DECLINE = (
    (0, 5.2),
    (2, 5.1),
    (10, 8.1),
    (16, 8.28),
    (40, 9.0),
    (48, 8.04),
    (54, 7.32),
)
_CURVES = {
    "happy": _HAPPY,
    "facility_a_declines": _FACILITY_DECLINE,
    "keeps_rising": ((0, 5.2), (2, 5.1), (10, 8.1), (27, 8.6), (42, 8.9)),
    "rise_during_qa": (*_HAPPY[:8], (44, 8.2), (52, 7.6)),
    "transfer_unconfirmed": ((0, 5.2), (2, 5.1), (10, 8.1), (34, 8.82)),
    "none_feasible": ((0, 5.2), (2, 5.1), (10, 8.1), (27, 8.6), (39, 8.6)),
    "a_no_capacity": (
        (0, 5.2),
        (2, 5.1),
        (10, 8.1),
        (15, 8.25),
        (39, 9.0),
        (47, 8.04),
        (53, 7.32),
    ),
}
_HAPPY_CURVE = {
    "driver_silent",
    "driver_silent_after_voice",
    "driver_silent_late",
    "approver_silent_within",
    "approver_silent_outside",
    "concurrent_duplicate",
    "photo_evidence",
    "reroute_unacked",
    "call_me",
    "call_me_approver",
    "opt_out",
    "opt_out_freetext",
    "concern",
    "misspelled_code",
    "hinglish",
    "voice_note",
    "sandbox_dashboard",
}
_FLAT = {
    "debounce_blip": (10, 5.2, {4.0: 8.2, 4.5: 5.3}),
    "false_alarm_cancel": (10, 5.2, {4.0: 8.4, 4.5: 8.5}),
}


def scenario_events(
    *,
    tenant_id: str,
    shipment_id: str,
    scenario: str,
    device_id: str = "RF-7781",
    start: datetime | None = None,
) -> list[dict[str, object]]:
    """Return PutEvents readings for one named scenario."""

    if scenario in EXCLUDED:
        raise ValueError(EXCLUDED[scenario])
    origin = start or SCENARIO_START
    events: list[dict[str, object]] = []
    for index, (minute, temperature_c) in enumerate(_samples(scenario), start=1):
        events.append(
            temperature_event(
                tenant_id=tenant_id,
                shipment_id=shipment_id,
                device_id=device_id,
                moment=origin + timedelta(minutes=minute),
                temperature_c=temperature_c,
                seq=index,
                lat=min(17.47, round(17.44 + (index - 1) * 0.0002, 6)),
                lon=min(78.42, round(78.38 + (index - 1) * 0.001, 6)),
            )
        )
    if scenario == "concurrent_duplicate":
        return _duplicate_first_hot(events)
    return events


def main(argv: list[str] | None = None) -> int:
    """Write one scenario's events as JSON. This does not publish them."""

    parser = argparse.ArgumentParser(description="Emit a deterministic scenario script")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--shipment", required=True)
    parser.add_argument("--scenario", default="happy")
    parser.add_argument("--device", default="RF-7781")
    parser.add_argument("--emit", choices=("eventbridge",), default="eventbridge")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--start", default=SCENARIO_START.isoformat())
    args = parser.parse_args(argv)
    events = scenario_events(
        tenant_id=args.tenant,
        shipment_id=args.shipment,
        scenario=args.scenario,
        device_id=args.device,
        start=datetime.fromisoformat(args.start),
    )
    payload = json.dumps(events, indent=2) + "\n"
    if args.out is None:
        sys.stdout.write(payload)
    else:
        args.out.write_text(payload, encoding="utf-8")
    return 0


def _samples(scenario: str) -> list[tuple[float, float]]:
    if scenario in _FLAT:
        end, default, overrides = _FLAT[scenario]
        return _flat(end, default, overrides)
    curve = _CURVES.get(scenario)
    if curve is None and scenario in _HAPPY_CURVE:
        curve = _HAPPY
    if curve is None:
        known = ", ".join(sorted({*REQUIRED, *_CURVES, *_HAPPY_CURVE, *_FLAT}))
        raise ValueError(f"unknown scenario {scenario!r}; known scenarios: {known}")
    return _interpolate(curve)


def _interpolate(points: tuple[tuple[float, float], ...]) -> list[tuple[float, float]]:
    samples: list[tuple[float, float]] = []
    end = points[-1][0]
    cursor = 0.0
    while cursor <= end + 1e-9:
        samples.append((cursor, _at(points, cursor)))
        cursor = round(cursor + 0.5, 1)
    return samples


def _at(points: tuple[tuple[float, float], ...], minute: float) -> float:
    for (left_min, left_c), (right_min, right_c) in zip(points, points[1:], strict=True):
        if left_min <= minute <= right_min:
            span = right_min - left_min
            if span == 0:
                return right_c
            if minute == left_min:
                return left_c
            if minute == right_min:
                return right_c
            weight = (minute - left_min) / span
            return round(left_c + (right_c - left_c) * weight, 4)
    return points[-1][1]


def _flat(
    end: float, default: float, overrides: dict[float, float]
) -> list[tuple[float, float]]:
    samples: list[tuple[float, float]] = []
    cursor = 0.0
    while cursor <= end + 1e-9:
        minute = round(cursor, 1)
        samples.append((minute, overrides.get(minute, default)))
        cursor += 0.5
    return samples


def _duplicate_first_hot(events: list[dict[str, object]]) -> list[dict[str, object]]:
    hot = [
        event
        for event in events
        if float(event["Detail"]["temperature_c"]) > 8  # type: ignore[index]
    ][:2]
    duplicated: list[dict[str, object]] = []
    pending = {id(event) for event in hot}
    for event in events:
        duplicated.append(event)
        if id(event) in pending:
            duplicated.append(dict(event))
    return duplicated


if __name__ == "__main__":
    raise SystemExit(main())
