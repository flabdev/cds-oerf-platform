"""Router, vision, and report logs keep ids and outcomes, not PII."""

import io
import json
import logging

import pytest

from oerf.platform.channels import router
from oerf.platform.evidence import vision
from oerf.platform.observability.logging import get_logger, log
from oerf.platform.report import generator

PHONE = "+910000000101"
EMAIL = "driver@example.com"
NAME = "Ada Example"
BODY = "pull over safely now"
TOKEN = "A" * 40
OBJECT_KEY = "tenants/t1/INC-LOG/photos/reefer_display.jpg"
_ROUTER = "oerf.platform.channels.router"


def _assert_clean(text: str) -> None:
    assert PHONE not in text
    assert EMAIL not in text
    assert NAME not in text
    assert BODY not in text
    assert TOKEN not in text
    assert OBJECT_KEY not in text
    assert "task_token" not in text


def test_redaction_drops_sensitive_keys_and_masks_values(caplog: pytest.LogCaptureFixture) -> None:
    logger = get_logger("oerf.test.redaction")
    with caplog.at_level(logging.INFO, logger="oerf.test.redaction"):
        log(
            logger,
            logging.INFO,
            f"call {PHONE} at {EMAIL} token {TOKEN}",
            tenant_id="t1",
            incident_id="INC-LOG",
            engagement_id="ENG-1",
            task_token=TOKEN,
            message=BODY,
            phone=PHONE,
            email=EMAIL,
            name=NAME,
            to=PHONE,
            recipient=EMAIL,
            body=BODY,
            text=BODY,
            e164=PHONE,
            address="1 Example Street",
            authorization="Bearer secret",
            key=OBJECT_KEY,
        )

    text = caplog.text
    _assert_clean(text)
    assert "Bearer secret" not in text
    assert "1 Example Street" not in text
    payload = json.loads(text[text.index("{") : text.rindex("}") + 1])
    assert payload["tenant_id"] == "t1"
    assert payload["incident_id"] == "INC-LOG"
    assert payload["engagement_id"] == "ENG-1"
    assert payload["msg"] == "call [redacted-phone] at [redacted-email] token [redacted-token]"
    assert "ts" in payload
    assert payload["level"] == "INFO"


def test_mock_router_sends_omit_recipient_and_message(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router, "USE_MOCK_WHATSAPP", True)
    monkeypatch.setattr(router, "USE_MOCK_SMS", True)
    monkeypatch.setattr(router, "USE_MOCK_EMAIL", True)
    event = {
        "tenant_id": "t1",
        "incident_id": "INC-LOG",
        "recipient_phone": PHONE,
        "recipient_email": EMAIL,
        "message": BODY,
        "subject": f"Alert for {NAME}",
        "task_token": TOKEN,
        "name": NAME,
        "channel_priority": ["WHATSAPP", "SMS", "EMAIL"],
    }

    with caplog.at_level(logging.INFO, logger=_ROUTER):
        router.handler(event, None)
        router.send_sms(PHONE, BODY)
        router.send_email(EMAIL, f"Alert for {NAME}", BODY)

    _assert_clean(caplog.text)
    assert "send_simulated" in caplog.text
    assert "route_complete" in caplog.text


def test_real_router_send_omits_recipient_message_and_errors(
    caplog: pytest.LogCaptureFixture, monkeypatch
) -> None:
    monkeypatch.setattr(router, "USE_MOCK_WHATSAPP", False)
    monkeypatch.setattr(router, "WHATSAPP_PHONE_NUMBER_ID", "phone-id-1")
    monkeypatch.setattr(router, "ROUTING_TABLE_NAME", "")

    calls = {"n": 0}

    class _Client:
        def send_whatsapp_message(self, **_kwargs: object) -> dict[str, str]:
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError(f"rejected {PHONE} {BODY} {TOKEN} {EMAIL} {NAME}")
            return {"messageId": "mid-1"}

    monkeypatch.setattr(router.boto3, "client", lambda *_args, **_kwargs: _Client())
    event = {
        "tenant_id": "t1",
        "incident_id": "INC-LOG",
        "recipient_phone": PHONE,
        "recipient_email": EMAIL,
        "message": BODY,
        "subject": f"Alert for {NAME}",
        "task_token": TOKEN,
        "channel_priority": ["WHATSAPP"],
    }

    with caplog.at_level(logging.INFO, logger=_ROUTER):
        sent = router.handler(event, None)
        failed = router.send_whatsapp(PHONE, BODY)

    assert sent["success"] is True
    assert failed["success"] is False
    assert PHONE in failed["error"]
    _assert_clean(caplog.text)
    assert "route_complete" in caplog.text


