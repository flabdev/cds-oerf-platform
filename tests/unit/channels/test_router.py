"""Fallback, opt-out, and correlation through the single outbound router."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from oerf.platform.channels.engagement import MemoryEngagementStore
from oerf.platform.channels.providers.aws import WhatsAppProvider
from oerf.platform.channels.providers.fake import FakeProvider
from oerf.platform.channels.registry import MemoryPhoneRegistry
from oerf.platform.channels.router import OutboundRequest, route_outbound
from oerf.platform.channels.templates import MissingFacts

MESSAGES = {
    "messages": {
        "driver_checkin": {
            "whatsapp": "Check {shipment_id} on WhatsApp.",
            "sms": "Temperature alert {shipment_id}.",
            "voice": "Temperature alert {shipment_id}.",
            "email": "Temperature alert {shipment_id}.",
            "rcs": "RCS alert {shipment_id}.",
        }
    }
}
DEADLINE = datetime(2026, 10, 20, 10, 21, tzinfo=UTC)


def test_whatsapp_failure_falls_back_to_sms_and_correlates() -> None:
    registry, engagements = _open()
    whatsapp = FakeProvider("whatsapp", succeed=False)
    sms = FakeProvider("sms")

    result = route_outbound(
        _request(),
        registry=registry,
        engagements=engagements,
        providers={"whatsapp": whatsapp, "sms": sms, "voice": FakeProvider("voice")},
        messages=MESSAGES,
    )

    assert result.delivered is True
    assert result.channel == "sms"
    assert result.provider_msg_id == "sms-1"
    assert [attempt.channel for attempt in result.attempts] == ["whatsapp", "sms"]
    assert result.attempts[0].delivered is False
    assert result.attempts[0].tenant_id == "t1"
    assert result.attempts[0].incident_id == "INC-000001"
    assert result.attempts[0].engagement_id == "eng-1"
    assert engagements.correlate("sms-1") is not None
    assert [item.engagement_id for item in engagements.for_address("sms", "+910000000101")] == [
        "eng-1"
    ]
    assert whatsapp.calls and sms.calls


def test_opted_out_contact_never_reaches_a_provider() -> None:
    registry, engagements = _open(opt_in=False)
    whatsapp = FakeProvider("whatsapp")

    result = route_outbound(
        _request(),
        registry=registry,
        engagements=engagements,
        providers={"whatsapp": whatsapp},
        messages=MESSAGES,
    )

    assert result.delivered is False
    assert result.reason == "OPT_OUT"
    assert result.attempts == []
    assert whatsapp.calls == []
    assert engagements.correlate("whatsapp-1") is None


def test_missing_facts_fail_before_a_provider_call() -> None:
    registry, engagements = _open()
    whatsapp = FakeProvider("whatsapp")
    request = _request().model_copy(update={"facts": {}})

    with pytest.raises(MissingFacts):
        route_outbound(
            request,
            registry=registry,
            engagements=engagements,
            providers={"whatsapp": whatsapp},
            messages=MESSAGES,
        )

    assert whatsapp.calls == []


def test_voice_body_directs_the_reply_to_whatsapp() -> None:
    registry, engagements = _open()
    voice = FakeProvider("voice")
    request = _request().model_copy(update={"channels": ("voice",)})

    result = route_outbound(
        request,
        registry=registry,
        engagements=engagements,
        providers={"voice": voice},
        messages=MESSAGES,
    )

    assert result.delivered is True
    assert result.channel == "voice"
    assert voice.calls[0][1].endswith(" Reply on WhatsApp.")


def test_rcs_is_skipped_unless_the_role_and_inbound_channel_allow_it() -> None:
    registry, engagements = _open()
    rcs = FakeProvider("rcs")
    sms = FakeProvider("sms")
    request = _request().model_copy(
        update={"channels": ("rcs", "sms"), "rcs_roles": ("driver",), "inbound_rcs": False}
    )

    result = route_outbound(
        request,
        registry=registry,
        engagements=engagements,
        providers={"rcs": rcs, "sms": sms},
        messages=MESSAGES,
    )

    assert rcs.calls == []
    assert result.attempts[0].reason == "rcs_unavailable"
    assert result.channel == "sms"


def test_rcs_is_used_for_a_configured_role_when_inbound_rcs_is_supported() -> None:
    registry, engagements = _open()
    rcs = FakeProvider("rcs")
    request = _request().model_copy(
        update={"channels": ("rcs",), "rcs_roles": ("driver",), "inbound_rcs": True}
    )

    result = route_outbound(
        request,
        registry=registry,
        engagements=engagements,
        providers={"rcs": rcs},
        messages=MESSAGES,
    )

    assert result.channel == "rcs"
    assert rcs.calls


def test_whatsapp_adapter_uses_the_simulated_sender(monkeypatch) -> None:
    from oerf.platform.channels import router

    monkeypatch.setattr(router, "USE_MOCK_WHATSAPP", True)

    result = WhatsAppProvider().send("+910000000101", "hello", "driver_checkin")

    assert result.delivered is False
    assert result.simulated is True
    assert result.reason == "simulated"
    assert result.provider_msg_id


def test_a_simulated_channel_does_not_fall_through_or_count_as_delivered() -> None:
    registry, engagements = _open()
    whatsapp = FakeProvider("whatsapp", simulated=True)
    sms = FakeProvider("sms")

    result = route_outbound(
        _request(),
        registry=registry,
        engagements=engagements,
        providers={"whatsapp": whatsapp, "sms": sms},
        messages=MESSAGES,
    )

    assert result.delivered is False
    assert result.simulated is True
    assert result.reason == "simulated"
    assert result.channel == "whatsapp"
    assert whatsapp.calls
    assert sms.calls == []
    assert engagements.correlate("whatsapp-1") is None


def test_provider_sdks_are_only_called_from_the_router() -> None:
    """Sends stay in the router. Inbound may call GetWhatsAppMessageMedia only."""

    offenders: list[str] = []
    for path in Path("oerf").rglob("*.py"):
        if path.name == "router.py":
            continue
        source = path.read_text(encoding="utf-8")
        inbound_media = path.as_posix() == "oerf/platform/inbound/handler.py"
        if "sns.publish" in source or ".send_email(" in source or "send_whatsapp_message" in source:
            offenders.append(str(path))
        if "socialmessaging" in source and not inbound_media:
            offenders.append(str(path))

    assert offenders == []


def _open(*, opt_in: bool = True):
    registry = MemoryPhoneRegistry()
    registry.register_contact(
        "t1", "C-T1-DRV", "driver", "+910000000101", opt_in=opt_in
    )
    engagements = MemoryEngagementStore(clock=lambda: datetime(2026, 10, 20, 10, 16, tzinfo=UTC))
    engagements.open_engagement(
        "t1",
        "INC-000001",
        "eng-1",
        "driver",
        "diagnose",
        "critical_field",
        "task-token-1",
        {"shipment_id": "PH-2841"},
        contact_id="C-T1-DRV",
        rung=1,
        deadline=DEADLINE,
    )
    return registry, engagements


def _request() -> OutboundRequest:
    return OutboundRequest(
        tenant_id="t1",
        incident_id="INC-000001",
        engagement_id="eng-1",
        role="driver",
        template_id="driver_checkin",
        facts={"shipment_id": "PH-2841"},
        contact_id="C-T1-DRV",
        recipient="+910000000101",
        email="driver@example.com",
    )
