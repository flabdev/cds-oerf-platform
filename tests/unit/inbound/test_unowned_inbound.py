"""Unowned WhatsApp events are acknowledged with no write and one log line.

The AWS clients are fakes. A write or an outbound call is recorded and fails
the test. Phone digits and the body token must not appear in the log.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import oerf.platform.inbound.handler as inbound

SENDER = "15550100991"
BODY = "UNOWNED-BODY-TOKEN"
_MUTATIONS = frozenset(
    {
        "put_item",
        "update_item",
        "delete_item",
        "batch_write_item",
        "transact_write_items",
        "send_task_success",
        "send_task_failure",
        "stop_execution",
        "get_whatsapp_message_media",
        "send_whatsapp_message",
        "generate_presigned_url",
        "put_object",
        "publish",
    }
)


class _Aws:
    def __init__(self, *, registered: bool) -> None:
        self.registered = registered
        self.writes: list[str] = []

    def query(self, **kwargs: Any) -> dict[str, list]:
        values = kwargs.get("ExpressionAttributeValues") or {}
        raw = values.get(":pk")
        pk = raw.get("S") if isinstance(raw, dict) else ""
        if self.registered and isinstance(pk, str) and pk.startswith("PHONE#"):
            return {
                "Items": [
                    {
                        "tenant_id": {"S": "t1"},
                        "contact_id": {"S": "contact-1"},
                        "role": {"S": "driver"},
                        "e164": {"S": f"+{SENDER}"},
                        "opt_in": {"S": "true"},
                    }
                ]
            }
        return {"Items": []}

    def get_item(self, **kwargs: Any) -> dict:
        del kwargs
        return {}

    def __getattr__(self, name: str):
        def _call(**kwargs: Any) -> dict:
            del kwargs
            if name in _MUTATIONS:
                self.writes.append(name)
            return {}

        return _call


def _notification(webhook: dict) -> dict:
    published = {
        "whatsAppWebhookEntry": json.dumps(webhook),
        "message_timestamp": "2026-11-04T00:00:00.000000000Z",
        "messageId": "wamid.unowned",
    }
    return {"Type": "Notification", "MessageId": "sns-1", "Message": json.dumps(published)}


def _message(message: dict) -> dict:
    return _notification(
        {
            "id": "503131219501234",
            "changes": [
                {
                    "field": "messages",
                    "value": {"messaging_product": "whatsapp", "messages": [message]},
                }
            ],
        }
    )


def _text(body: str, *, context: str | None = None) -> dict:
    message: dict[str, Any] = {
        "from": SENDER,
        "id": "wamid.unowned-text",
        "timestamp": "1761645643",
        "type": "text",
        "text": {"body": body},
    }
    if context is not None:
        message["context"] = {"id": context}
    return _message(message)


def _status() -> dict:
    return _notification(
        {
            "id": "503131219501234",
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "statuses": [
                            {
                                "id": "wamid.other-stage",
                                "status": "delivered",
                                "timestamp": "1761645600",
                                "recipient_id": SENDER,
                            }
                        ],
                    },
                }
            ],
        }
    )


def _button() -> dict:
    return _message(
        {
            "from": SENDER,
            "id": "wamid.unowned-button",
            "timestamp": "1761645643",
            "type": "button",
            "button": {"payload": BODY, "text": BODY},
            "context": {"id": "wamid.other-stage"},
        }
    )


def _photo() -> dict:
    return _message(
        {
            "from": SENDER,
            "id": "wamid.unowned-photo",
            "timestamp": "1761645643",
            "type": "image",
            "image": {
                "mime_type": "image/jpeg",
                "id": "media-unowned",
                "caption": BODY,
            },
        }
    )


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch):
    client = _Aws(registered=False)

    def _install(*, registered: bool) -> _Aws:
        client.registered = registered
        client.writes.clear()
        monkeypatch.setattr(inbound, "INCIDENT_TABLE_NAME", "incidents")
        monkeypatch.setattr(inbound, "TENANT_TABLE_NAME", "tenants")
        monkeypatch.setattr(inbound, "ROUTING_TABLE_NAME", "routing")
        monkeypatch.setenv("EVIDENCE_BUCKET_NAME", "evidence")
        monkeypatch.setenv(
            "STATE_MACHINE_ARN",
            "arn:aws:states:us-east-1:000000000000:stateMachine:oerf-cold-chain",
        )
        inbound._client = None
        inbound.configure_dependencies(None)
        monkeypatch.setattr(inbound.boto3, "client", lambda *_args, **_kwargs: client)
        return client

    yield _install
    inbound._client = None
    inbound.configure_dependencies(None)


@pytest.mark.parametrize(
    ("kind", "registered", "event"),
    [
        ("status", False, _status()),
        ("button", False, _button()),
        ("text", False, _text(BODY)),
        ("text", True, _text(BODY)),
        ("opt_out", False, _text("STOP")),
        ("opt_out", False, _text("UNSUBSCRIBE")),
        ("media", False, _photo()),
    ],
    ids=[
        "status",
        "button",
        "text-unknown-sender",
        "text-registered-no-engagement",
        "stop",
        "unsubscribe",
        "media",
    ],
)
def test_unowned_event_is_a_quiet_no_op(
    aws, kind: str, registered: bool, event: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    client = aws(registered=registered)
    response = inbound.handler(
        {"Records": [{"messageId": "sqs-1", "body": json.dumps(event)}]}, None
    )

    assert response == {"batchItemFailures": []}
    assert client.writes == []
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    ignored = [line for line in lines if "not_owned" in str(line.get("msg"))]
    assert len(ignored) == 1
    assert ignored[0]["msg"] == f"inbound.ignored reason=not_owned kind={kind}"
    assert ignored[0]["level"] == "INFO"
    assert ignored[0]["outcome"] == "ignored"
    assert ignored[0]["count"] == 1
    assert set(ignored[0]) == {"ts", "level", "msg", "outcome", "count"}
    rendered = "\n".join(json.dumps(line) for line in lines)
    assert SENDER not in rendered
    assert BODY not in rendered
