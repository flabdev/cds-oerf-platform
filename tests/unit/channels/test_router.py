"""Fallback, opt-out, and correlation through the single outbound router."""

import json
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
    assert result.provider_msg_id == "whatsapp-1"
    correlated = engagements.correlate("whatsapp-1")
    assert correlated is not None
    assert correlated.status == "WAITING"
    assert correlated.task_token == "task-token-1"
    waiting = engagements.for_address("whatsapp", "+910000000101")
    assert [item.engagement_id for item in waiting] == ["eng-1"]


def test_a_live_send_stores_the_api_id_and_the_wamid() -> None:
    from oerf.platform.channels.providers.base import ProviderResult

    class _Both:
        def send(
            self,
            recipient,
            body,
            template_id,
            parameters=(),
            *,
            incident_id=None,
            report_id=None,
        ):
            del recipient, body, template_id, parameters, incident_id, report_id
            return ProviderResult(
                delivered=True,
                provider_msg_id="aws-mid-1",
                provider_msg_ids=("aws-mid-1", "wamid.HBgLstored"),
            )

    registry, engagements = _open()
    result = route_outbound(
        _request(),
        registry=registry,
        engagements=engagements,
        providers={"whatsapp": _Both()},
        messages=MESSAGES,
    )

    assert result.delivered is True
    assert result.provider_msg_id == "aws-mid-1"
    assert engagements.correlate("aws-mid-1") is not None
    quoted = engagements.correlate("wamid.HBgLstored")
    assert quoted is not None and quoted.engagement_id == "eng-1"


def test_inline_whatsapp_text_uses_the_router_without_template_rendering() -> None:
    registry, engagements = _open()
    whatsapp = FakeProvider("whatsapp")
    request = _request().model_copy(
        update={
            "template_id": "One final question?",
            "facts": {},
            "inline_text": True,
            "channels": ("whatsapp",),
        }
    )

    result = route_outbound(
        request,
        registry=registry,
        engagements=engagements,
        providers={"whatsapp": whatsapp},
        messages=MESSAGES,
    )

    assert result.delivered is True
    assert whatsapp.calls[0] == ("+910000000101", "One final question?", "")


def test_live_whatsapp_template_keeps_parameter_order() -> None:
    from oerf.platform.channels.router import _whatsapp_payload

    payload = _whatsapp_payload("+910000000101", "rendered", "driver_checkin", ("PH-2841", "8.1"))
    assert payload["type"] == "template"
    assert payload["template"]["name"] == "oerf_driver_checkin"
    parameters = payload["template"]["components"][0]["parameters"]
    assert [item["text"] for item in parameters] == ["PH-2841", "8.1"]
    prefixed = _whatsapp_payload("+910000000101", "rendered", "oerf_driver_checkin", ())
    assert prefixed["template"]["name"] == "oerf_driver_checkin"
    assert "components" not in prefixed["template"]
    assert all(item["type"] != "button" for item in payload["template"]["components"])


def test_dev_url_templates_use_dev_names_only_when_the_flag_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from oerf.platform.channels.router import _whatsapp_payload

    monkeypatch.setenv("WHATSAPP_STAGE_URL_TEMPLATES", "true")
    escalation = _whatsapp_payload(
        "+910000000101",
        "rendered",
        "ops_escalation_options",
        ("PH-2841", "no facility"),
        incident_id="INC-000001",
    )
    report = _whatsapp_payload(
        "+910000000101",
        "rendered",
        "qa_report_ready",
        ("PH-2841",),
        report_id="RPT-000001",
    )
    other = _whatsapp_payload("+910000000101", "rendered", "driver_checkin", ("PH-2841",))
    assert escalation["template"]["name"] == "oerf_dev_ops_escalation_options"
    assert report["template"]["name"] == "oerf_dev_qa_report_ready"
    assert other["template"]["name"] == "oerf_driver_checkin"