def test_vision_calls_omit_the_object_key_and_pii(
    caplog: pytest.LogCaptureFixture, monkeypatch
) -> None:
    event = {
        "bucket": "evidence-bucket",
        "key": OBJECT_KEY,
        "tenant_id": "t1",
        "incident_id": "INC-LOG",
        "phone": PHONE,
        "email": EMAIL,
        "name": NAME,
        "message": BODY,
        "task_token": TOKEN,
    }
    monkeypatch.setattr(vision, "USE_MOCK_BEDROCK", True)
    with caplog.at_level(logging.INFO, logger="oerf.platform.evidence.vision"):
        mock = vision.handler(event, None)

        monkeypatch.setattr(vision, "USE_MOCK_BEDROCK", False)
        monkeypatch.setattr(vision, "INCIDENT_TABLE_NAME", "")
        monkeypatch.setattr(vision, "GUARDRAIL_ID", "")

        class _Body:
            def read(self) -> bytes:
                return b"image-bytes"

        class _S3:
            def get_object(self, **kwargs: object) -> dict[str, _Body]:
                return {"Body": _Body()}

        class _Bedrock:
            def converse(self, **kwargs: object) -> dict[str, object]:
                return {"output": {"message": {"content": [{"text": '{"setpoint_temp_c": 4}'}]}}}

        monkeypatch.setattr(vision, "get_s3", lambda: _S3())
        monkeypatch.setattr(vision, "get_bedrock", lambda: _Bedrock())
        live = vision.handler(event, None)

        class _BrokenS3:
            def get_object(self, **kwargs: object) -> dict[str, str]:
                raise vision.ClientError(
                    {"Error": {"Code": "NoSuchKey", "Message": f"{OBJECT_KEY} {PHONE} {BODY}"}},
                    "GetObject",
                )

        monkeypatch.setattr(vision, "get_s3", lambda: _BrokenS3())
        failed = vision.handler(event, None)

    assert mock["success"] is True
    assert live["success"] is True
    assert failed["success"] is False
    _assert_clean(caplog.text)
    assert "evidence_analyzed" in caplog.text


def test_report_run_omits_the_object_key_and_pii(
    caplog: pytest.LogCaptureFixture, monkeypatch
) -> None:
    monkeypatch.setattr(generator, "INCIDENT_TABLE_NAME", "incidents")
    monkeypatch.setattr(generator, "REPORT_BUCKET_NAME", "")

    class _Table:
        def get_item(self, **kwargs: object) -> dict[str, dict[str, str]]:
            return {
                "Item": {
                    "shipment_id": "PH-1",
                    "status": "RESOLVED",
                    "diagnosis": BODY,
                    "phone": PHONE,
                    "email": EMAIL,
                    "name": NAME,
                    "task_token": TOKEN,
                }
            }

    class _Dynamo:
        def Table(self, _name: str) -> _Table:
            return _Table()

    monkeypatch.setattr(generator, "get_dynamodb", lambda: _Dynamo())
    with caplog.at_level(logging.INFO, logger="oerf.platform.report.generator"):
        result = generator.handler(
            {
                "tenant_id": "t1",
                "incident_id": "INC-LOG",
                "qa_email": EMAIL,
                "task_token": TOKEN,
                "name": NAME,
                "phone": PHONE,
                "message": BODY,
            },
            None,
        )

    assert result["success"] is True
    assert result["report_key"] not in caplog.text
    assert "s3://" not in caplog.text
    _assert_clean(caplog.text)
    assert "report_published" in caplog.text


def test_mask_covers_a_stream_formatter() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = get_logger("oerf.test.redaction.stream")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        log(logger, logging.INFO, f"to {PHONE}", phone=PHONE, message=BODY)
    finally:
        logger.removeHandler(handler)

    text = stream.getvalue()
    assert PHONE not in text
    assert BODY not in text
