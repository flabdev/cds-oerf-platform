"""Sandbox JOIN isolation, expiry, cap, and dashboard-only delivery."""

import threading
from datetime import UTC, datetime, timedelta

import pytest

from oerf.platform.channels.engagement import MemoryEngagementStore
from oerf.platform.channels.providers.fake import FakeProvider
from oerf.platform.channels.registry import DynamoPhoneRegistry, MemoryPhoneRegistry
from oerf.platform.channels.router import OutboundRequest, route_outbound
from oerf.platform.inbound.normalize import NormalizedMessage
from oerf.platform.sandbox.join import (
    DAILY_JOIN_CAP,
    INTRODUCTION,
    DynamoSandboxStore,
    MemorySandboxStore,
    handle_sandbox_message,
    reset_sandbox_tenant,
)
from tests.unit.support.fake_dynamo import FakeDynamoClient

START = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
DAY = "2026-10-20"
PHONE = "+910000019001"
MESSAGES = {"messages": {"driver_checkin": {"whatsapp": "Check {shipment_id}."}}}


@pytest.fixture(params=["memory", "dynamo"])
def stack(request):
    if request.param == "memory":
        return MemoryPhoneRegistry(), MemorySandboxStore()
    client = FakeDynamoClient()
    registry = DynamoPhoneRegistry(client, "routing", "tenant")
    return registry, DynamoSandboxStore(client, "routing", "tenant")


def test_join_creates_only_an_sbx_session(stack) -> None:
    registry, store = stack

    result = _join(registry, store, "m-1")

    assert result.outcome == "created"
    assert result.message == INTRODUCTION
    assert result.delivered is False
    assert result.simulated is False
    assert result.session is not None
    assert result.session.tenant_id == "sbx"
    assert result.session.consent_at == START
    assert result.session.consent_source == "whatsapp"
    assert result.session.expires_at == START + timedelta(hours=24)
    contact = registry.get_contact("sbx", result.session.contact_id)
    assert contact is not None
    assert contact.opt_in is True
    assert registry.get_contact("t1", result.session.contact_id) is None
    assert store.join_count(DAY) == 1


def test_a_production_number_is_rejected_without_leaking_its_tenant(stack) -> None:
    registry, store = stack
    registry.register_contact("t1", "C-T1-DRV", "driver", PHONE, opt_in=True)

    result = _join(registry, store, "m-1")

    assert result.outcome == "rejected"
    assert result.session is None
    assert "t1" not in result.message
    assert "C-T1-DRV" not in result.message
    assert registry.get_contact("t1", "C-T1-DRV").opt_in is True
    assert registry.resolve_sender(PHONE).matches[0].tenant_id == "t1"
    assert store.join_count(DAY) == 0
    assert store.sessions() == []


def test_duplicate_join_reuses_the_session_and_the_cap_slot(stack) -> None:
    registry, store = stack
    first = _join(registry, store, "m-1")
    second = _join(registry, store, "m-2")
    replay = _join(registry, store, "m-1")

    assert first.outcome == "created"
    assert second.outcome == "duplicate"
    assert second.session.session_id == first.session.session_id
    assert replay.outcome == "created"
    assert replay.session.session_id == first.session.session_id
    assert store.join_count(DAY) == 1


def test_session_expires_after_24_hours(stack) -> None:
    registry, store = stack
    first = _join(registry, store, "m-1")
    still = START + timedelta(hours=24) - timedelta(seconds=1)
    opened = START + timedelta(hours=24)

    active = _join(registry, store, "m-2", now=still)
    renewed = _join(registry, store, "m-3", now=opened)

    assert active.outcome == "duplicate"
    assert renewed.outcome == "created"
    assert renewed.session.session_id != first.session.session_id
    assert renewed.session.expires_at == opened + timedelta(hours=24)
    assert store.join_count(DAY) == 1
    assert store.join_count("2026-10-21") == 1


def test_the_21st_join_of_the_day_creates_nothing(stack) -> None:
    registry, store = stack
    for index in range(DAILY_JOIN_CAP):
        result = handle_sandbox_message(
            _inbound(f"m-{index}", f"+910000020{index:03d}"),
            registry=registry,
            store=store,
            now=START,
        )
        assert result.outcome == "created"

    blocked = handle_sandbox_message(
        _inbound("m-full", "+910000029999"),
        registry=registry,
        store=store,
        now=START,
    )

    assert blocked.outcome == "full"
    assert blocked.session is None
    assert registry.resolve_sender("+910000029999").unknown is True
    assert store.join_count(DAY) == DAILY_JOIN_CAP


def test_concurrent_joins_stop_at_the_daily_cap(stack) -> None:
    registry, store = stack
    barrier = threading.Barrier(25)
    outcomes: list[str] = []
    guard = threading.Lock()

    def attempt(index: int) -> None:
        barrier.wait()
        result = handle_sandbox_message(
            _inbound(f"race-{index}", f"+910000030{index:03d}"),
            registry=registry,
            store=store,
            now=START,
        )
        with guard:
            outcomes.append(result.outcome)

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(25)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("created") == DAILY_JOIN_CAP
    assert outcomes.count("full") == 5
    assert store.join_count(DAY) == DAILY_JOIN_CAP


