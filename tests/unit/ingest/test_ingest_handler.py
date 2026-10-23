"""EventBridge ingest validation, debounce, and start-execution contract."""

import threading
from datetime import UTC, datetime
from typing import Any

import pytest

from oerf.platform.incidents.debounce import MemoryDebounce
from oerf.platform.incidents.repository import IncidentRepository
from oerf.platform.ingest.handler import (
    IngestDependencies,
    handler,
)
from oerf.platform.tenancy.config_resolver import get_config

NOW = datetime(2026, 10, 20, 10, 16, tzinfo=UTC)


class Devices:
    def __init__(self, mapping=None):
        self.mapping = {"RF-1": ("t1", "PH-1")} if mapping is None else mapping

    def resolve(self, device_id: str):
        return self.mapping.get(device_id)


class Shipments:
    def __init__(self, found=True):
        self.found = found

    def get(self, tenant_id: str, shipment_id: str):
        if not self.found:
            return None
        return {"shipment_id": shipment_id, "product_profile": "PP-2-8-45"}


class Configs:
    def get(self, tenant_id: str, domain: str):
        return get_config(tenant_id, domain)


class Dedupe:
    def __init__(self):
        self.ids: set[str] = set()

    def register(self, event_id: str, *, now: datetime) -> bool:
        if event_id in self.ids:
            return False
        self.ids.add(event_id)
        return True

    def forget(self, event_id: str) -> None:
        self.ids.discard(event_id)


class Executions:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def start(self, *, name: str, input: dict[str, Any]) -> str:
        self.calls.append({"name": name, "input": input})
        return f"arn:aws:states:us-east-1:123456789012:execution:test:{name}"


class FailingExecutions(Executions):
    def start(self, *, name: str, input: dict[str, Any]) -> str:
        raise RuntimeError("Step Functions unavailable")


def _deps(*, devices=None, shipments=None, executions=None):
    return IngestDependencies(
        devices=devices or Devices(),
        shipments=shipments or Shipments(),
        configs=Configs(),
        debounce=MemoryDebounce(clock=lambda: NOW),
        incidents=IncidentRepository(clock=lambda: NOW),
        dedupe=Dedupe(),
        executions=executions or Executions(),
        state_machine_arn="arn:aws:states:us-east-1:123456789012:stateMachine:test",
        clock=lambda: NOW,
    )


def _event(event_id: str, *, temperature: float = 9, detail: dict[str, Any] | None = None):
    return {
        "id": event_id,
        "source": "sim.reefer",
        "detail-type": "TemperatureReading",
        "detail": {
            "tenant_id": "t1",
            "shipment_id": "PH-1",
            "device_id": "RF-1",
            "ts": "2026-10-20T10:16:00Z",
            "temperature_c": temperature,
            "lat": 17.4,
            "lon": 78.3,
            "seq": 7,
            **(detail or {}),
        },
    }


def test_invalid_envelope_is_ignored_without_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    deps = _deps()

    def unexpected_build() -> IngestDependencies:
        raise AssertionError("unsupported events must not initialize AWS dependencies")

    monkeypatch.setattr("oerf.platform.ingest.handler.build_dependencies", unexpected_build)
    unsupported = handler({"source": "wrong", "detail-type": "TemperatureReading"}, None)
    assert unsupported == {
        "status": "ignored",
        "reason": "unsupported_event",
    }
    malformed = handler(_event("bad", detail={"seq": -1}), None, dependencies=deps)
    assert malformed["reason"] == "invalid_reading"
    assert deps.executions.calls == []


def test_unknown_device_and_claim_mismatch_are_ignored() -> None:
    unknown = _deps(devices=Devices({}))
    assert handler(_event("unknown"), None, dependencies=unknown)["reason"] == "unknown_device"

    mismatch = _deps()
    result = handler(_event("mismatch", detail={"tenant_id": "t2"}), None, dependencies=mismatch)
    assert result["reason"] == "tenant_mismatch"
    assert mismatch.executions.calls == []


