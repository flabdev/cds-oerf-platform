"""A mocked driver reply resumes the engagement the router just sent."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oerf.platform.channels.address import resolve_party_address
from oerf.platform.channels.engagement import DynamoEngagementStore
from oerf.platform.channels.engagement_workflow import send_rung
from oerf.platform.channels.providers.fake import FakeProvider
from oerf.platform.channels.registry import DynamoPhoneRegistry
from oerf.platform.channels.router import OutboundRequest
from oerf.platform.inbound.dedupe import InboundDedupe
from oerf.platform.inbound.handler import InboundDependencies, process_message
from oerf.platform.inbound.normalize import whatsapp_event_body
from scripts.demo_reply import provider_message
from scripts.seed_runtime import seed_runtime
from tests.unit.support.fake_dynamo import FakeDynamoClient

NOW = datetime(2026, 10, 27, 14, 6, 42, tzinfo=UTC)
TENANT = "tenant"
ROUTING = "routing"
INCIDENT = "incident"
TOKEN = "task-token-driver-diagnose"
ENGAGEMENT = "ENG-INC-000003-driver-diagnose"
MESSAGES = json.loads(
    (Path("domains/cold_chain_excursion/messages.json")).read_text(encoding="utf-8")
)


class _Callbacks:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def send_task_success(self, *, taskToken: str, output: str) -> None:
        self.calls.append({"taskToken": taskToken, "output": output})


def _send(engagement_id: str, token: str) -> tuple[DynamoPhoneRegistry, DynamoEngagementStore, str]:
    client = FakeDynamoClient()
    seed_runtime(client, TENANT, "t1", apply=True, routing_table=ROUTING)
    registry = DynamoPhoneRegistry(client, ROUTING, TENANT)
    store = DynamoEngagementStore(client, INCIDENT, ROUTING, clock=lambda: NOW)
    recipient = resolve_party_address(
        "t1", "driver", "C-T1-DRV", registry=registry, parameters=None, live=False
    )
    send_rung(
        OutboundRequest(
            tenant_id="t1",
            incident_id="INC-000003",
            engagement_id=engagement_id,
            role="driver",
            template_id="driver_checkin",
            facts={"shipment_id": "PH-2841", "temperature_c": "8.6"},
            contact_id="C-T1-DRV",
            recipient=recipient,
            channels=("whatsapp",),
        ),
        ask="diagnose",
        policy_id="critical_field",
        task_token=token,
        rung=1,
        deadline=datetime(2026, 10, 27, 14, 10, tzinfo=UTC),
        engagements=store,
        registry=registry,
        providers={"whatsapp": FakeProvider("whatsapp", simulated=True)},
        messages=MESSAGES,
    )
    return registry, store, recipient


def _inbound(
    registry: DynamoPhoneRegistry, store: DynamoEngagementStore
) -> tuple[InboundDependencies, _Callbacks]:
    callbacks = _Callbacks()
    dependencies = InboundDependencies(
        InboundDedupe(clock=lambda: NOW),
        store,
        callbacks,
        registry,
    )
    return dependencies, callbacks


def test_demo_reply_resumes_the_driver_token(capsys: pytest.CaptureFixture[str]) -> None:
    registry, store, recipient = _send(ENGAGEMENT, TOKEN)
    dependencies, callbacks = _inbound(registry, store)
    body = provider_message(
        sender=recipient,
        text="Running",
        received_at="2026-10-27T14:06:42Z",
        provider_msg_id="wa-demo-driver",
    )

    assert process_message(body, dependencies) == "resumed"

    assert callbacks.calls[0]["taskToken"] == TOKEN
    assert "does not own" not in capsys.readouterr().out


def test_end_user_messaging_digits_resume_the_same_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry, store, _recipient = _send(ENGAGEMENT, TOKEN)
    dependencies, callbacks = _inbound(registry, store)
    webhook = {
        "changes": [
            {
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "messages": [
                        {
                            "from": "15550100001",
                            "id": "wamid.driver-1",
                            "timestamp": "1761574002",
                            "type": "text",
                            "text": {"body": "Running"},
                        }
                    ],
                },
            }
        ]
    }
    published = {
        "whatsAppWebhookEntry": json.dumps(webhook),
        "messageId": "wamid.driver-1",
        "message_timestamp": "2026-10-27T14:06:42.000Z",
    }
    body = {
        "Type": "Notification",
        "MessageId": "sns-driver-1",
        "Message": json.dumps(published),
    }
    prepared = whatsapp_event_body(body)
    assert prepared is not None
    assert prepared["sender"] == "+15550100001"

    assert process_message(body, dependencies) == "resumed"

    assert callbacks.calls[0]["taskToken"] == TOKEN
    assert "does not own" not in capsys.readouterr().out
