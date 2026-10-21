"""Engagement delivery facade tests; provider access stays behind the router."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from oerf.platform.channels.engagement import MemoryEngagementStore
from oerf.platform.channels.engagement_workflow import send_rung
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


def test_no_provider_sdk_is_used_outside_the_router() -> None:
    source = Path("oerf/platform/channels/engagement_workflow.py").read_text(encoding="utf-8")
    assert "provider.send(" not in source
    assert "route_outbound(" in source
