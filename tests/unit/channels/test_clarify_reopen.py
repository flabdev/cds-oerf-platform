"""Unclear reply: inbound marks ANSWERED, interpret clarifies, send_rung reopens."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from oerf.platform.cadence.engage import compute_rung
from oerf.platform.cadence.engagement_lifecycle import reset_ladder
from oerf.platform.cadence.interpret import interpret_workflow
from oerf.platform.channels.address import resolve_party_address
from oerf.platform.channels.engagement import DynamoEngagementStore
from oerf.platform.channels.engagement_workflow import send_rung
from oerf.platform.channels.providers.fake import FakeProvider
from oerf.platform.channels.registry import DynamoPhoneRegistry
from oerf.platform.channels.router import OutboundRequest
from tests.unit.support.fake_dynamo import FakeDynamoClient

MESSAGES = json.loads(
    Path("domains/cold_chain_excursion/messages.json").read_text(encoding="utf-8")
)
START = datetime(2026, 10, 27, 14, 0, tzinfo=UTC)
TENANT = "tenant"
ROUTING = "routing"
INCIDENT = "incident"


def _ports(policy_id: str, *, role: str, ask: str, template: str, contact: str) -> tuple[
    DynamoEngagementStore, DynamoPhoneRegistry, str, dict
]:
    client = FakeDynamoClient()
    registry = DynamoPhoneRegistry(client, ROUTING, TENANT)
    phone = "+15550100001" if role == "driver" else "+15550100002"
    registry.register_contact("t1", contact, role, phone)
    store = DynamoEngagementStore(client, INCIDENT, ROUTING, clock=lambda: START)
    recipient = resolve_party_address(
        "t1", role, contact, registry=registry, parameters=None, live=False
    )
    assert recipient == phone
    workflow = {
        "tenant_id": "t1",
        "incident_id": "INC-000003",
        "shipment_id": "PH-2841",
        "engagement_id": f"ENG-INC-000003-{role}-{ask}",
        "role": role,
        "ask": ask,
        "policy_id": policy_id,
        "facts": {
            "shipment_id": "PH-2841",
            "temperature_c": "8.6",
            "minutes": "20",
            "range": "8-12",
        },
        "engagement_started_at": START.isoformat(),
    }
    return store, registry, recipient, workflow


def _send(
    store: DynamoEngagementStore,
    registry: DynamoPhoneRegistry,
    recipient: str,
    workflow: dict,
    *,
    token: str,
    rung: int,
    template: str,
) -> str:
    deadline = START + timedelta(minutes=5)
    result = send_rung(
        OutboundRequest(
            tenant_id=workflow["tenant_id"],
            incident_id=workflow["incident_id"],
            engagement_id=workflow["engagement_id"],
            role=workflow["role"],
            template_id=template,
            facts=workflow["facts"],
            contact_id="C-T1-DRV" if workflow["role"] == "driver" else "C-T1-FACA",
            recipient=recipient,
            channels=("whatsapp",),
        ),
        ask=workflow["ask"],
        policy_id=workflow["policy_id"],
        task_token=token,
        rung=rung,
        deadline=deadline,
        engagements=store,
        registry=registry,
        providers={"whatsapp": FakeProvider("whatsapp")},
        messages=MESSAGES,
    )
    assert result.delivered
    return result.provider_msg_id


def _inbound_answered(store: DynamoEngagementStore, workflow: dict) -> None:
    store.answer(workflow["tenant_id"], workflow["incident_id"], workflow["engagement_id"])
    saved = store.get(
        workflow["tenant_id"], workflow["incident_id"], workflow["engagement_id"]
    )
    assert saved is not None and saved.status == "ANSWERED"


def _clarify_path(workflow: dict, reply_text: str) -> dict:
    workflow = interpret_workflow({**workflow, "reply": {"text": reply_text}}, enabled=False)
    assert workflow["response"]["intent"] == "clarify"
    assert workflow["response"]["valid"] is False
    return reset_ladder(workflow)


def test_driver_accept_clarify_then_running() -> None:
    store, registry, recipient, workflow = _ports(
        "critical_field",
        role="driver",
        ask="diagnose",
        template="driver_checkin",
        contact="C-T1-DRV",
    )
    _send(store, registry, recipient, workflow, token="token-1", rung=1, template="driver_checkin")
    _inbound_answered(store, workflow)

    workflow = _clarify_path(workflow, "accept")
    workflow = compute_rung(workflow, START + timedelta(seconds=30))
    assert workflow["cadence"]["awaits_reply"] is True

    _send(
        store,
        registry,
        recipient,
        workflow,
        token="token-2",
        rung=workflow["cadence"]["next_rung"],
        template=workflow["cadence"]["message"],
    )
    saved = store.get(workflow["tenant_id"], workflow["incident_id"], workflow["engagement_id"])
    assert saved.status == "WAITING"
    assert saved.task_token == "token-2"
    assert store.for_address("whatsapp", recipient)

    workflow = interpret_workflow({**workflow, "reply": {"text": "Running"}}, enabled=False)
    assert workflow["response"]["intent"] == "unit_running"
    assert workflow["response"]["valid"] is True
    store.answer(workflow["tenant_id"], workflow["incident_id"], workflow["engagement_id"])
    assert (
        store.get(workflow["tenant_id"], workflow["incident_id"], workflow["engagement_id"]).status
        == "ANSWERED"
    )


def test_facility_running_clarify_then_decline() -> None:
    store, registry, recipient, workflow = _ports(
        "partner_request",
        role="facility",
        ask="accept",
        template="facility_request",
        contact="C-T1-FACA",
    )
    _send(
        store, registry, recipient, workflow, token="fac-1", rung=1, template="facility_request"
    )
    _inbound_answered(store, workflow)

    workflow = _clarify_path(workflow, "running")
    workflow = compute_rung(workflow, START + timedelta(seconds=45))

    _send(
        store,
        registry,
        recipient,
        workflow,
        token="fac-2",
        rung=workflow["cadence"]["next_rung"],
        template=workflow["cadence"]["message"],
    )

    workflow = interpret_workflow({**workflow, "reply": {"text": "Decline"}}, enabled=False)
    assert workflow["response"]["intent"] == "decline"
    assert workflow["response"]["valid"] is True
    store.answer(workflow["tenant_id"], workflow["incident_id"], workflow["engagement_id"])


def test_two_clarifies_then_cadence_exhausts() -> None:
    store, registry, recipient, workflow = _ports(
        "critical_field",
        role="driver",
        ask="diagnose",
        template="driver_checkin",
        contact="C-T1-DRV",
    )
    token = "token-1"
    now = START
    _send(store, registry, recipient, workflow, token=token, rung=1, template="driver_checkin")
    for cycle in range(2):
        _inbound_answered(store, workflow)
        workflow = _clarify_path(workflow, "accept")
        now += timedelta(seconds=45)
        workflow = compute_rung(workflow, now)
        if workflow["cadence"].get("exhausted"):
            break
        token = f"token-{cycle + 2}"
        _send(
            store,
            registry,
            recipient,
            workflow,
            token=token,
            rung=workflow["cadence"]["next_rung"],
            template=workflow["cadence"]["message"],
        )
    else:
        _inbound_answered(store, workflow)
        workflow = _clarify_path(workflow, "accept")
        exhausted_at = START + timedelta(minutes=5)
        workflow = compute_rung(workflow, exhausted_at)
        assert workflow["cadence"]["exhausted"] is True