def test_dashboard_only_mode_creates_nothing_and_does_not_send(stack) -> None:
    registry, store = stack
    store.set_mode("dashboard_only")
    provider = FakeProvider("whatsapp")

    result = _join(registry, store, "m-1")
    delivered = route_outbound(
        _request("sbx", "C-SBX-X", PHONE),
        registry=registry,
        engagements=MemoryEngagementStore(clock=lambda: START),
        providers={"whatsapp": provider},
        messages=MESSAGES,
        sandbox=store,
    )

    assert result.outcome == "dashboard_only"
    assert result.simulated is True
    assert result.delivered is False
    assert result.channel == "dashboard"
    assert result.session is None
    assert registry.resolve_sender(PHONE).unknown is True
    assert store.join_count(DAY) == 0
    assert delivered.delivered is False
    assert delivered.reason == "simulated"
    assert delivered.channel == "dashboard"
    assert provider.calls == []


def test_stop_blocks_a_later_send_and_a_later_join(stack) -> None:
    registry, store = stack
    provider = FakeProvider("whatsapp")
    opened = _join(registry, store, "m-1")
    stopped = handle_sandbox_message(
        _inbound("m-2", PHONE, "STOP"),
        registry=registry,
        store=store,
        now=START,
    )
    engagements = MemoryEngagementStore(clock=lambda: START)
    engagements.open_engagement(
        "sbx",
        "INC-SBX",
        "eng-1",
        "driver",
        "diagnose",
        "critical_field",
        "task-token-1",
        {"shipment_id": opened.session.shipment_id},
        contact_id=opened.session.contact_id,
        rung=1,
        deadline=START + timedelta(minutes=5),
    )
    sent = route_outbound(
        _request("sbx", opened.session.contact_id, PHONE, opened.session.shipment_id),
        registry=registry,
        engagements=engagements,
        providers={"whatsapp": provider},
        messages=MESSAGES,
        sandbox=store,
    )
    again = _join(registry, store, "m-3")

    assert stopped.outcome == "opted_out"
    assert stopped.delivered is False
    assert registry.get_contact("sbx", opened.session.contact_id).opt_in is False
    assert sent.reason == "OPT_OUT"
    assert provider.calls == []
    assert again.outcome == "opted_out"
    assert store.join_count(DAY) == 1


def test_stop_for_a_production_number_does_not_change_that_contact(stack) -> None:
    registry, store = stack
    registry.register_contact("t1", "C-T1-DRV", "driver", PHONE, opt_in=True)

    result = handle_sandbox_message(
        _inbound("m-1", PHONE, "unsubscribe"),
        registry=registry,
        store=store,
        now=START,
    )

    assert result.outcome == "ignored"
    assert registry.get_contact("t1", "C-T1-DRV").opt_in is True


def test_reset_removes_sbx_rows_and_keeps_the_platform_cap(stack) -> None:
    registry, store = stack
    registry.register_contact("t1", "C-T1-DRV", "driver", "+910000019111", opt_in=True)
    first = _join(registry, store, "m-1")
    handle_sandbox_message(
        _inbound("m-2", "+910000019002"),
        registry=registry,
        store=store,
        now=START,
    )
    store.set_mode("dashboard_only")

    reset_sandbox_tenant(registry, store)

    assert store.join_count(DAY) == 2
    assert store.mode() == "dashboard_only"
    assert store.sessions() == []
    assert registry.get_contact("sbx", first.session.contact_id) is None
    assert registry.resolve_sender(PHONE).unknown is True
    assert registry.get_contact("t1", "C-T1-DRV") is not None
    store.set_mode("join_phone")
    renewed = handle_sandbox_message(
        _inbound("m-3", PHONE),
        registry=registry,
        store=store,
        now=START,
    )
    assert renewed.outcome == "created"
    assert store.join_count(DAY) == 3


def test_join_phone_mode_can_send_after_the_contact_exists(stack) -> None:
    registry, store = stack
    provider = FakeProvider("whatsapp")
    opened = _join(registry, store, "m-1")
    engagements = MemoryEngagementStore(clock=lambda: START)
    engagements.open_engagement(
        "sbx",
        "INC-SBX",
        "eng-1",
        "driver",
        "diagnose",
        "critical_field",
        "task-token-1",
        {"shipment_id": opened.session.shipment_id},
        contact_id=opened.session.contact_id,
        rung=1,
        deadline=START + timedelta(minutes=5),
    )

    sent = route_outbound(
        _request("sbx", opened.session.contact_id, PHONE, opened.session.shipment_id),
        registry=registry,
        engagements=engagements,
        providers={"whatsapp": provider},
        messages=MESSAGES,
        sandbox=store,
    )

    assert sent.delivered is True
    assert sent.channel == "whatsapp"
    assert provider.calls


def _join(registry, store, message_id, sender=PHONE, body="JOIN", now=START):
    return handle_sandbox_message(
        _inbound(message_id, sender, body),
        registry=registry,
        store=store,
        now=now,
    )


def _inbound(message_id: str, sender: str, body: str = "JOIN") -> NormalizedMessage:
    return NormalizedMessage(
        provider_msg_id=message_id,
        channel="whatsapp",
        sender=sender,
        received_at="2026-10-20T09:00:00Z",
        text=body,
        type="text",
    )


def _request(
    tenant_id: str,
    contact_id: str,
    recipient: str,
    shipment_id: str = "SBX-0001",
) -> OutboundRequest:
    return OutboundRequest(
        tenant_id=tenant_id,
        incident_id="INC-SBX",
        engagement_id="eng-1",
        role="driver",
        template_id="driver_checkin",
        facts={"shipment_id": shipment_id},
        contact_id=contact_id,
        recipient=recipient,
    )
