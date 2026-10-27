"""EventBridge ingest validation, debounce, and start-execution contract."""

import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from domains.cold_chain_excursion.steps.workflow_stub import handler as workflow_stub
from oerf.platform.agent.context import StepDependencies
from oerf.platform.agent.envelope import StepEnvelope, parse_envelope
from oerf.platform.agent.handlers import gather_context
from oerf.platform.agent.idempotency import MemoryStepResultStore
from oerf.platform.incidents.debounce import MemoryDebounce
from oerf.platform.incidents.repository import IncidentRepository
from oerf.platform.ingest.handler import IngestDependencies, handler
from oerf.platform.report.generator import handler as report_handler
from oerf.platform.report.store import MemoryReportStore
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
    assert "cancel_requested" not in start_input
    assert start_input["workflow"]["trigger"]["temperature_c"] == 9
    incident = deps.incidents.get("t1", second["incident_id"])
    assert incident.config_snapshot["tenant_id"] == "t1"
    readings = deps.incidents.readings("t1", incident.incident_id)
    assert len(readings) == 1
    assert readings[0]["temperature_c"] == 9
    assert set(readings[0]) == {
        "pk",
        "sk",
        "tenant_id",
        "ts",
        "temperature_c",
        "expires_at",
    }


def test_ph2841_seed_profile_reaches_gather_context() -> None:
    seed_path = (
        Path(__file__).resolve().parents[3] / "sim" / "seed" / "t1" / "shipments.json"
    )
    row = next(
        item
        for item in json.loads(seed_path.read_text(encoding="utf-8"))
        if item["shipment_id"] == "PH-2841"
    )
    assert row["product_profile"] == "PP-2-8-45"
    assert "product_profile_id" not in row

    class SeedShipments:
        def get(self, tenant_id: str, shipment_id: str):
            assert tenant_id == "t1"
            assert shipment_id == "PH-2841"
            return row

    deps = _deps(
        devices=Devices({"RF-7781": ("t1", "PH-2841")}),
        shipments=SeedShipments(),
    )
    detail = {"device_id": "RF-7781", "shipment_id": "PH-2841"}
    handler(_event("hot-1", detail=detail), None, dependencies=deps)
    started = handler(
        _event("hot-2", detail={**detail, "seq": 8}),
        None,
        dependencies=deps,
    )

    start_input = deps.executions.calls[0]["input"]
    shipment = start_input["workflow"]["shipment"]
    assert shipment["product_profile"] == "PP-2-8-45"
    assert shipment["product_profile_id"] == "PP-2-8-45"
    assert row.get("product_profile_id") is None
    gathered = gather_context(
        StepEnvelope(
            step="GatherContext",
            tenant_id=start_input["tenant_id"],
            incident_id=started["incident_id"],
            shipment_id="PH-2841",
            workflow=start_input["workflow"],
            context=start_input["context"],
        ),
        StepDependencies(
            incidents=deps.incidents,
            results=MemoryStepResultStore(clock=lambda: NOW),
            clock=lambda: NOW,
        ),
    )
    assert gathered.context["product_profile_id"] == "PP-2-8-45"


def _choice_roots(definition: dict[str, Any]) -> set[str]:
    """Top-level fields any Choice comparison reads, including nested And/Or/Not."""

    roots: set[str] = set()

    def walk(rule: dict[str, Any]) -> None:
        variable = rule.get("Variable")
        if isinstance(variable, str) and variable.startswith("$."):
            roots.add(variable.removeprefix("$.").split(".", 1)[0])
        for key in ("And", "Or"):
            for item in rule.get(key, []):
                walk(item)
        nested = rule.get("Not")
        if isinstance(nested, dict):
            walk(nested)

    for state in definition["States"].values():
        if state.get("Type") != "Choice":
            continue
        for choice in state.get("Choices", []):
            walk(choice)
    return roots


def test_ph2841_open_output_parses_as_gather_context() -> None:
    """The start input, after the real OPEN stub, is a GatherContext envelope."""

    seed_path = Path(__file__).resolve().parents[3] / "sim" / "seed" / "t1" / "shipments.json"
    row = next(
        item
        for item in json.loads(seed_path.read_text(encoding="utf-8"))
        if item["shipment_id"] == "PH-2841"
    )

    class SeedShipments:
        def get(self, tenant_id: str, shipment_id: str):
            assert tenant_id == "t1"
            assert shipment_id == "PH-2841"
            return row

    deps = _deps(
        devices=Devices({"RF-7781": ("t1", "PH-2841")}),
        shipments=SeedShipments(),
    )
    detail = {"device_id": "RF-7781", "shipment_id": "PH-2841"}
    handler(_event("hot-1", detail=detail), None, dependencies=deps)
    handler(_event("hot-2", detail={**detail, "seq": 8}), None, dependencies=deps)
    start_input = deps.executions.calls[0]["input"]

    opened = workflow_stub({"step_name": "OPEN", "workflow": start_input}, None)
    envelope = parse_envelope({"step_name": "GatherContext", "workflow": opened})

    assert envelope.step == "GatherContext"
    assert envelope.tenant_id == "t1"
    assert envelope.shipment_id == "PH-2841"
    definition = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "domains"
            / "cold_chain_excursion"
            / "statemachine.asl.json"
        ).read_text(encoding="utf-8")
    )
    roots = _choice_roots(definition)
    assert "cancel_requested" in roots
    assert "cancel_requested" not in StepEnvelope.model_fields
    assert "cancel_requested" not in start_input
    allowed = set(StepEnvelope.model_fields)
    for field in roots:
        if field in start_input:
            assert field in allowed


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
    assert "shipment=PH-1 incident=" in text
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


def test_one_accepted_reading_on_an_open_incident_creates_one_read_row() -> None:
    deps = _deps()
    handler(_event("hot-1"), None, dependencies=deps)
    opened = handler(
        _event("hot-2", temperature=12, detail={"seq": 8, "ts": "2026-10-20T10:20:00Z"}),
        None,
        dependencies=deps,
    )
    incident_id = opened["incident_id"]
    assert len(deps.incidents.readings("t1", incident_id)) == 1

    accepted = handler(
        _event("steady", temperature=5, detail={"seq": 9, "ts": "2026-10-20T10:26:00Z"}),
        None,
        dependencies=deps,
    )
    assert accepted["status"] == "accepted"
    rows = deps.incidents.readings("t1", incident_id)
    assert len(rows) == 2
    suffix = hashlib.sha256(accepted["event_id"].encode("utf-8")).hexdigest()
    assert rows[-1] == {
        "pk": f"TENANT#t1#INC#{incident_id}",
        "sk": f"READ#2026-10-20T10:26:00Z#000000000009#{suffix}",
        "tenant_id": "t1",
        "ts": "2026-10-20T10:26:00Z",
        "temperature_c": 5.0,
        "expires_at": int(datetime(2026, 10, 20, 10, 26, tzinfo=UTC).timestamp())
        + 90 * 24 * 60 * 60,
    }

    deps.dedupe.forget(accepted["event_id"])
    retried = handler(
        _event("steady", temperature=5, detail={"seq": 9, "ts": "2026-10-20T10:26:00Z"}),
        None,
        dependencies=deps,
    )
    assert retried["status"] == "accepted"
    assert deps.incidents.readings("t1", incident_id) == rows


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
    table = _ReportTable(incident, deps.incidents.readings("t1", incident.incident_id))
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