def test_url_templates_keep_todays_names_when_the_flag_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from oerf.platform.channels.router import _whatsapp_payload

    monkeypatch.delenv("WHATSAPP_STAGE_URL_TEMPLATES", raising=False)
    for logical, deployed in (
        ("ops_escalation_options", "oerf_ops_escalation_options"),
        ("qa_report_ready", "oerf_qa_report_ready"),
        ("driver_checkin", "oerf_driver_checkin"),
    ):
        payload = _whatsapp_payload(
            "+910000000101",
            "rendered",
            logical,
            ("PH-2841",),
            incident_id="INC-000001",
            report_id="RPT-000001",
        )
        assert payload["template"]["name"] == deployed
    monkeypatch.setenv("WHATSAPP_STAGE_URL_TEMPLATES", "false")
    main = _whatsapp_payload(
        "+910000000101",
        "rendered",
        "ops_escalation_options",
        (),
        incident_id="INC-000001",
    )
    assert main["template"]["name"] == "oerf_ops_escalation_options"


def test_url_button_component_carries_only_the_safe_id() -> None:
    from oerf.platform.channels.router import _whatsapp_payload

    escalation = _whatsapp_payload(
        "+910000000101",
        "body-do-not-log",
        "ops_escalation_options",
        ("PH-2841", "reason"),
        incident_id="INC-000001",
    )
    report = _whatsapp_payload(
        "+910000000101",
        "body-do-not-log",
        "qa_report_ready",
        ("PH-2841",),
        report_id="RPT-000001",
    )
    checkin = _whatsapp_payload(
        "+910000000101",
        "body-do-not-log",
        "driver_checkin",
        ("PH-2841",),
        incident_id="INC-000001",
    )
    assert _button(escalation) == {
        "type": "button",
        "sub_type": "url",
        "index": "1",
        "parameters": [{"type": "text", "text": "INC-000001"}],
    }
    assert _button(report)["index"] == "3"
    assert _button(report)["parameters"] == [{"type": "text", "text": "RPT-000001"}]
    assert _button(checkin) is None
    for unsafe in ("INC 1", "t1/secret", "+910000000101", "id#frag", ""):
        with pytest.raises(ValueError, match="url-safe"):
            _whatsapp_payload(
                "+910000000101",
                "body-do-not-log",
                "ops_escalation_options",
                (),
                incident_id=unsafe,
            )


