"""Credential-free Wave 3 ingest, inbound, and evidence checks. No AWS."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from oerf.platform.channels.engagement import MemoryEngagementStore
from oerf.platform.channels.registry import MemoryPhoneRegistry
from oerf.platform.evidence import vision
from oerf.platform.inbound.dedupe import InboundDedupe
from oerf.platform.inbound.handler import InboundDependencies, process_message
from oerf.platform.incidents.debounce import MemoryDebounce
from oerf.platform.incidents.repository import IncidentRepository
from oerf.platform.ingest.handler import IngestDependencies, handler as ingest
from oerf.platform.tenancy.config_resolver import get_config

NOW = datetime(2026, 10, 20, 10, 16, tzinfo=UTC)
DEADLINE = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)


class _Devices:
    def resolve(self, device_id: str):
        return {"RF-1": ("t1", "PH-1")}.get(device_id)


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


class _Callbacks:
    def __init__(self):
        self.calls: list[dict[str, str]] = []

    def send_task_success(self, *, taskToken: str, output: str) -> None:
        self.calls.append({"taskToken": taskToken, "output": output})


class _NoopEvidence:
    def store(self, tenant_id, incident_id, message_id, media_id, mime_type):
        return None


def _ingest_deps() -> IngestDependencies:
    return IngestDependencies(
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


def _reading(event_id: str, *, seq: int = 7) -> dict[str, Any]:
    return {
        "id": event_id,
        "source": "sim.reefer",
        "detail-type": "TemperatureReading",
        "detail": {
            "tenant_id": "t1",
            "shipment_id": "PH-1",
            "device_id": "RF-1",
            "ts": "2026-10-20T10:16:00Z",
            "temperature_c": 9,
            "lat": 17.4,
            "lon": 78.3,
            "seq": seq,
        },
    }


@pytest.mark.integration
def test_duplicate_reading_does_not_start_a_second_execution() -> None:
    deps = _ingest_deps()
    first = ingest(_reading("same"), None, dependencies=deps)
    second = ingest(_reading("same"), None, dependencies=deps)

    assert first["status"] == "accepted"
    assert second == {"status": "duplicate", "event_id": first["event_id"]}
    assert deps.executions.calls == []


@pytest.mark.integration
def test_inbound_reply_resumes_the_stored_token_not_the_payload_token() -> None:
    engagements = MemoryEngagementStore(clock=lambda: NOW)
    registry = MemoryPhoneRegistry()
    registry.register_contact("trusted-tenant", "contact-1", "driver", "+15550000001")
    callbacks = _Callbacks()
    deps = InboundDependencies(
        InboundDedupe(clock=lambda: NOW),
        engagements,
        callbacks,
        registry,
        _NoopEvidence(),
    )
    opened = engagements.open_engagement(
        "trusted-tenant",
        "incident-1",
        "engagement-1",
        "driver",
        "confirm",
        "policy-1",
        "secret-task-token",
        {"shipment_id": "ship-1"},
        contact_id="contact-1",
        rung=1,
        deadline=DEADLINE,
    )
    engagements.register_outbound("outbound-1", opened)

    outcome = process_message(
        {
            "provider": "whatsapp",
            "message_id": "incoming-1",
            "from": "+15550000001",
            "timestamp": "2026-10-20T10:20:00Z",
            "text": "yes",
            "context_msg_id": "outbound-1",
            "tenant_id": "attacker-tenant",
            "task_token": "attacker-token",
        },
        deps,
    )
    payload = json.loads(callbacks.calls[0]["output"])

    assert outcome == "resumed"
    assert callbacks.calls[0]["taskToken"] == "secret-task-token"
    assert "task_token" not in payload
    assert payload["text"] == "yes"


@pytest.mark.integration
def test_evidence_pdf_key_is_not_analyzed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vision, "USE_MOCK_BEDROCK", True)
    event = {
        "source": "aws.s3",
        "detail-type": "Object Created",
        "detail": {
            "bucket": {"name": "evidence-bucket"},
            "object": {"key": "tenants/t1/inbound/INC-42/hash/display.pdf"},
        },
    }

    assert vision.handler(event, None) == {
        "success": False,
        "error": "invalid_s3_evidence_event",
    }
