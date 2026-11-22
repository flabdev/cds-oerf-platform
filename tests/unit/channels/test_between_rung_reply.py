"""A reply that lands between rungs is interpreted, and no later reminder is sent.

The order matches incident t1-INC-000130: the first wait ends, the one-way rung
runs, inbound answers while the child is in WaitUntilDue, then the next rung
comes due.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from oerf.platform.cadence.engage import handler as cadence_handler
from oerf.platform.cadence.engagement_lifecycle import handler as lifecycle_handler
from oerf.platform.cadence.interpret import interpret_workflow
from oerf.platform.channels.engagement import MemoryEngagementStore
from oerf.platform.channels.engagement_send import send_engagement
from oerf.platform.channels.one_way import MemoryOutbox, send_one_way
from oerf.platform.channels.providers.fake import FakeProvider
from oerf.platform.channels.registry import MemoryPhoneRegistry
from oerf.platform.inbound.dedupe import InboundDedupe
from oerf.platform.inbound.handler import InboundDependencies, process_message
from tests.unit.test_statemachine_definition import _apply_pass, _assign, evaluate_choice

MESSAGES = json.loads(
    Path("domains/cold_chain_excursion/messages.json").read_text(encoding="utf-8")
)
ASL = json.loads(
    Path("domains/cold_chain_excursion/engage_party.asl.json").read_text(encoding="utf-8")
)
PHONE = "+15550100001"
CONTACT = "C-T1-DRV"
NOW = datetime(2026, 11, 21, 16, 30, tzinfo=UTC)


class _Callbacks:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, str]] = []
        self.error = error

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
        }
        self.messages = MESSAGES
        self.parameters = None
        self.mock_whatsapp = True
        self.outbox = MemoryOutbox(clock=lambda: NOW)


def _workflow() -> dict[str, Any]:
    return {
        "tenant_id": "t1",
        "incident_id": "INC-000130",
        "engagement_id": "ENG-INC-000130-driver-diagnose",
        "role": "driver",
        "ask": "diagnose",
        "policy_id": "critical_field",
        "contact_id": CONTACT,
        "facts": {
            "shipment": {"shipment_id": "PH-2841", "driver": CONTACT},
            "trigger": {"temperature_c": 8.6},
            "minutes": "3",
        },
    }


def _deps(store: MemoryEngagementStore, registry: MemoryPhoneRegistry, callbacks: _Callbacks):
    return InboundDependencies(
        InboundDedupe(clock=lambda: NOW),
        store,
        callbacks,
        registry,
        sandbox_join_enabled=False,
        audience_join_enabled=False,
        clock=lambda: NOW,
    )


def _reply(text: str, quoted: str) -> dict[str, Any]:
    return {
        "provider": "whatsapp",
        "message_id": f"incoming-{text[:12]}",
        "from": PHONE,
        "timestamp": "2026-11-21T16:33:00Z",
        "text": text,
        "context_msg_id": quoted,
    }


def _terminal(state: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in state.get("Parameters", {}).items():
        if key.endswith(".$"):
            output[key[:-2]] = _path(data, value)
        else:
            output[key] = value
    return output


def _path(data: dict[str, Any], path: str) -> Any:
    node: Any = data
    for part in path.removeprefix("$").split("."):
        if not part:
            continue
        node = node[part]
    return node


def _walk(mode: str, text: str, *, reask: bool = False) -> dict[str, Any]:
    """Walk the real engage_party graph. ``mode`` is ``between`` or ``live``."""

    store = MemoryEngagementStore(clock=lambda: NOW)
    registry = MemoryPhoneRegistry()
    registry.register_contact("t1", CONTACT, "driver", PHONE)
    ports = _Ports(store, registry)
    late = ClientError(
        {"Error": {"Code": "TaskTimedOut", "Message": "the wait ended"}},
        "SendTaskSuccess",
    )
    callbacks = _Callbacks(error=None if mode == "live" else late)
    deps = _deps(store, registry, callbacks)
    states = ASL["States"]
    current = ASL["StartAt"]
    data = _workflow()
    visited: list[str] = []
    sends: list[str] = []
    one_ways: list[str] = []
    reasks: list[str] = []
    interpreted: list[str] = []
    quoted: str | None = None
    answered = False
    seen_one_way = False

    def answer_now() -> None:
        nonlocal answered
        assert quoted is not None, "the check-in never produced a message id to quote"
        outcome = process_message(_reply(text, quoted), deps)
        assert outcome == ("resumed" if mode == "live" else "late")
        answered = True

    for _ in range(96):
        visited.append(current)
        state = states[current]
        if state["Type"] == "Choice":
            current = evaluate_choice(state, data)
            continue
        if state["Type"] == "Wait":
            if (
                current == "WaitUntilDue"
                and seen_one_way
                and not answered
                and mode != "after_last"
            ):
                answer_now()
            current = state["Next"]
            continue
        if state["Type"] == "Pass":
            if state.get("End") or "Next" not in state:
                return {
                    "visited": visited,
                    "terminal": _terminal(state, data),
                    "data": data,
                    "sends": sends,
                    "one_ways": one_ways,
                    "reasks": reasks,
                    "interpreted": interpreted,
                    "store": store,
                    "callbacks": callbacks,
                    "providers": ports.providers,
                }
            data = _apply_pass(state, data)
            current = state["Next"]
            continue
        if state["Type"] == "Fail":
            raise AssertionError(f"engagement failed at {current}")
        if state["Type"] != "Task":
            raise AssertionError(state["Type"])

        if state["Resource"].endswith(".waitForTaskToken"):
            message = data["cadence"]["message"]
            if answered:
                if not reask:
                    raise AssertionError(f"cadence rung {message} was sent after the reply")
                _token_send(data, ports, sends=None)
                reasks.append(message)
                return {
                    "visited": visited,
                    "terminal": None,
                    "data": data,
                    "sends": sends,
                    "one_ways": one_ways,
                    "reasks": reasks,
                    "interpreted": interpreted,
                    "store": store,
                    "callbacks": callbacks,
                    "providers": ports.providers,
                }
            body = _token_send(data, ports, sends=sends)
            if isinstance(body.get("provider_msg_id"), str):
                quoted = body["provider_msg_id"]
            if mode == "live":
                answer_now()
                data = dict(data)
                data["reply"] = json.loads(callbacks.calls[-1]["output"])
                current = state["Next"]
                continue
            timeout = next(
                catcher
                for catcher in state["Catch"]
                if "States.Timeout" in catcher["ErrorEquals"]
            )
            current = timeout["Next"]
            continue

        step_name = state["Parameters"]["Payload"]["step_name"]
        if step_name == "EngagementSendViaRouter":
            body = send_engagement(
                {"step_name": step_name, "workflow": data},
                ports=ports,
            )
            _assign(data, "$.recall", {"status": body["status"], "reply": body.get("reply")})
            current = state["Next"]
            continue
        if step_name in {"EngagementComputeNextRung", "EngagementAdvanceRung"}:
            data = cadence_handler({"step_name": step_name, "workflow": data}, None)
            current = state["Next"]
            continue
        if step_name == "EngagementInterpret":
            interpreted.append(data["reply"]["text"])
            data = interpret_workflow(data, enabled=False)
            current = state["Next"]
            continue
        if step_name == "EngagementResetLadder":
            data = lifecycle_handler({"step_name": step_name, "workflow": data}, None)
            current = state["Next"]
            continue
        if current == "SendOneWay":
            one_ways.append(data["cadence"]["message"])
            outcome = send_one_way(
                {
                    "step_name": step_name,
                    "tenant_id": data["tenant_id"],
                    "incident_id": data["incident_id"],
                    "role": data["role"],
                    "message": data["cadence"]["message"],
                    "channel": data["cadence"]["channel"],
                    "facts": data["facts"],
                    "dedupe_key": (
                        f"{data['incident_id']}:{data['cadence']['message']}:"
                        f"{data['cadence']['next_rung']}"
                    ),
                },
                ports=object(),
            )
            _assign(data, state.get("ResultPath") or "$.one_way", outcome)
            seen_one_way = True
            current = state["Next"]
            if (
                mode == "after_last"
                and data["cadence"]["message"] == "supervisor_escalation"
                and not answered
            ):
                answer_now()
            continue
        if current == "AcknowledgeReply":
            outcome = send_one_way(
                {
                    "step_name": step_name,
                    "tenant_id": data["tenant_id"],
                    "incident_id": data["incident_id"],
                    "role": data["role"],
                    "message": "driver_ack",
                    "channel": "whatsapp",
                    "facts": {
                        "answer": data["reply"]["text"],
                        "shipment": data["facts"]["shipment"],
                    },
                    "dedupe_key": f"{data['incident_id']}:driver_ack",
                },
                ports=ports,
            )
            _assign(data, state.get("ResultPath") or "$.ack", outcome)
            current = state["Next"]
            continue
        raise AssertionError(step_name)
    raise AssertionError(f"engagement simulation did not terminate: {visited}")


def _token_send(data: dict[str, Any], ports: _Ports, *, sends: list[str] | None) -> dict[str, Any]:
    message = data["cadence"]["message"]
    if sends is not None:
        sends.append(message)
    return send_engagement(
        {
            "step_name": "EngagementSendViaRouter",
            "task_token": f"token-{data['cadence']['next_rung']}",
            "workflow": data,
        },
        ports=ports,
    )


def _templates(result: dict[str, Any]) -> list[str]:
    templates: list[str] = []
    for provider in result["providers"].values():
        templates.extend(call[2] for call in provider.calls if call[2])
    return templates


def _bodies(result: dict[str, Any]) -> list[str]:
    bodies: list[str] = []
    for provider in result["providers"].values():
        bodies.extend(call[1] for call in provider.calls)
    return bodies


def test_reply_during_wait_until_due_is_interpreted_and_sends_no_reminder() -> None:
    result = _walk("between", "Running")

    assert result["sends"] == ["driver_checkin", "driver_reminder", "driver_reminder"]
    assert result["one_ways"] == ["driver_call"]
    assert result["reasks"] == []
    assert result["interpreted"] == ["Running"]
    assert result["visited"].index("SendOneWay") < result["visited"].index("UseStoredReply")
    assert result["visited"].index("UseStoredReply") < result["visited"].index("Interpret")
    assert "SendViaRouter" not in result["visited"][result["visited"].index("SendOneWay") :]
    saved = result["store"].get("t1", "INC-000130", "ENG-INC-000130-driver-diagnose")
    assert saved is not None and saved.status == "ANSWERED"
    assert saved.reply is not None and saved.reply["text"] == "Running"
    assert result["terminal"]["status"] == "answered"
    assert result["terminal"]["intent"] == "unit_running"
    assert result["data"]["response"]["valid"] is True
    assert _templates(result) == ["driver_checkin"]
    assert any(body.startswith("Thanks, noted: Running") for body in _bodies(result))
    assert "supervisor_escalation" not in _templates(result)


def test_clarify_reply_between_rungs_still_reasks() -> None:
    result = _walk("between", "what do you mean", reask=True)

    assert result["one_ways"] == ["driver_call"]
    assert result["interpreted"] == ["what do you mean"]
    assert result["data"]["response"]["intent"] == "clarify"
    assert result["reasks"] == ["driver_checkin"]
    assert "Answered" not in result["visited"]
    assert "supervisor_escalation" not in result["sends"]
    assert "supervisor_escalation" not in _templates(result)
    saved = result["store"].get("t1", "INC-000130", "ENG-INC-000130-driver-diagnose")
    assert saved is not None and saved.status == "WAITING"


def test_reply_during_a_live_wait_still_resumes_the_token() -> None:
    result = _walk("live", "Running")

    assert result["sends"] == ["driver_checkin"]
    assert result["one_ways"] == []
    assert "UseStoredReply" not in result["visited"]
    assert result["interpreted"] == ["Running"]
    assert result["terminal"]["status"] == "answered"
    assert result["terminal"]["intent"] == "unit_running"
    assert len(result["callbacks"].calls) == 1
    assert json.loads(result["callbacks"].calls[0]["output"])["text"] == "Running"
    saved = result["store"].get("t1", "INC-000130", "ENG-INC-000130-driver-diagnose")
    assert saved is not None and saved.status == "ANSWERED"
    assert saved.reply is None
    assert _templates(result) == ["driver_checkin"]
    assert any(body.startswith("Thanks, noted: Running") for body in _bodies(result))


def test_a_reply_after_the_last_rung_is_interpreted_before_exhaustion() -> None:
    result = _walk("after_last", "Running")

    assert result["one_ways"] == ["driver_call", "supervisor_escalation"]
    assert "ApplyExhaustAction" not in result["visited"]
    assert "Exhausted" not in result["visited"]
    assert result["visited"].index("SendOneWay") < result["visited"].index("RecallBeforeExhaust")
    assert result["visited"].index("RecallBeforeExhaust") < result["visited"].index("Interpret")
    assert result["interpreted"] == ["Running"]
    assert result["terminal"]["status"] == "answered"
    assert result["terminal"]["intent"] == "unit_running"
    saved = result["store"].get("t1", "INC-000130", "ENG-INC-000130-driver-diagnose")
    assert saved is not None and saved.status == "ANSWERED"
    assert saved.reply is not None and saved.reply["text"] == "Running"
    assert "supervisor_escalation" not in _templates(result)


def test_recall_choice_routes_a_stored_reply_to_interpret() -> None:
    """The state machine, not a task-token callback, carries the stored reply."""

    states = ASL["States"]
    assert evaluate_choice(
        states["AlreadyAnswered?"],
        {"recall": {"status": "already_answered", "reply": {"text": "Running"}}},
    ) == "UseStoredReply"
    routed = _apply_pass(
        states["UseStoredReply"],
        {
            "tenant_id": "t1",
            "recall": {"status": "already_answered", "reply": {"text": "Running"}},
        },
    )
    assert routed["tenant_id"] == "t1"
    assert routed["reply"] == {"text": "Running"}
