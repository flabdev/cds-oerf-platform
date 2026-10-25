"""demo_reply posts a provider message. It cannot cancel or stop an execution."""

import json
from datetime import UTC, datetime

import pytest

from oerf.platform.channels.engagement import MemoryEngagementStore
from oerf.platform.channels.registry import MemoryPhoneRegistry
from oerf.platform.inbound.dedupe import InboundDedupe
from oerf.platform.inbound.handler import InboundDependencies, process_message
from scripts.demo_reply import (
    button_labels,
    channels_live_from_configuration,
    provider_message,
    queued_summary,
)

NOW = datetime(2026, 10, 20, 10, 16, tzinfo=UTC)
PLACEHOLDER = "+910000000101"


class _Callbacks:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def send_task_success(self, *, taskToken: str, output: str) -> None:
        self.calls.append({"taskToken": taskToken, "output": output})


class _Executions:
    def __init__(self) -> None:
        self.stops: list[dict[str, str]] = []

    def stop_execution(self, *, executionArn: str, error: str, cause: str) -> None:
        self.stops.append({"executionArn": executionArn, "error": error, "cause": cause})


def test_button_ids_come_from_the_approved_template() -> None:
    assert button_labels() == {
        "running": "Running",
        "stopped": "Stopped",
        "not_sure": "Not sure",
        "call_me": "Call me",
    }


def test_a_missing_router_flag_fails() -> None:
    with pytest.raises(SystemExit, match="USE_MOCK_WHATSAPP"):
        channels_live_from_configuration({})
    assert channels_live_from_configuration({"USE_MOCK_WHATSAPP": "false"}) is True
    assert channels_live_from_configuration({"USE_MOCK_WHATSAPP": "true"}) is False


@pytest.mark.security
def test_demo_reply_cannot_produce_a_dashboard_action() -> None:
    message = provider_message(
        sender=PLACEHOLDER,
        text="Running",
        received_at="2026-10-20T10:16:00Z",
        provider_msg_id="wa-demo-1",
    )
    assert message["channel"] == "whatsapp"
    assert message["type"] == "text"
    assert message["media"] == []
    assert "action" not in message
    assert "action_id" not in message
    summary = queued_summary(message, tenant_id="t1", shipment_id="PH-2841", role="driver")
    assert PLACEHOLDER not in summary
    assert "PH-2841" in summary

    engagements = MemoryEngagementStore(clock=lambda: NOW)
    opened = engagements.open_engagement(
        "t1",
        "INC-000001",
        "eng-1",
        "driver",
        "diagnose",
        "critical_field",
        "task-token-1",
        {},
        contact_id="C-T1-DRV",
        rung=1,
        deadline=NOW,
    )
    engagements.register_address("whatsapp", PLACEHOLDER, opened)
    registry = MemoryPhoneRegistry()
    registry.register_contact("t1", "C-T1-DRV", "driver", PLACEHOLDER)
    executions = _Executions()
    callbacks = _Callbacks()
    dependencies = InboundDependencies(
        dedupe=InboundDedupe(clock=lambda: NOW),
        engagements=engagements,
        callbacks=callbacks,
        registry=registry,
        executions=executions,
        state_machine_arn="arn:aws:states:us-east-1:000000000000:stateMachine:oerf-cold-chain",
    )

    assert process_message(message, dependencies) == "resumed"
    assert executions.stops == []
    assert len(callbacks.calls) == 1
    assert callbacks.calls[0]["taskToken"] == "task-token-1"
    payload = json.loads(callbacks.calls[0]["output"])
    assert payload["channel"] == "whatsapp"
    assert "action" not in payload
    assert payload["text"] == "Running"
