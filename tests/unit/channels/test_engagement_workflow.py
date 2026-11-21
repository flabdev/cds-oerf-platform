"""Engagement delivery facade tests; provider access stays behind the router."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oerf.platform.channels.engagement import MemoryEngagementStore
from oerf.platform.channels.engagement_workflow import AlreadyAnswered, PendingSend, send_rung
from oerf.platform.channels.providers.fake import FakeProvider
from oerf.platform.channels.registry import MemoryPhoneRegistry
from oerf.platform.channels.router import OutboundRequest

MESSAGES = {"messages": {"check": {"whatsapp": "Checking {shipment_id}"}}}


def test_send_rung_persists_wait_before_router_correlates_outbound() -> None:
    now = datetime(2026, 10, 20, 10, 16, tzinfo=UTC)
    deadline = now + timedelta(minutes=5)
    store = MemoryEngagementStore(clock=lambda: now)
    registry = MemoryPhoneRegistry()
    registry.register_contact("t1", "C-1", "driver", "+15550000001")
    provider = FakeProvider("whatsapp")
    request = OutboundRequest(
        tenant_id="t1",
        incident_id="INC-1",
        engagement_id="ENG-1",
        role="driver",
        template_id="check",
        facts={"shipment_id": "PH-2841"},
        contact_id="C-1",
        recipient="+15550000001",
        channels=("whatsapp",),
    )

    result = send_rung(
        request,
        ask="diagnose",
        policy_id="critical_field",
        task_token="token-1",
        rung=1,
        deadline=deadline,
        engagements=store,
        registry=registry,
        providers={"whatsapp": provider},
        messages=MESSAGES,
    )

    saved = store.get("t1", "INC-1", "ENG-1")
    assert result.delivered is True
    assert saved.task_token == "token-1"
    assert saved.rung == 1
    assert store.correlate(result.provider_msg_id).task_token == "token-1"


def test_send_rung_reopens_after_inbound_answered_for_clarify() -> None:
    now = datetime(2026, 10, 20, 10, 16, tzinfo=UTC)
    deadline = now + timedelta(minutes=5)
    store = MemoryEngagementStore(clock=lambda: now)
    registry = MemoryPhoneRegistry()
    registry.register_contact("t1", "C-1", "driver", "+15550000001")
    provider = FakeProvider("whatsapp")
    request = OutboundRequest(
        tenant_id="t1",
        incident_id="INC-1",
        engagement_id="ENG-1",
        role="driver",
        template_id="check",
        facts={"shipment_id": "PH-2841"},
        contact_id="C-1",
        recipient="+15550000001",
        channels=("whatsapp",),
    )
    send_rung(
        request,
        ask="diagnose",
        policy_id="critical_field",
        task_token="token-1",
        rung=1,
        deadline=deadline,
        engagements=store,
        registry=registry,
        providers={"whatsapp": provider},
        messages=MESSAGES,
    )
    store.answer("t1", "INC-1", "ENG-1")
    assert store.get("t1", "INC-1", "ENG-1").status == "ANSWERED"

    result = send_rung(
        request,
        ask="diagnose",
        policy_id="critical_field",
        task_token="token-2",
        rung=1,
        deadline=deadline,
        engagements=store,
        registry=registry,
        providers={"whatsapp": provider},
        messages=MESSAGES,
        response_intent="clarify",
    )

    saved = store.get("t1", "INC-1", "ENG-1")
    assert result.delivered is True
    assert saved.status == "WAITING"
    assert saved.task_token == "token-2"


def test_answered_engagement_with_valid_intent_does_not_reopen() -> None:
    now = datetime(2026, 10, 20, 10, 16, tzinfo=UTC)
    deadline = now + timedelta(minutes=5)
    store = MemoryEngagementStore(clock=lambda: now)
    registry = MemoryPhoneRegistry()
    registry.register_contact("t1", "C-1", "driver", "+15550000001")
    provider = FakeProvider("whatsapp")
    request = OutboundRequest(
        tenant_id="t1",
        incident_id="INC-1",
        engagement_id="ENG-1",
        role="driver",
        template_id="check",
        facts={"shipment_id": "PH-2841"},
        contact_id="C-1",
        recipient="+15550000001",
        channels=("whatsapp",),
    )
    send_rung(
        request,
        ask="diagnose",
        policy_id="critical_field",
        task_token="token-1",
        rung=1,
        deadline=deadline,
        engagements=store,
        registry=registry,
        providers={"whatsapp": provider},
        messages=MESSAGES,
    )
    store.answer("t1", "INC-1", "ENG-1")
    with pytest.raises(ValueError, match="only a WAITING engagement"):
        send_rung(
            request,
            ask="diagnose",
            policy_id="critical_field",
            task_token="token-2",
            rung=2,
            deadline=deadline,
            engagements=store,
            registry=registry,
            providers={"whatsapp": provider},
            messages=MESSAGES,
            response_intent="unit_running",
        )


def test_second_send_after_clarify_reopen_stays_waiting_without_double_reopen() -> None:
    now = datetime(2026, 10, 20, 10, 16, tzinfo=UTC)
    deadline = now + timedelta(minutes=5)
    store = MemoryEngagementStore(clock=lambda: now)
    registry = MemoryPhoneRegistry()
    registry.register_contact("t1", "C-1", "driver", "+15550000001")
    provider = FakeProvider("whatsapp")
    request = OutboundRequest(
        tenant_id="t1",
        incident_id="INC-1",
        engagement_id="ENG-1",
        role="driver",
        template_id="check",
        facts={"shipment_id": "PH-2841"},
        contact_id="C-1",
        recipient="+15550000001",
        channels=("whatsapp",),
    )
    send_rung(
        request,
        ask="diagnose",
        policy_id="critical_field",
        task_token="token-1",
        rung=1,
        deadline=deadline,
        engagements=store,
        registry=registry,
        providers={"whatsapp": provider},
        messages=MESSAGES,
    )
    store.answer("t1", "INC-1", "ENG-1")
    send_rung(
        request,
        ask="diagnose",
        policy_id="critical_field",
        task_token="token-2",
        rung=1,
        deadline=deadline,
        engagements=store,
        registry=registry,
        providers={"whatsapp": provider},
        messages=MESSAGES,
        response_intent="clarify",
    )
    send_rung(
        request,
        ask="diagnose",
        policy_id="critical_field",
        task_token="token-2",
        rung=1,
        deadline=deadline,
        engagements=store,
        registry=registry,
        providers={"whatsapp": provider},
        messages=MESSAGES,
        response_intent="clarify",
    )
    assert store.get("t1", "INC-1", "ENG-1").status == "WAITING"
    assert store.get("t1", "INC-1", "ENG-1").task_token == "token-2"


def test_stored_reply_is_returned_without_another_send() -> None:
    now = datetime(2026, 10, 20, 10, 16, tzinfo=UTC)
    deadline = now + timedelta(minutes=5)
    store = MemoryEngagementStore(clock=lambda: now)
    registry = MemoryPhoneRegistry()
    registry.register_contact("t1", "C-1", "driver", "+15550000001")
    provider = FakeProvider("whatsapp")
    request = OutboundRequest(
        tenant_id="t1",
        incident_id="INC-1",
        engagement_id="ENG-1",
        role="driver",
        template_id="check",
        facts={"shipment_id": "PH-2841"},
        contact_id="C-1",
        recipient="+15550000001",
        channels=("whatsapp",),
    )
    send_rung(
        request,
        ask="diagnose",
        policy_id="critical_field",
        task_token="token-1",
        rung=1,
        deadline=deadline,
        engagements=store,
        registry=registry,
        providers={"whatsapp": provider},
        messages=MESSAGES,
    )
    store.answer(
        "t1",
        "INC-1",
        "ENG-1",
        reply={"text": "Running", "channel": "whatsapp", "type": "text"},
    )
    sent = len(provider.calls)

    result = send_rung(
        request,
        ask="diagnose",
        policy_id="critical_field",
        task_token="token-2",
        rung=2,
        deadline=deadline,
        engagements=store,
        registry=registry,
        providers={"whatsapp": provider},
        messages=MESSAGES,
    )

    assert isinstance(result, AlreadyAnswered)
    assert result.status == "already_answered"
    assert result.reply["text"] == "Running"
    assert len(provider.calls) == sent
    assert store.get("t1", "INC-1", "ENG-1").status == "ANSWERED"


def test_empty_token_does_not_send_when_nothing_is_answered() -> None:
    now = datetime(2026, 10, 20, 10, 16, tzinfo=UTC)
    store = MemoryEngagementStore(clock=lambda: now)
    registry = MemoryPhoneRegistry()
    provider = FakeProvider("whatsapp")
    request = OutboundRequest(
        tenant_id="t1",
        incident_id="INC-1",
        engagement_id="ENG-1",
        role="driver",
        template_id="check",
        facts={"shipment_id": "PH-2841"},
        contact_id="C-1",
        recipient="+15550000001",
        channels=("whatsapp",),
    )

    result = send_rung(
        request,
        ask="diagnose",
        policy_id="critical_field",
        task_token="",
        rung=1,
        deadline=now,
        engagements=store,
        registry=registry,
        providers={"whatsapp": provider},
        messages=MESSAGES,
    )

    assert isinstance(result, PendingSend)
    assert provider.calls == []
    assert store.get("t1", "INC-1", "ENG-1") is None


def test_stopped_engagement_still_cannot_send() -> None:
    now = datetime(2026, 10, 20, 10, 16, tzinfo=UTC)
    deadline = now + timedelta(minutes=5)
    store = MemoryEngagementStore(clock=lambda: now)
    registry = MemoryPhoneRegistry()
    registry.register_contact("t1", "C-1", "driver", "+15550000001")
    provider = FakeProvider("whatsapp")
    request = OutboundRequest(
        tenant_id="t1",
        incident_id="INC-1",
        engagement_id="ENG-1",
        role="driver",
        template_id="check",
        facts={"shipment_id": "PH-2841"},
        contact_id="C-1",
        recipient="+15550000001",
        channels=("whatsapp",),
    )
    send_rung(
        request,
        ask="diagnose",
        policy_id="critical_field",
        task_token="token-1",
        rung=1,
        deadline=deadline,
        engagements=store,
        registry=registry,
        providers={"whatsapp": provider},
        messages=MESSAGES,
    )
    store.stop("t1", "INC-1", "ENG-1")

    with pytest.raises(ValueError, match="only a WAITING engagement"):
        send_rung(
            request,
            ask="diagnose",
            policy_id="critical_field",
            task_token="token-2",
            rung=2,
            deadline=deadline,
            engagements=store,
            registry=registry,
            providers={"whatsapp": provider},
            messages=MESSAGES,
        )


def test_clarify_with_a_stored_reply_still_reopens_and_sends() -> None:
    now = datetime(2026, 10, 20, 10, 16, tzinfo=UTC)
    deadline = now + timedelta(minutes=5)
    store = MemoryEngagementStore(clock=lambda: now)
    registry = MemoryPhoneRegistry()
    registry.register_contact("t1", "C-1", "driver", "+15550000001")
    provider = FakeProvider("whatsapp")
    request = OutboundRequest(
        tenant_id="t1",
        incident_id="INC-1",
        engagement_id="ENG-1",
        role="driver",
        template_id="check",
        facts={"shipment_id": "PH-2841"},
        contact_id="C-1",
        recipient="+15550000001",
        channels=("whatsapp",),
    )
    send_rung(
        request,
        ask="diagnose",
        policy_id="critical_field",
        task_token="token-1",
        rung=1,
        deadline=deadline,
        engagements=store,
        registry=registry,
        providers={"whatsapp": provider},
        messages=MESSAGES,
    )
    store.answer("t1", "INC-1", "ENG-1", reply={"text": "what do you mean", "type": "text"})

    result = send_rung(
        request,
        ask="diagnose",
        policy_id="critical_field",
        task_token="token-2",
        rung=2,
        deadline=deadline,
        engagements=store,
        registry=registry,
        providers={"whatsapp": provider},
        messages=MESSAGES,
        response_intent="clarify",
    )

    saved = store.get("t1", "INC-1", "ENG-1")
    assert not isinstance(result, (AlreadyAnswered, PendingSend))
    assert result.delivered is True
    assert saved is not None and saved.status == "WAITING"
    assert saved.reply is None
    assert saved.task_token == "token-2"


def test_no_provider_sdk_is_used_outside_the_router() -> None:
    source = Path("oerf/platform/channels/engagement_workflow.py").read_text(encoding="utf-8")
    assert "provider.send(" not in source
    assert "route_outbound(" in source
