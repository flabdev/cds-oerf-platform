"""Credential-free PH-2841 path: ingest, desk action, HTML report."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oerf.platform.api.dashboard import (
    DashboardServices,
    MemoryActionQueue,
    MemoryIncidentQuery,
    Principal,
    dispatch,
)
from oerf.platform.incidents.debounce import MemoryDebounce
from oerf.platform.incidents.repository import IncidentRepository
from oerf.platform.ingest.handler import IngestDependencies, handler as ingest
from oerf.platform.report.generator import handler as report
from oerf.platform.report.store import MemoryReportStore
from oerf.platform.tenancy.config_resolver import get_config

NOW = datetime(2026, 10, 20, 10, 16, tzinfo=UTC)
SEED = Path(__file__).resolve().parents[2] / "sim" / "seed" / "t1" / "shipments.json"


class _Devices:
    def resolve(self, device_id: str):
        return {"RF-7781": ("t1", "PH-2841")}.get(device_id)


class _Shipments:
    def get(self, tenant_id: str, shipment_id: str):
        row = next(
            item
            for item in json.loads(SEED.read_text(encoding="utf-8"))
            if item["shipment_id"] == shipment_id
        )
        assert tenant_id == "t1"
        return row


class _Configs:
    def get(self, tenant_id: str, domain: str):
        return get_config(tenant_id, domain)


class _Dedupe:
    def __init__(self):
        self.ids: set[str] = set()

    def register(self, event_id: str, *, now: datetime) -> bool:
        if event_id in self.ids:
            return False
        self.ids.add(event_id)
        return True

    def forget(self, event_id: str) -> None:
        self.ids.discard(event_id)


class _Executions:
    def __init__(self):
        self.calls: list[dict] = []

    def start(self, *, name: str, input: dict) -> str:
        self.calls.append({"name": name, "input": input})
        return f"arn:aws:states:us-east-1:123456789012:execution:test:{name}"


class _Table:
    def __init__(self, item: dict):
        self.item = item
        self.keys: list[dict] = []

    def Table(self, _name: str):
        return self

    def get_item(self, Key):
        self.keys.append(Key)
        if Key.get("pk") == f"TENANT#{self.item['tenant_id']}#INC#{self.item['incident_id']}":
            return {"Item": self.item}
        return {}

    def query(self, **_kwargs):
        return {"Items": []}


def _event(event_id: str, seq: int) -> dict:
    return {
        "id": event_id,
        "source": "sim.reefer",
        "detail-type": "TemperatureReading",
        "detail": {
            "tenant_id": "t1",
            "shipment_id": "PH-2841",
            "device_id": "RF-7781",
            "ts": "2026-10-20T10:16:00Z",
            "temperature_c": 8.1,
            "lat": 17.44,
            "lon": 78.38,
            "seq": seq,
        },
    }


@pytest.mark.e2e
def test_ph2841_ingest_desk_pick_and_html_report(monkeypatch: pytest.MonkeyPatch) -> None:
    deps = IngestDependencies(
        devices=_Devices(),
        shipments=_Shipments(),
        configs=_Configs(),
        debounce=MemoryDebounce(clock=lambda: NOW),
        incidents=IncidentRepository(clock=lambda: NOW),
        dedupe=_Dedupe(),
        executions=_Executions(),
        state_machine_arn="arn:aws:states:us-east-1:123456789012:stateMachine:test",
        clock=lambda: NOW,
    )
    ingest(_event("hot-1", 7), None, dependencies=deps)
    started = ingest(_event("hot-2", 8), None, dependencies=deps)
    stored = deps.incidents.get("t1", started["incident_id"])
    queue = MemoryActionQueue()
    services = DashboardServices(
        queries=MemoryIncidentQuery(
            [
                {
                    "tenant_id": stored.tenant_id,
                    "incident_id": stored.incident_id,
                    "state": "ASSESSING",
                    "shipment_id": stored.shipment_id,
                    "engagement_id": "ENG-03",
                }
            ]
        ),
        queue=queue,
    )
    ops = Principal("t1", "ops_user", "user-1", True)

    listed = dispatch("GET", "/incidents", ops, None, services)
    picked = dispatch(
        "POST",
        f"/incidents/{stored.incident_id}/actions",
        ops,
        {
            "action": "pick_option",
            "action_id": "act-1",
            "engagement_id": "ENG-03",
            "option_id": "FACILITY_A",
        },
        services,
        idempotency_key="act-1",
    )

    store = MemoryReportStore()
    table = _Table(
        {
            "tenant_id": "t1",
            "incident_id": stored.incident_id,
            "shipment_id": "PH-2841",
            "state": "CLOSED",
        }
    )
    monkeypatch.setattr("oerf.platform.report.generator._store", lambda: store)
    monkeypatch.setattr("oerf.platform.report.generator.INCIDENT_TABLE_NAME", "incidents")
    monkeypatch.setattr("oerf.platform.report.generator.get_dynamodb", lambda: table)
    monkeypatch.setenv("REPORT_PDF_BACKEND", "weasyprint")
    published = report({"tenant_id": "t1", "incident_id": stored.incident_id}, None)

    assert started["status"] == "triggered"
    assert listed[1]["incidents"][0]["incident_id"] == stored.incident_id
    assert picked == (202, {"action_id": "act-1", "status": "queued"})
    assert queue.messages[0]["option_id"] == "FACILITY_A"
    assert published["success"] is True
    assert published["report_key"] == f"reports/t1/{stored.incident_id}/v1.html"
    assert store.objects[published["report_key"]].startswith(b"<!DOCTYPE html")
    assert b"PH-2841" in store.objects[published["report_key"]]
