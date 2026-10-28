"""Real End User Messaging shapes resume the t1 driver engagement."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oerf.platform.channels.address import resolve_party_address
from oerf.platform.channels.engagement import DynamoEngagementStore, Engagement
from oerf.platform.channels.engagement_workflow import send_rung
from oerf.platform.channels.providers.fake import FakeProvider
from oerf.platform.channels.registry import DynamoPhoneRegistry
from oerf.platform.channels.router import OutboundRequest
from oerf.platform.inbound.dedupe import InboundDedupe
from oerf.platform.inbound.handler import InboundDependencies, process_message
from scripts.seed_runtime import seed_runtime
from tests.unit.support.fake_dynamo import FakeDynamoClient

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
TENANT = "tenant"
ROUTING = "routing"
INCIDENT = "incident"
TOKEN = "task-token-driver-diagnose"
OTHER_TOKEN = "task-token-other-rung"
ENGAGEMENT = "ENG-INC-000003-driver-diagnose"
OTHER = "ENG-INC-000004-driver-diagnose"
WAMID = "wamid.HBgLMTU1NTAxMDAwMDEVAgARGBIexample"
AWS_EVENT = "6d69f07a-c317-4278-9d5c-6a84078419ec"
DRIVER = "15550100001"
OTHER_NUMBER = "+910000000199"
MESSAGES = json.loads(
    Path("domains/cold_chain_excursion/messages.json").read_text(encoding="utf-8")
)


class _Callbacks:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def send_task_success(self, *, taskToken: str, output: str) -> None:
        self.calls.append({"taskToken": taskToken, "output": output})


def _ports() -> tuple[DynamoPhoneRegistry, DynamoEngagementStore, InboundDependencies, _Callbacks]:
    client = FakeDynamoClient()
    seed_runtime(client, TENANT, "t1", apply=True, routing_table=ROUTING)
    registry = DynamoPhoneRegistry(client, ROUTING, TENANT)
    store = DynamoEngagementStore(client, INCIDENT, ROUTING, clock=lambda: NOW)
    callbacks = _Callbacks()
    dependencies = InboundDependencies(
        InboundDedupe(clock=lambda: NOW),
        store,
        callbacks,
        registry,
    )
    return registry, store, dependencies, callbacks


def _send(
    store: DynamoEngagementStore,
    registry: DynamoPhoneRegistry,
    engagement_id: str,
    token: str,
    provider: FakeProvider | None = None,
) -> Engagement:
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
        deadline=datetime(2026, 10, 28, 12, 10, tzinfo=UTC),
        engagements=store,
        registry=registry,
        providers={"whatsapp": provider or FakeProvider("whatsapp", simulated=True)},
        messages=MESSAGES,
    )
    opened = store.get("t1", "INC-000003", engagement_id)
    assert opened is not None
    return opened


def _notification(webhook: dict, *, message_id: str = "wamid.inbound-1") -> dict:
    """SNS notification carrying an End User Messaging event header."""

    published = {
        "context": {
            "MetaPhoneNumberIds": [
                {
                    "metaPhoneNumberId": "abcde1234567890",
                    "arn": (
                        "arn:aws:social-messaging:us-east-1:000000000000:phone-number-id/example"
                    ),
                }
            ]
        },
        "whatsAppWebhookEntry": json.dumps(webhook),
        "aws_account_id": "000000000000",
        "message_timestamp": "2026-10-28T12:00:43.271279391Z",
        "messageId": message_id,
    }
    return {
        "Type": "Notification",
        "MessageId": "sns-1",
        "Message": json.dumps(published),
    }


def _entry(message: dict, *, wrapped: bool = False) -> dict:
    change = {
        "field": "messages",
        "value": {
            "messaging_product": "whatsapp",
            "metadata": {
                "display_phone_number": "15550783881",
                "phone_number_id": "46271669example",
            },
            "contacts": [{"profile": {"name": "Driver"}, "wa_id": DRIVER}],
            "messages": [message],
        },
    }
    if wrapped:
        return {
            "object": "whatsapp_business_account",
            "entry": [{"id": "503131219501234", "changes": [change]}],
        }
    return {"id": "503131219501234", "changes": [change]}


def _text(sender: str = DRIVER, *, context: str | None = None) -> dict:
    message: dict = {
        "from": sender,
        "id": "wamid.inbound-text",
        "timestamp": 1761645643,
        "type": "text",
        "text": {"body": "Running"},
    }
    if context is not None:
        message["context"] = {"from": "15550783881", "id": context}
    return message


def _button(kind: str, context: str) -> dict:
    message: dict = {
        "from": DRIVER,
        "id": f"wamid.inbound-{kind}",
        "timestamp": "1761645643",
        "context": {"from": "15550783881", "id": context},
    }
    if kind == "button":
        message.update({"type": "button", "button": {"payload": "Running", "text": "Running"}})
    elif kind == "payload":
        message.update({"type": "button", "button": {"payload": "Stopped"}})
    else:
        message.update(
            {
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": "Running", "title": "Running"},
                },
            }
        )
    return message


def _reply_text(callbacks: _Callbacks) -> str:
    assert callbacks.calls
    return json.loads(callbacks.calls[-1]["output"])["text"]


def test_unquoted_text_reply_resumes(capsys: pytest.CaptureFixture[str]) -> None:
    registry, store, dependencies, callbacks = _ports()
    _send(store, registry, ENGAGEMENT, TOKEN)
    body = _notification(_entry(_text(), wrapped=True))

    assert process_message(body, dependencies) == "resumed"

    assert callbacks.calls[0]["taskToken"] == TOKEN
    assert _reply_text(callbacks) == "Running"
    assert "requesting SQS retry" not in capsys.readouterr().out
    closed = store.get("t1", "INC-000003", ENGAGEMENT)
    assert closed is not None and closed.status == "ANSWERED"


@pytest.mark.parametrize("kind", ["button", "payload", "interactive"])
def test_quoted_button_with_an_unknown_context_uses_the_sender(kind: str) -> None:
    registry, store, dependencies, callbacks = _ports()
    _send(store, registry, ENGAGEMENT, TOKEN)
    expected = "Stopped" if kind == "payload" else "Running"
    body = _notification(_entry(_button(kind, WAMID), wrapped=True), message_id=f"wamid.in-{kind}")

    assert process_message(body, dependencies) == "resumed"

    assert callbacks.calls[0]["taskToken"] == TOKEN
    assert _reply_text(callbacks) == expected


def test_quoted_reply_with_the_stored_id_resumes_directly() -> None:
    registry, store, dependencies, callbacks = _ports()
    provider = FakeProvider("whatsapp", simulated=True)
    first = _send(store, registry, ENGAGEMENT, TOKEN, provider)
    second = _send(store, registry, OTHER, OTHER_TOKEN, provider)
    store.register_outbound(WAMID, first)
    assert len(store.for_address("whatsapp", "+15550100001")) == 2
    body = _notification(_entry(_button("button", WAMID)))

    assert process_message(body, dependencies) == "resumed"

    assert callbacks.calls[0]["taskToken"] == TOKEN
    assert _reply_text(callbacks) == "Running"
    assert store.get("t1", "INC-000003", OTHER) == second


def test_status_only_event_is_ignored_and_stores_the_wamid() -> None:
    registry, store, dependencies, callbacks = _ports()
    opened = _send(store, registry, ENGAGEMENT, TOKEN)
    webhook = {
        "id": "503131219501234",
        "changes": [
            {
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "statuses": [
                        {
                            "id": WAMID,
                            "status": "sent",
                            "timestamp": "1761645600",
                            "recipient_id": DRIVER,
                        }
                    ],
                },
            }
        ],
    }

    assert process_message(_notification(webhook, message_id=AWS_EVENT), dependencies) == "ignored"

    assert callbacks.calls == []
    assert store.correlate(WAMID) == opened
    assert store.get("t1", "INC-000003", ENGAGEMENT) == opened


@pytest.mark.security
def test_a_reply_from_another_tenants_number_is_ignored(
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry, store, dependencies, callbacks = _ports()
    opened = _send(store, registry, ENGAGEMENT, TOKEN)
    registry.register_contact("t2", "C-T2-DRV", "driver", OTHER_NUMBER)
    stored = store.for_address("whatsapp", "+15550100001")
    assert stored
    store.register_outbound(WAMID, stored[0])
    body = _notification(_entry(_text(OTHER_NUMBER.lstrip("+"), context=WAMID)))

    assert process_message(body, dependencies) == "ignored"

    assert callbacks.calls == []
    assert store.get("t1", "INC-000003", ENGAGEMENT) == opened
    text = capsys.readouterr().out
    assert OTHER_NUMBER not in text
    assert "does not own" in text
