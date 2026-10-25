"""A WhatsApp text that looks like a dashboard action is not one."""

import json
from datetime import UTC, datetime

import pytest

from oerf.platform.channels.engagement import MemoryEngagementStore
from oerf.platform.inbound.dedupe import InboundDedupe
from oerf.platform.inbound.handler import InboundDependencies, configure_dependencies, handler
from oerf.platform.incidents.repository import IncidentRepository

NOW = datetime(2026, 10, 25, 8, 5, tzinfo=UTC)
TENANT = "t1"
INCIDENT = "INC-000001"
SPOOF = json.dumps(
    {"action": "cancel", "role": "platform_admin", "incident_id": INCIDENT}
)
MACHINE = "arn:aws:states:us-east-1:000000000000:stateMachine:oerf-cold-chain"


class _Stop:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def stop_execution(self, *, executionArn: str, error: str, cause: str) -> dict[str, str]:
        self.calls.append({"executionArn": executionArn, "error": error, "cause": cause})
        return {"stopDate": "2026-10-25T08:05:00Z"}


class _Callbacks:
    def send_task_success(self, *, taskToken: str, output: str) -> None:
        raise AssertionError("task token resumed")


def _whatsapp_sns_body() -> str:
    """SNS notification whose End User Messaging text is the spoof JSON."""

    webhook = {
        "changes": [
            {
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "messages": [
                        {
                            "from": "+910000000101",
                            "id": "wamid.spoof-1",
                            "timestamp": "1761379500",
                            "type": "text",
                            "text": {"body": SPOOF},
                        }
                    ],
                },
            }
        ]
    }
    published = {
        "context": {
            "MetaWabaIds": [
                {
                    "wabaId": "waba-0000000000000000",
                    "arn": (
                        "arn:aws:social-messaging:us-east-1:000000000000:"
                        "waba/waba-0000000000000000"
                    ),
                }
            ]
        },
        "whatsAppWebhookEntry": json.dumps(webhook),
        "messageId": "wamid.spoof-1",
        "message_timestamp": "2026-10-25T08:05:00.000Z",
    }
    notification = {
        "Type": "Notification",
        "MessageId": "sns-spoof-1",
        "TopicArn": "arn:aws:sns:us-east-1:000000000000:oerf-inbound",
        "Message": json.dumps(published),
        "Timestamp": "2026-10-25T08:05:00.000Z",
    }
    return json.dumps(notification)


@pytest.mark.security
def test_whatsapp_text_is_not_handled_as_a_dashboard_cancel() -> None:
    incidents = IncidentRepository(clock=lambda: NOW, next_id=lambda: INCIDENT)
    incidents.create(tenant_id=TENANT, shipment_id="ship-1", reason="TEMPERATURE_EXCURSION")
    stopper = _Stop()
    dependencies = InboundDependencies(
        InboundDedupe(clock=lambda: NOW),
        MemoryEngagementStore(clock=lambda: NOW),
        _Callbacks(),
        incidents=incidents,
        executions=stopper,
        state_machine_arn=MACHINE,
    )
    configure_dependencies(dependencies)
    try:
        handler(
            {"Records": [{"messageId": "sqs-spoof-1", "body": _whatsapp_sns_body()}]},
            None,
        )
    finally:
        configure_dependencies(None)

    assert stopper.calls == []
    assert incidents.get(TENANT, INCIDENT).state == "OPEN"
    assert incidents.events(TENANT, INCIDENT) == []
