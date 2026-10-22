"""Vision handler: mock schema stays put, and the live path calls Converse."""

import json

import pytest
from botocore.exceptions import ClientError

from oerf.platform.evidence import policy, vision

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

_READING = {
    "setpoint_temp_c": 4.0,
    "ambient_temp_c": 9.5,
    "fault_codes": ["AL02"],
    "alarm_status": "ACTIVE",
    "door_alarm_status": "CLOSED",
    "power_status": "RUNNING",
    "confidence_score": 0.8,
    "display_readout_summary": "display",
}


def test_mock_path_keeps_the_output_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vision, "USE_MOCK_BEDROCK", True)
    monkeypatch.setattr(vision, "TENANT_TABLE_NAME", "")

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
    monkeypatch.setattr(vision, "GUARDRAIL_ID", "")
    monkeypatch.setattr(vision, "GUARDRAIL_VERSION", "")
    monkeypatch.setattr(vision, "INCIDENT_TABLE_NAME", "")
    monkeypatch.setattr(vision, "TENANT_TABLE_NAME", "")
    monkeypatch.setattr(vision, "get_s3", lambda: _S3())
    monkeypatch.setattr(vision, "get_bedrock", lambda: _Bedrock())

    result = vision.handler(_EVENT, None)

    assert result["success"] is True
    assert result["analysis"] == analysis
    assert result["verification"]["unverified_fault_codes"] == ["AL02"]
    assert result["requires_confirmation"] is True
    assert calls["converse"]["modelId"] == "amazon.nova-lite-v1:0"
    image = calls["converse"]["messages"][0]["content"][0]["image"]
    assert image["format"] == "jpeg"
    assert image["source"]["bytes"] == b"jpeg-bytes"
    assert "anthropic.claude" not in json.dumps(calls["converse"]["system"])
    assert "setpoint_temp_c" in calls["converse"]["system"][0]["text"]
    assert "guardrailConfig" not in calls["converse"]


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
    monkeypatch.setattr(vision, "GUARDRAIL_ID", "")
    monkeypatch.setattr(vision, "get_s3", lambda: _S3())
    monkeypatch.setattr(vision, "get_bedrock", lambda: _Bedrock())

    result = vision.handler(_EVENT, None)

    assert result["success"] is False
    assert result["fallback_required"] is True
    assert "claude" not in result["error"].lower()


def test_missing_tenant_does_not_read_s3_or_write_dynamodb(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vision, "INCIDENT_TABLE_NAME", "incidents")
    monkeypatch.setattr(vision, "get_s3", lambda: (_ for _ in ()).throw(AssertionError("s3")))
    monkeypatch.setattr(
        vision, "get_dynamodb", lambda: (_ for _ in ()).throw(AssertionError("dynamodb"))
    )

    for mock in (True, False):
        monkeypatch.setattr(vision, "USE_MOCK_BEDROCK", mock)
        result = vision.handler({"bucket": "evidence", "key": "photos/display.jpg"}, None)
        assert result["success"] is False
        assert result["error"] == "tenant_required"


def _live(monkeypatch: pytest.MonkeyPatch, text: str, *, response: dict | None = None) -> dict:
    """Run the live path over a fake S3 object and model answer."""

    class _Body:
        def read(self) -> bytes:
            return b"jpeg-bytes"

    class _S3:
        def get_object(self, **kwargs):
            return {"Body": _Body()}

    class _Bedrock:
        def converse(self, **kwargs):
            return response or {"output": {"message": {"content": [{"text": text}]}}}

    monkeypatch.setattr(vision, "USE_MOCK_BEDROCK", False)
    monkeypatch.setattr(vision, "GUARDRAIL_ID", "")
    monkeypatch.setattr(vision, "GUARDRAIL_VERSION", "")
    monkeypatch.setattr(vision, "INCIDENT_TABLE_NAME", "")
    monkeypatch.setattr(vision, "TENANT_TABLE_NAME", "")
    monkeypatch.setattr(vision, "get_s3", lambda: _S3())
    monkeypatch.setattr(vision, "get_bedrock", lambda: _Bedrock())
    return vision.handler(_EVENT, None)


def test_a_key_outside_the_tenant_prefix_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vision, "USE_MOCK_BEDROCK", True)
    monkeypatch.setattr(vision, "TENANT_TABLE_NAME", "")

    result = vision.handler({**_EVENT, "key": "tenants/t2/photos/reefer_display.jpg"}, None)

    assert result["success"] is False
    assert result["fallback_required"] is True
    assert "t2" not in result["error"]


def test_a_well_formed_reading_is_validated_and_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored: dict = {}

    class _Table:
        def update_item(self, **kwargs):
            stored.update(kwargs)

    class _Dynamo:
        def Table(self, name: str) -> _Table:
            stored["table"] = name
            return _Table()

    monkeypatch.setattr(vision, "get_dynamodb", lambda: _Dynamo())

    result = _live(monkeypatch, json.dumps(_READING))
    monkeypatch.setattr(vision, "INCIDENT_TABLE_NAME", "incident-table")
    vision.handler(_EVENT, None)

    assert result["analysis"]["fault_codes"] == ["AL02"]
    assert stored["table"] == "incident-table"
    assert stored["Key"] == {"pk": "TENANT#t1", "sk": "INCIDENT#INC-1"}
    assert stored["ExpressionAttributeValues"][":ea"]["fault_codes"] == ["AL02"]
    assert ":ev" in stored["ExpressionAttributeValues"]
    assert stored["ExpressionAttributeValues"][":k"] == _EVENT["key"]


