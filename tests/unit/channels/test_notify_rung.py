"""A rung addressed to another role notifies that role and leaves the party waiting."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from oerf.platform.cadence.engage import advance_rung, compute_rung
from oerf.platform.channels.engagement import MemoryEngagementStore
from oerf.platform.channels.engagement_send import send_engagement
from oerf.platform.channels.one_way import MemoryOutbox, send_one_way
from oerf.platform.channels.providers.fake import FakeProvider
from oerf.platform.channels.registry import MemoryPhoneRegistry
from oerf.platform.inbound.dedupe import InboundDedupe
from oerf.platform.inbound.handler import InboundDependencies, process_message

MESSAGES = json.loads(
    Path("domains/cold_chain_excursion/messages.json").read_text(encoding="utf-8")
)
START = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
DRIVER = "+15550100001"
OPS = "+15550100002"
DRIVER_CONTACT = "C-T1-DRV"
OPS_CONTACT = "C-T1-OPS"
ENGAGEMENT = "ENG-INC-000131-driver-diagnose"


class _Callbacks:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.error: Exception | None = ClientError(
            {"Error": {"Code": "TaskTimedOut", "Message": "the wait ended"}},
            "SendTaskSuccess",
        )

    def send_task_success(self, *, taskToken: str, output: str) -> None:
        self.calls.append({"taskToken": taskToken, "output": output})
        if self.error is not None:
            raise self.error


class _Ports:
    def __init__(self, store: MemoryEngagementStore, registry: MemoryPhoneRegistry) -> None:
        self.engagements = store
        self.registry = registry
        self.providers = {
            "whatsapp": FakeProvider("whatsapp"),
            "sms": FakeProvider("sms"),
            "voice": FakeProvider("voice"),
        }
        self.messages = MESSAGES
        self.parameters = None
        self.mock_whatsapp = True
        self.outbox = MemoryOutbox(clock=lambda: START)


def test_silent_driver_notifies_ops_once_and_the_driver_reply_still_answers() -> None:
    store = MemoryEngagementStore(clock=lambda: START)
    registry = MemoryPhoneRegistry()
    registry.register_contact("t1", DRIVER_CONTACT, "driver", DRIVER)
    registry.register_contact("t1", OPS_CONTACT, "ops_manager", OPS)
    ports = _Ports(store, registry)
    callbacks = _Callbacks()
    deps = InboundDependencies(
        InboundDedupe(clock=lambda: START),
        store,
        callbacks,
        registry,
        sandbox_join_enabled=False,
        audience_join_enabled=False,
        clock=lambda: START,
    )
    workflow = _workflow("critical_field", "driver")
    now = START
    supervisor_sends = 0
    for _ in range(12):
        planned = compute_rung(workflow, now)
        assert planned["role"] == "driver"
        cadence = planned["cadence"]
        if cadence.get("exhausted") or "message" not in cadence:
            break
        due = datetime.fromisoformat(cadence["due_at"])
        if cadence.get("awaits_reply", True):
            send_engagement(
                {
                    "step_name": "EngagementSendViaRouter",
                    "task_token": f"token-{cadence['next_rung']}",
                    "workflow": planned,
                },
                ports=ports,
            )
            now = due + timedelta(seconds=int(cadence["timeout_seconds"]))
        else:
            if cadence["message"] == "supervisor_escalation":
                supervisor_sends += 1
                assert cadence["send_role"] == "ops_manager"
                assert cadence["to_role"] == "ops_manager"
            outcome = send_one_way(
                {
                    "step_name": "EngagementSendOneWay",
                    "tenant_id": planned["tenant_id"],
                    "incident_id": planned["incident_id"],
                    "role": cadence["send_role"],
                    "message": cadence["message"],
                    "channel": cadence["channel"],
                    "facts": planned["facts"],
                    "engagement_started_at": planned["engagement_started_at"],
                    "dedupe_key": (
                        f"{planned['incident_id']}:{cadence['message']}:{cadence['next_rung']}"
                    ),
                },
                ports=ports,
                now=due,
            )
            if cadence["message"] == "supervisor_escalation":
                assert outcome["status"] == "sent"
            now = due
        workflow = advance_rung(planned, now)
    else:
        raise AssertionError("cadence ladder did not exhaust")

    whatsapp = ports.providers["whatsapp"]
    notices = [call for call in whatsapp.calls if call[2] == "supervisor_escalation"]
    assert supervisor_sends == 1
    assert len(notices) == 1
    assert notices[0][0] == OPS
    assert "PH-2841" in notices[0][1]
    assert "3 minutes" in notices[0][1]
    assert all(call[2] != "supervisor_escalation" for call in whatsapp.calls if call[0] == DRIVER)
    saved = store.get("t1", "INC-000131", ENGAGEMENT)
    assert saved is not None and saved.status == "WAITING"
    assert saved.role == "driver"
    assert saved.contact_id == DRIVER_CONTACT
    assert [item.engagement_id for item in store.for_address("whatsapp", DRIVER)] == [ENGAGEMENT]
    assert store.for_address("whatsapp", OPS) == []

    ignored = process_message(_reply(OPS, "Calling driver", "ops-reply"), deps)
    assert ignored == "ignored"
    assert callbacks.calls == []
    still_waiting = store.get("t1", "INC-000131", ENGAGEMENT)
    assert still_waiting is not None and still_waiting.status == "WAITING"

    answered = process_message(_reply(DRIVER, "Running", "driver-reply"), deps)
    assert answered == "late"
    closed = store.get("t1", "INC-000131", ENGAGEMENT)
    assert closed is not None and closed.status == "ANSWERED"
    assert closed.reply is not None and closed.reply["text"] == "Running"


def test_a_different_to_role_is_one_way_and_the_same_role_still_waits() -> None:
    facility = _messages("partner_request", "facility")
    assert ("ops_escalation", False, "ops_manager") in facility
    same_party = _messages("partner_request", "ops_manager")
    assert ("ops_escalation", True, "ops_manager") in same_party
    # Due at the exhaust instant, so the engine does not select the rung.
    assert all(message != "ops_escalation" for message, _awaits, _role in _messages(
        "approval_fast", "ops_manager"
    ))


def _messages(policy_id: str, role: str) -> list[tuple[str, bool, str | None]]:
    workflow = _workflow(policy_id, role)
    now = START
    seen: list[tuple[str, bool, str | None]] = []
    for _ in range(12):
        planned = compute_rung(workflow, now)
        cadence = planned["cadence"]
        if cadence.get("exhausted") or "message" not in cadence:
            return seen
        seen.append(
            (
                cadence["message"],
                bool(cadence.get("awaits_reply")),
                cadence.get("send_role") if isinstance(cadence.get("send_role"), str) else None,
            )
        )
        due = datetime.fromisoformat(cadence["due_at"])
        if cadence.get("awaits_reply", True):
            now = due + timedelta(seconds=int(cadence["timeout_seconds"]))
        else:
            now = due
        workflow = advance_rung(planned, now)
    raise AssertionError("cadence ladder did not exhaust")


def _workflow(policy_id: str, role: str) -> dict[str, Any]:
    return {
        "tenant_id": "t1",
        "incident_id": "INC-000131",
        "engagement_id": ENGAGEMENT,
        "role": role,
        "ask": "diagnose",
        "policy_id": policy_id,
        "contact_id": DRIVER_CONTACT if role == "driver" else OPS_CONTACT,
        "facts": {
            "shipment": {"shipment_id": "PH-2841", "driver": DRIVER_CONTACT},
            "trigger": {"temperature_c": 8.6},
        },
    }


def _reply(sender: str, text: str, message_id: str) -> dict[str, str]:
    return {
        "provider": "whatsapp",
        "message_id": message_id,
        "from": sender,
        "timestamp": "2026-11-22T10:04:00Z",
        "text": text,
    }
