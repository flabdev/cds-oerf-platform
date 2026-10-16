"""Deterministic cold-chain temperature readings.

Events use the EventBridge PutEvents field names. Tenant and shipment live in
``Detail``. This module prints JSON. It does not call PutEvents.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta

SCENARIOS = ("normal", "excursion", "recovery")
READING_INTERVAL = timedelta(seconds=30)
EXCURSION_CURVE = (
    (0, 5.1),
    (2, 5.8),
    (4, 6.6),
    (6, 7.4),
    (8, 8.1),
)


def generate_temperature_events(
    *,
    tenant_id: str,
    shipment_id: str,
    scenario: str,
    device_id: str = "RF-7781",
    start: datetime | None = None,
) -> list[dict[str, object]]:
    """Return a deterministic series for ``normal``, ``excursion``, or ``recovery``."""

    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}")
    origin = start or _default_start(scenario)
    samples = _samples(scenario)
    events: list[dict[str, object]] = []
    for index, (offset_min, temperature_c) in enumerate(samples, start=1):
        moment = origin + timedelta(minutes=offset_min)
        events.append(
            temperature_event(
                tenant_id=tenant_id,
                shipment_id=shipment_id,
                device_id=device_id,
                moment=moment,
                temperature_c=temperature_c,
                seq=index,
                lat=17.44,
                lon=round(78.38 + (index - 1) * 0.001, 6),
            )
        )
    return events


def main(argv: list[str] | None = None) -> int:
    """Print generated readings as JSON."""

    parser = argparse.ArgumentParser(description="Generate cold-chain temperature readings")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--shipment", required=True)
    parser.add_argument("--scenario", choices=SCENARIOS, default="excursion")
    parser.add_argument("--device", default="RF-7781")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    events = generate_temperature_events(
        tenant_id=args.tenant,
        shipment_id=args.shipment,
        scenario=args.scenario,
        device_id=args.device,
    )
    json.dump(events, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def temperature_event(
    *,
    tenant_id: str,
    shipment_id: str,
    device_id: str,
    moment: datetime,
    temperature_c: float,
    seq: int,
    lat: float,
    lon: float,
) -> dict[str, object]:
    """One PutEvents record. ``Detail`` is an object so tests can read the fields."""

    stamp = _iso_z(moment)
    return {
        "Source": "sim.reefer",
        "DetailType": "TemperatureReading",
        "EventBusName": "oerf-events",
        "Time": stamp,
        "Detail": {
            "tenant_id": tenant_id,
            "shipment_id": shipment_id,
            "device_id": device_id,
            "ts": stamp,
            "temperature_c": temperature_c,
            "lat": lat,
            "lon": lon,
            "seq": seq,
        },
    }


def _samples(scenario: str) -> list[tuple[float, float]]:
    if scenario == "normal":
        return [(index * 0.5, 5.2) for index in range(8)]
    if scenario == "excursion":
        return _curve(EXCURSION_CURVE)
    return _line(start_c=8.6, end_c=5.0, duration_min=10)


def _curve(points: tuple[tuple[float, float], ...]) -> list[tuple[float, float]]:
    samples: list[tuple[float, float]] = []
    end = points[-1][0]
    step = 0.5
    cursor = 0.0
    while cursor < end + 1e-9:
        samples.append((cursor, _interpolate(points, cursor)))
        cursor += step
    return samples


def _line(*, start_c: float, end_c: float, duration_min: float) -> list[tuple[float, float]]:
    count = int(duration_min / 0.5)
    return [
        (index * 0.5, start_c + (end_c - start_c) * index / count)
        for index in range(count + 1)
    ]


def _interpolate(points: tuple[tuple[float, float], ...], minute: float) -> float:
    for (left_min, left_c), (right_min, right_c) in zip(points, points[1:], strict=True):
        if left_min <= minute <= right_min:
            span = right_min - left_min
            weight = (minute - left_min) / span
            return left_c + (right_c - left_c) * weight
    return points[-1][1]


def _default_start(scenario: str) -> datetime:
    if scenario == "normal":
        return datetime(2026, 10, 20, 10, 6, tzinfo=UTC)
    if scenario == "excursion":
        return datetime(2026, 10, 20, 10, 8, tzinfo=UTC)
    return datetime(2026, 10, 20, 10, 33, tzinfo=UTC)


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
