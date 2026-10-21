"""Vision handler: mock schema stays put, and the live path calls Converse."""

import json

import pytest
from botocore.exceptions import ClientError

from oerf.platform.evidence import vision

_SCHEMA = {
    "setpoint_temp_c",
    "ambient_temp_c",
    "fault_codes",
    "alarm_status",
    "door_alarm_status",
    "power_status",
    "confidence_score",
    "display_readout_summary",
}

_EVENT = {
    "bucket": "evidence-bucket",
    "key": "tenants/t1/photos/reefer_display.jpg",
    "tenant_id": "t1",
    "incident_id": "INC-1",
}


def test_mock_path_keeps_the_output_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vision, "USE_MOCK_BEDROCK", True)

    result = vision.handler(_EVENT, None)

    assert result["success"] is True
    assert result["source"] == "mock"
    assert set(result["analysis"]) == _SCHEMA
    assert result["analysis"]["fault_codes"] == ["AL02"]
    assert result["analysis"]["setpoint_temp_c"] == 4.0


def test_converse_uses_the_configured_model_and_parses_the_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analysis = {
        "setpoint_temp_c": 4.0,
        "ambient_temp_c": 9.5,
        "fault_codes": ["AL02"],
        "alarm_status": "ACTIVE",
        "door_alarm_status": "CLOSED",
        "power_status": "RUNNING",
        "confidence_score": 0.8,
        "display_readout_summary": "display",
    }
    calls: dict = {}

    class _Body:
        def read(self) -> bytes:
            return b"jpeg-bytes"

    class _S3:
        def get_object(self, **kwargs):
            calls["s3"] = kwargs
            return {"Body": _Body()}

    class _Bedrock:
        def converse(self, **kwargs):
            calls["converse"] = kwargs
            return {"output": {"message": {"content": [{"text": json.dumps(analysis)}]}}}

        def invoke_model(self, **kwargs):
            raise AssertionError(kwargs)

    monkeypatch.setattr(vision, "USE_MOCK_BEDROCK", False)
    monkeypatch.setattr(vision, "BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
    monkeypatch.setattr(vision, "INCIDENT_TABLE_NAME", "")
    monkeypatch.setattr(vision, "get_s3", lambda: _S3())
    monkeypatch.setattr(vision, "get_bedrock", lambda: _Bedrock())

    result = vision.handler(_EVENT, None)

    assert result == {"success": True, "analysis": analysis}
    assert calls["converse"]["modelId"] == "amazon.nova-lite-v1:0"
    image = calls["converse"]["messages"][0]["content"][0]["image"]
    assert image["format"] == "jpeg"
    assert image["source"]["bytes"] == b"jpeg-bytes"
    assert "anthropic.claude" not in json.dumps(calls["converse"]["system"])
    assert "setpoint_temp_c" in calls["converse"]["system"][0]["text"]


def test_converse_failure_does_not_call_a_second_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Body:
        def read(self) -> bytes:
            return b"jpeg-bytes"

    class _S3:
        def get_object(self, **kwargs):
            return {"Body": _Body()}

    class _Bedrock:
        def converse(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ValidationException", "Message": "denied"}},
                "Converse",
            )

        def invoke_model(self, **kwargs):
            raise AssertionError(kwargs)

    monkeypatch.setattr(vision, "USE_MOCK_BEDROCK", False)
    monkeypatch.setattr(vision, "BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
    monkeypatch.setattr(vision, "get_s3", lambda: _S3())
    monkeypatch.setattr(vision, "get_bedrock", lambda: _Bedrock())

    result = vision.handler(_EVENT, None)

    assert result["success"] is False
    assert result["fallback_required"] is True
    assert "claude" not in result["error"].lower()