def test_the_model_call_trace_carries_only_sanitized_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict] = []

    def _capture(metadata, *, segment=None):
        recorded.append(dict(metadata))
        return dict(metadata)

    monkeypatch.setattr(vision, "record_invocation", _capture)
    response = {
        "output": {"message": {"content": [{"text": json.dumps(_READING)}]}},
        "usage": {"inputTokens": 120, "outputTokens": 40},
        "metadata": {"stopReason": "end_turn"},
    }

    _live(monkeypatch, json.dumps(_READING), response=response)

    assert recorded == [
        {
            "model_id": vision.BEDROCK_MODEL_ID,
            "latency_ms": recorded[0]["latency_ms"],
            "input_tokens": 120,
            "output_tokens": 40,
            "guardrail_action": "NONE",
            "outcome": "SUCCESS",
        }
    ]
    assert isinstance(recorded[0]["latency_ms"], int)
    assert "display" not in json.dumps(recorded)


def test_a_guardrail_intervention_is_recorded_as_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict] = []
    monkeypatch.setattr(
        vision,
        "record_invocation",
        lambda metadata, segment=None: recorded.append(dict(metadata)),
    )
    response = {
        "output": {"message": {"content": [{"text": json.dumps(_READING)}]}},
        "usage": {"inputTokens": 10, "outputTokens": 2},
        "metadata": {"stopReason": "guardrail_intervened"},
    }

    _live(monkeypatch, json.dumps(_READING), response=response)

    assert recorded[0]["guardrail_action"] == "BLOCKED"


def test_a_failed_call_is_traced_as_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[dict] = []
    monkeypatch.setattr(
        vision,
        "record_invocation",
        lambda metadata, segment=None: recorded.append(dict(metadata)),
    )

    result = _live(monkeypatch, "not json at all")

    assert result["success"] is False
    assert recorded == [
        {
            "model_id": vision.BEDROCK_MODEL_ID,
            "latency_ms": recorded[0]["latency_ms"],
            "input_tokens": 0,
            "output_tokens": 0,
            "guardrail_action": "NONE",
            "outcome": "ERROR",
        }
    ]


def test_the_tenant_floor_and_catalog_govern_the_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant, config = policy.memory_readers(
        tenants={"t1": {"tenant_id": "t1"}},
        codes={"t1": {"AL02"}},
        configs={"t1": {"evidence": {"min_confidence": 0.9}}},
    )
    monkeypatch.setattr(vision, "USE_MOCK_BEDROCK", True)
    monkeypatch.setattr(vision, "TENANT_TABLE_NAME", "")
    monkeypatch.setattr(
        vision,
        "_policy",
        lambda tenant_id: policy.resolve_policy(tenant_id, tenant=tenant, config=config),
    )

    result = vision.handler(_EVENT, None)

    assert result["analysis"]["fault_codes"] == ["AL02"]
    assert result["verification"]["min_confidence"] == 0.9
    # The mock reading is 0.95, so it clears the raised floor, and AL02 is known.
    assert result["verification"]["requires_confirmation"] is False


def test_tenant_key_accepts_only_the_tenant_prefix() -> None:
    assert vision.tenant_key("t1", "tenants/t1/photos/a.jpg") is True
    assert vision.tenant_key("t1", "tenants/t11/photos/a.jpg") is False
    assert vision.tenant_key("t1", "photos/a.jpg") is False


def test_guardrail_config_is_sent_only_on_the_live_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict = {}

    class _Body:
        def read(self) -> bytes:
            return b"jpeg-bytes"

    class _S3:
        def get_object(self, **kwargs):
            return {"Body": _Body()}

    class _Bedrock:
        def converse(self, **kwargs):
            calls["converse"] = kwargs
            return {"output": {"message": {"content": [{"text": "{}"}]}}}

    monkeypatch.setattr(vision, "GUARDRAIL_ID", "guardrail-1")
    monkeypatch.setattr(vision, "GUARDRAIL_VERSION", "1")
    monkeypatch.setattr(vision, "TENANT_TABLE_NAME", "")
    monkeypatch.setattr(vision, "get_s3", lambda: _S3())
    monkeypatch.setattr(vision, "get_bedrock", lambda: _Bedrock())

    monkeypatch.setattr(vision, "USE_MOCK_BEDROCK", True)
    mocked = vision.handler(_EVENT, None)
    assert mocked["source"] == "mock"
    assert "converse" not in calls

    monkeypatch.setattr(vision, "USE_MOCK_BEDROCK", False)
    monkeypatch.setattr(vision, "INCIDENT_TABLE_NAME", "")
    vision.handler(_EVENT, None)
    assert calls["converse"]["guardrailConfig"] == {
        "guardrailIdentifier": "guardrail-1",
        "guardrailVersion": "1",
        "trace": "disabled",
    }
