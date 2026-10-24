"""EventBridge ingest validation, debounce, and start-execution contract."""

import threading
from datetime import UTC, datetime
from typing import Any

import pytest

from oerf.platform.incidents.debounce import MemoryDebounce
from oerf.platform.incidents.repository import IncidentRepository
from oerf.platform.ingest.handler import (
    READING_RETENTION_SECONDS,
    DynamoReadingStore,
    IngestDependencies,
    handler,
)
from oerf.platform.report.generator import handler as report_handler
from oerf.platform.report.store import MemoryReportStore
from oerf.platform.storage.dynamo import client_error
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


class Readings:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._keys: set[tuple[str, str]] = set()

    def put(
        self,
        *,
        pk: str,
        sk: str,
        ts: str,
        temperature_c: float,
        expires_at: int,
    ) -> bool:
        if (pk, sk) in self._keys:
            return False
        self._keys.add((pk, sk))
        self.rows.append(
            {
                "pk": pk,
                "sk": sk,
                "ts": ts,
                "temperature_c": temperature_c,
                "expires_at": expires_at,
            }
        )
        return True


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
        readings=Readings(),
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
    readings = deps.incidents.readings("t1", incident.incident_id)
    assert len(readings) == 1
    assert readings[0]["temperature_c"] == 9
    assert readings[0]["device_id"] == "RF-1"


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


def test_open_incident_stores_one_reading_and_a_retry_does_not_duplicate() -> None:
    deps = _deps()
    waiting = handler(_event("hot-1"), None, dependencies=deps)
    assert waiting["status"] == "accepted"
    assert deps.readings.rows == []
    opened = handler(
        _event("hot-2", temperature=12, detail={"seq": 8, "ts": "2026-10-20T10:20:00Z"}),
        None,
        dependencies=deps,
    )

    assert opened["status"] == "triggered"
    row = deps.readings.rows[0]
    incident_id = opened["incident_id"]
    assert row == {
        "pk": f"TENANT#t1#INC#{incident_id}",
        "sk": "READ#2026-10-20T10:20:00Z#000008",
        "ts": "2026-10-20T10:20:00Z",
        "temperature_c": 12.0,
        "expires_at": int(datetime(2026, 10, 20, 10, 20, tzinfo=UTC).timestamp())
        + READING_RETENTION_SECONDS,
    }
    assert "lat" not in row
    assert "device_id" not in row

    replay = handler(
        _event("hot-2", temperature=12, detail={"seq": 8, "ts": "2026-10-20T10:20:00Z"}),
        None,
        dependencies=deps,
    )
    assert replay["status"] == "duplicate"
    deps.dedupe.forget(opened["event_id"])
    retried = handler(
        _event("hot-2", temperature=12, detail={"seq": 8, "ts": "2026-10-20T10:20:00Z"}),
        None,
        dependencies=deps,
    )
    assert retried["status"] == "triggered"
    assert deps.readings.rows == [row]


def test_conditional_put_keeps_a_single_reading_item() -> None:
    client = _ConditionalClient()
    store = DynamoReadingStore(client, "incidents")
    item = {
        "pk": "TENANT#t1#INC#INC-1",
        "sk": "READ#2026-10-20T10:20:00Z#000008",
        "ts": "2026-10-20T10:20:00Z",
        "temperature_c": 12.0,
        "expires_at": 1,
    }

    assert store.put(**item) is True
    assert store.put(**item) is False

    stored = next(iter(client.items.values()))
    assert set(stored) == {"pk", "sk", "ts", "temperature_c", "expires_at"}
    assert stored["temperature_c"] == {"N": "12"}
    assert client.calls[0]["ConditionExpression"] == "attribute_not_exists(#pk)"


def test_report_temperature_section_uses_stored_readings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps = _deps()
    handler(_event("hot-1"), None, dependencies=deps)
    opened = handler(
        _event("hot-2", temperature=12, detail={"seq": 8, "ts": "2026-10-20T10:20:00Z"}),
        None,
        dependencies=deps,
    )
    handler(
        _event("safe", temperature=5, detail={"seq": 9, "ts": "2026-10-20T10:26:00Z"}),
        None,
        dependencies=deps,
    )
    handler(
        _event("later", temperature=9.5, detail={"seq": 10, "ts": "2026-10-20T10:31:00Z"}),
        None,
        dependencies=deps,
    )
    incident = deps.incidents.get("t1", opened["incident_id"])
    store = MemoryReportStore()
    table = _ReportTable(incident, deps.readings.rows)
    monkeypatch.setattr("oerf.platform.report.generator._store", lambda: store)
    monkeypatch.setattr("oerf.platform.report.generator.INCIDENT_TABLE_NAME", "incidents")
    monkeypatch.setattr("oerf.platform.report.generator.get_dynamodb", lambda: table)

    result = report_handler({"tenant_id": "t1", "incident_id": incident.incident_id}, None)
    html = store.objects[result["report_key"]].decode()
    section = html.split('id="temperature_timeline"', 1)[1].split('id="state_timeline"', 1)[0]
    points = [
        (datetime.fromisoformat(stamp.replace("Z", "+00:00")), float(temperature))
        for stamp, temperature, _unit in (line.split() for line in _list_items(section))
    ]
    limit = get_config("t1", "cold_chain_excursion").product_profile("PP-2-8-45").range_c[1]
    minutes = sum(
        (end - start).total_seconds() / 60
        for (start, temperature), (end, _following) in zip(points, points[1:])
        if temperature > limit
    )

    assert limit == 8
    assert max(temperature for _stamp, temperature in points) == 12.0
    assert minutes == 6
    assert "17.4" not in section
    assert "RF-1" not in section


def _list_items(section: str) -> list[str]:
    return [item.split("</li>", 1)[0] for item in section.split("<li>")[1:]]


class _ReportTable:
    def __init__(self, incident: Any, readings: list[dict[str, Any]]) -> None:
        self._incident = incident
        self._readings = readings

    def Table(self, _name: str) -> "_ReportTable":
        return self

    def get_item(self, Key: dict[str, str]) -> dict[str, Any]:
        if Key == {"pk": self._incident.pk, "sk": "META"}:
            return {
                "Item": {
                    "pk": self._incident.pk,
                    "sk": "META",
                    "tenant_id": self._incident.tenant_id,
                    "incident_id": self._incident.incident_id,
                    "shipment_id": self._incident.shipment_id,
                    "state": self._incident.state,
                }
            }
        return {}

    def query(self, **kwargs: object) -> dict[str, Any]:
        values = kwargs.get("ExpressionAttributeValues")
        prefix = values.get(":prefix") if isinstance(values, dict) else ""
        if prefix == "READ#":
            return {"Items": [dict(row) for row in self._readings]}
        return {"Items": []}


class _ConditionalClient:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []

    def put_item(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        item = kwargs["Item"]
        identity = (item["pk"]["S"], item["sk"]["S"])
        if identity in self.items:
            raise client_error("ConditionalCheckFailedException")
        self.items[identity] = item
