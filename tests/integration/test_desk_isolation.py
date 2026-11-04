"""Credential-free Wave 2 isolation: ingest, list, and dashboard actions."""

from datetime import UTC, datetime

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
from oerf.platform.storage.dynamo import incident_pk
from oerf.platform.tenancy.config_resolver import get_config

NOW = datetime(2026, 10, 20, 10, 16, tzinfo=UTC)


class _Devices:
    def __init__(self, mapping):
        self.mapping = mapping

    def resolve(self, device_id: str):
        return self.mapping.get(device_id)


class _Shipments:
    def get(self, tenant_id: str, shipment_id: str):
        return {"shipment_id": shipment_id, "product_profile": "PP-2-8-45"}


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


def _reading(event_id: str, *, tenant_id: str = "t1", seq: int = 7, temperature: float = 9):
    return {
        "id": event_id,
        "source": "sim.reefer",
        "detail-type": "TemperatureReading",
        "detail": {
            "tenant_id": tenant_id,
            "shipment_id": "PH-1",
            "device_id": "RF-1",
            "ts": "2026-10-20T10:16:00Z",
            "temperature_c": temperature,
            "lat": 17.4,
            "lon": 78.3,
            "seq": seq,
        },
    }


def _ingest_deps(*, devices=None):
    return IngestDependencies(
        devices=devices or _Devices({"RF-1": ("t1", "PH-1")}),
        shipments=_Shipments(),
        configs=_Configs(),
        debounce=MemoryDebounce(clock=lambda: NOW),
        incidents=IncidentRepository(clock=lambda: NOW),
        dedupe=_Dedupe(),
        executions=_Executions(),
        state_machine_arn="arn:aws:states:us-east-1:123456789012:stateMachine:test",
        clock=lambda: NOW,
    )


@pytest.mark.integration
def test_ingest_opens_one_tenant_incident_and_dashboard_cannot_see_the_other() -> None:
    deps = _ingest_deps()
    ingest(_reading("evt-1"), None, dependencies=deps)
    started = ingest(_reading("evt-2", seq=8), None, dependencies=deps)
    incident_id = started["incident_id"]
    stored = deps.incidents.get("t1", incident_id)

    services = DashboardServices(
        queries=MemoryIncidentQuery(
            [
                {
                    "tenant_id": stored.tenant_id,
                    "incident_id": stored.incident_id,
                    "state": stored.state,
                    "shipment_id": stored.shipment_id,
                    "engagement_id": "ENG-03",
                }
            ]
        ),
        queue=MemoryActionQueue(),
    )
    ops = Principal("t1", "ops_user", "user-1", True)
    other = Principal("t2", "ops_user", "user-2", True)

    listed = dispatch("GET", "/incidents", ops, None, services)
    hidden = dispatch("GET", f"/incidents/{incident_id}", other, None, services)
    claimed = dispatch("GET", "/incidents", ops, {"tenant_id": "t2"}, services)

    assert started["status"] == "triggered"
    assert incident_pk(stored.tenant_id, stored.incident_id) == f"TENANT#t1#INC#{incident_id}"
    assert listed[0] == 200
    assert listed[1]["incidents"][0]["incident_id"] == incident_id
    assert hidden == (404, {"error": "not_found"})
    assert claimed == (403, {"error": "tenant_mismatch"})


@pytest.mark.integration
def test_unknown_device_and_claim_mismatch_do_not_start_an_incident() -> None:
    unknown = _ingest_deps(devices=_Devices({}))
    mismatch = _ingest_deps()

    dropped = ingest(_reading("unknown"), None, dependencies=unknown)
    rejected = ingest(_reading("mismatch", tenant_id="t2"), None, dependencies=mismatch)

    assert dropped["reason"] == "unknown_device"
    assert rejected["reason"] == "tenant_mismatch"
    assert unknown.executions.calls == []
    assert mismatch.executions.calls == []