def test_template_send_logs_the_name_and_stage_only(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from oerf.platform.channels.router import _whatsapp_payload

    monkeypatch.setenv("WHATSAPP_STAGE_URL_TEMPLATES", "true")
    _whatsapp_payload(
        "+910000000101",
        "body-do-not-log",
        "ops_escalation_options",
        ("PH-2841",),
        incident_id="INC-000001",
    )
    text = capsys.readouterr().out
    lines = [json.loads(line) for line in text.splitlines() if line.startswith("{")]
    logged = next(line for line in lines if line["msg"] == "whatsapp_template")
    assert logged["template"] == "oerf_dev_ops_escalation_options"
    assert logged["stage"] == "dev"
    assert "+910000000101" not in text
    assert "body-do-not-log" not in text
    assert "PH-2841" not in json.dumps(logged)


def test_route_outbound_passes_the_incident_and_report_ids() -> None:
    from oerf.platform.channels.providers.base import ProviderResult

    seen: dict[str, tuple[str | None, str | None, str]] = {}

    class _Probe:
        def send(
            self,
            recipient: str,
            body: str,
            template_id: str,
            parameters: tuple[str, ...] = (),
            *,
            incident_id: str | None = None,
            report_id: str | None = None,
        ) -> ProviderResult:
            del recipient, body, parameters
            seen["ids"] = (incident_id, report_id, template_id)
            return ProviderResult(delivered=True, provider_msg_id="wa-1")

    registry, engagements = _open()
    request = _request().model_copy(
        update={"facts": {"shipment_id": "PH-2841", "report_id": "RPT-000001"}}
    )
    route_outbound(
        request,
        registry=registry,
        engagements=engagements,
        providers={"whatsapp": _Probe()},
        messages=MESSAGES,
    )
    assert seen["ids"] == ("INC-000001", "RPT-000001", "driver_checkin")


_URL_MESSAGES = {
    "messages": {
        "ops_escalation_options": {
            "whatsapp": "body-do-not-log",
            "sms": "sms-escalation",
        },
        "qa_report_ready": {
            "whatsapp": "body-do-not-log",
            "sms": "sms-report",
        },
    }
}


def _live_whatsapp(monkeypatch: pytest.MonkeyPatch, sent: list[dict]) -> None:
    """A live WhatsApp send that records the API payload and does not call AWS."""

    from oerf.platform.channels import phone_id, router

    monkeypatch.setattr(router, "USE_MOCK_WHATSAPP", False)
    monkeypatch.setattr(router, "WA_PHONE_NUMBER_ID_PARAM", "")
    monkeypatch.setattr(router, "WHATSAPP_PHONE_NUMBER_ID", "pn-test")
    monkeypatch.setattr(phone_id, "_cached_phone_number_id", phone_id._PHONE_UNSET)

    class _Social:
        def send_whatsapp_message(self, **kwargs: object) -> dict[str, str]:
            raw = kwargs["message"]
            sent.append(json.loads(raw))
            return {"messageId": "mid-1"}

    monkeypatch.setattr(router.boto3, "client", lambda *_args, **_kwargs: _Social())


def _url_request(template_id: str, incident_id: str, facts: dict) -> OutboundRequest:
    return _request().model_copy(
        update={
            "template_id": template_id,
            "incident_id": incident_id,
            "facts": facts,
            "channels": ("whatsapp", "sms"),
        }
    )


@pytest.mark.parametrize(
    ("template_id", "incident_id", "facts", "suffix"),
    [
        ("ops_escalation_options", "", {}, ""),
        ("qa_report_ready", "INC-000001", {}, ""),
        (
            "qa_report_ready",
            "INC-000001",
            {"report_id": "bad/report"},
            "bad/report",
        ),
    ],
)
def test_invalid_url_button_falls_through_when_the_stage_flag_is_on(
    template_id: str,
    incident_id: str,
    facts: dict,
    suffix: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("WHATSAPP_STAGE_URL_TEMPLATES", "true")
    sent: list[dict] = []
    _live_whatsapp(monkeypatch, sent)
    registry, engagements = _open()
    sms = FakeProvider("sms")

    result = route_outbound(
        _url_request(template_id, incident_id, facts),
        registry=registry,
        engagements=engagements,
        providers={"whatsapp": WhatsAppProvider(), "sms": sms},
        messages=_URL_MESSAGES,
        correlate=False,
    )

    assert result.delivered is True
    assert result.channel == "sms"
    assert [(item.channel, item.delivered, item.reason) for item in result.attempts] == [
        ("whatsapp", False, "invalid_button_data"),
        ("sms", True, None),
    ]
    assert sms.calls
    assert sent == []
    text = capsys.readouterr().out
    assert '"msg":"invalid_button_data"' in text
    assert "+910000000101" not in text
    assert "body-do-not-log" not in text
    if suffix:
        assert suffix not in text


def test_valid_url_button_is_still_sent_when_the_stage_flag_is_on(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("WHATSAPP_STAGE_URL_TEMPLATES", "true")
    sent: list[dict] = []
    _live_whatsapp(monkeypatch, sent)
    registry, engagements = _open()
    sms = FakeProvider("sms")
    cases = (
        ("ops_escalation_options", {}, "1", "INC-000001", "oerf_dev_ops_escalation_options"),
        (
            "qa_report_ready",
            {"report_id": "RPT-000001"},
            "3",
            "RPT-000001",
            "oerf_dev_qa_report_ready",
        ),
    )

    for template_id, facts, index, suffix, name in cases:
        sent.clear()
        result = route_outbound(
            _url_request(template_id, "INC-000001", facts),
            registry=registry,
            engagements=engagements,
            providers={"whatsapp": WhatsAppProvider(), "sms": sms},
            messages=_URL_MESSAGES,
            correlate=False,
        )
        assert result.delivered is True
        assert result.channel == "whatsapp"
        assert result.attempts[0].reason is None
        payload = sent[0]
        assert payload["template"]["name"] == name
        assert _button(payload) == {
            "type": "button",
            "sub_type": "url",
            "index": index,
            "parameters": [{"type": "text", "text": suffix}],
        }

    assert sms.calls == []
    text = capsys.readouterr().out
    assert "+910000000101" not in text
    assert "body-do-not-log" not in text
    assert "INC-000001" not in text
    assert "RPT-000001" not in text


@pytest.mark.parametrize("flag", [None, "false"])
@pytest.mark.parametrize(
    ("template_id", "incident_id", "facts", "suffix"),
    [
        ("ops_escalation_options", "", {}, ""),
        ("qa_report_ready", "INC-000001", {}, ""),
        (
            "qa_report_ready",
            "INC-000001",
            {"report_id": "bad/report"},
            "bad/report",
        ),
    ],
)
def test_invalid_url_button_still_stops_routing_when_the_flag_is_off(
    flag: str | None,
    template_id: str,
    incident_id: str,
    facts: dict,
    suffix: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if flag is None:
        monkeypatch.delenv("WHATSAPP_STAGE_URL_TEMPLATES", raising=False)
    else:
        monkeypatch.setenv("WHATSAPP_STAGE_URL_TEMPLATES", flag)
    sent: list[dict] = []
    _live_whatsapp(monkeypatch, sent)
    registry, engagements = _open()
    sms = FakeProvider("sms")

    with pytest.raises(ValueError, match="url-safe") as caught:
        route_outbound(
            _url_request(template_id, incident_id, facts),
            registry=registry,
            engagements=engagements,
            providers={"whatsapp": WhatsAppProvider(), "sms": sms},
            messages=_URL_MESSAGES,
            correlate=False,
        )

    assert sms.calls == []
    assert sent == []
    assert "+910000000101" not in str(caught.value)
    assert "body-do-not-log" not in str(caught.value)
    if suffix:
        assert suffix not in str(caught.value)
    assert "invalid_button_data" not in capsys.readouterr().out


@pytest.mark.parametrize("flag", [None, "false"])
def test_valid_url_button_is_unchanged_when_the_flag_is_off(
    flag: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    if flag is None:
        monkeypatch.delenv("WHATSAPP_STAGE_URL_TEMPLATES", raising=False)
    else:
        monkeypatch.setenv("WHATSAPP_STAGE_URL_TEMPLATES", flag)
    sent: list[dict] = []
    _live_whatsapp(monkeypatch, sent)
    registry, engagements = _open()
    sms = FakeProvider("sms")

    result = route_outbound(
        _url_request("ops_escalation_options", "INC-000001", {}),
        registry=registry,
        engagements=engagements,
        providers={"whatsapp": WhatsAppProvider(), "sms": sms},
        messages=_URL_MESSAGES,
        correlate=False,
    )

    assert result.delivered is True
    assert result.channel == "whatsapp"
    assert sms.calls == []
    assert sent[0]["template"]["name"] == "oerf_ops_escalation_options"
    assert _button(sent[0])["parameters"] == [{"type": "text", "text": "INC-000001"}]


def test_another_value_error_still_stops_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHATSAPP_STAGE_URL_TEMPLATES", "true")

    class _Boom:
        def send(
            self,
            recipient: str,
            body: str,
            template_id: str,
            parameters: tuple[str, ...] = (),
            *,
            incident_id: str | None = None,
            report_id: str | None = None,
        ):
            del recipient, body, template_id, parameters, incident_id, report_id
            raise ValueError("other failure")

    registry, engagements = _open()
    sms = FakeProvider("sms")
    request = _request().model_copy(update={"channels": ("whatsapp", "sms")})

    with pytest.raises(ValueError, match="other failure"):
        route_outbound(
            request,
            registry=registry,
            engagements=engagements,
            providers={"whatsapp": _Boom(), "sms": sms},
            messages=MESSAGES,
        )

    assert sms.calls == []


def _button(payload: dict) -> dict | None:
    components = payload["template"].get("components", [])
    found = [item for item in components if item["type"] == "button"]
    assert len(found) <= 1
    return found[0] if found else None


def test_an_engagement_send_does_not_use_the_legacy_correlation(monkeypatch) -> None:
    from oerf.platform.channels import engagement_send, router

    def forbid(*_args, **_kwargs):
        raise AssertionError("legacy correlation")

    monkeypatch.setattr(router, "record_outbound_message", forbid)
    monkeypatch.setattr(engagement_send, "send_engagement", lambda event, ports=None: {"ok": True})

    assert router.handler({"step_name": "EngagementSendViaRouter"}, None) == {"ok": True}


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
    registry.register_contact("t1", "C-T1-DRV", "driver", "+910000000101", opt_in=opt_in)
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