def test_first_breach_waits_for_configured_threshold_and_second_starts_once() -> None:
    deps = _deps()

    first = handler(_event("evt-1"), None, dependencies=deps)
    second = handler(_event("evt-2", detail={"seq": 8}), None, dependencies=deps)

    assert first["status"] == "accepted"
    assert first["consecutive_breaches"] == 1
    assert second["status"] == "triggered"
    assert len(deps.executions.calls) == 1
    start_input = deps.executions.calls[0]["input"]
    assert start_input["tenant_id"] == "t1"
    assert start_input["shipment_id"] == "PH-1"
    assert start_input["workflow"]["trigger"]["temperature_c"] == 9
    incident = deps.incidents.get("t1", second["incident_id"])
    assert incident.config_snapshot["tenant_id"] == "t1"


def test_safe_reading_clears_streak_before_breach_restarts_it() -> None:
    deps = _deps()
    handler(_event("hot-1"), None, dependencies=deps)
    safe = handler(_event("safe", temperature=5, detail={"seq": 8}), None, dependencies=deps)
    hot_again = handler(_event("hot-2", detail={"seq": 9}), None, dependencies=deps)

    assert safe["reason"] == "within_range"
    assert hot_again["status"] == "accepted"
    assert hot_again["consecutive_breaches"] == 1
    assert deps.executions.calls == []


def test_duplicate_event_has_no_second_debounce_or_execution() -> None:
    deps = _deps()
    first = handler(_event("same"), None, dependencies=deps)
    second = handler(_event("same"), None, dependencies=deps)

    assert first["status"] == "accepted"
    assert second == {"status": "duplicate", "event_id": first["event_id"]}


def test_second_trigger_for_open_shipment_reuses_incident_and_execution_name() -> None:
    deps = _deps()
    handler(_event("hot-1"), None, dependencies=deps)
    first = handler(_event("hot-2", detail={"seq": 8}), None, dependencies=deps)
    second = handler(_event("hot-3", detail={"seq": 9}), None, dependencies=deps)

    assert first["incident_id"] == second["incident_id"]
    assert deps.executions.calls[0]["name"] == deps.executions.calls[1]["name"]


def test_start_failure_releases_event_dedupe_for_eventbridge_retry() -> None:
    deps = _deps(executions=FailingExecutions())
    handler(_event("hot-1"), None, dependencies=deps)

    with pytest.raises(RuntimeError, match="unavailable"):
        handler(_event("hot-2", detail={"seq": 8}), None, dependencies=deps)

    assert "hot-2" not in deps.dedupe.ids


def test_concurrent_trigger_calls_open_one_incident() -> None:
    deps = _deps()
    handler(_event("hot-1"), None, dependencies=deps)
    barrier = threading.Barrier(2)
    responses: list[dict[str, Any]] = []

    def run(event_id: str, sequence: int) -> None:
        barrier.wait()
        result = handler(_event(event_id, detail={"seq": sequence}), None, dependencies=deps)
        responses.append(result)

    threads = [
        threading.Thread(target=run, args=("hot-2", 8)),
        threading.Thread(target=run, args=("hot-3", 9)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(deps.executions.calls) == 2
    assert len({item["input"]["incident_id"] for item in deps.executions.calls}) == 1
    assert len({item["name"] for item in deps.executions.calls}) == 1


def test_reading_values_are_not_logged(capsys: pytest.CaptureFixture[str]) -> None:
    """Log lines carry identifiers only; untrusted reading values stay out."""
    deps = _deps()

    handler(_event("hot-1"), None, dependencies=deps)
    handler(_event("hot-2", detail={"seq": 8}), None, dependencies=deps)
    text = capsys.readouterr().out

    assert "tenant=t1" in text
    assert "shipment=PH-1" in text
    assert "temperature_c" not in text
    assert "17.4" not in text
    assert "78.3" not in text


def test_reading_is_never_passed_through_as_the_return_value() -> None:
    """The state machine receives the reading; the Lambda result stays small."""
    deps = _deps()
    handler(_event("hot-1"), None, dependencies=deps)
    result = handler(_event("hot-2", detail={"seq": 8}), None, dependencies=deps)

    assert result["status"] == "triggered"
    assert set(result) == {
        "status",
        "event_id",
        "tenant_id",
        "shipment_id",
        "incident_id",
        "execution_arn",
    }
    assert "temperature_c" not in result
