"""Multimodal Bedrock vision evidence extractor (ADR-006, ADR-020).

Reads a reefer display photo from the evidence bucket and asks the configured
Bedrock model, default Amazon Nova Lite, for the telemetry JSON below.

The reading is advisory. It is validated against the tenant's own evidence
policy (``oerf.platform.evidence.policy``): the confidence floor is the
tenant's ``evidence.min_confidence`` and a fault code counts as verified only
when the tenant catalog has a ``FAULTCODE#`` row for it. The call is measured
with :func:`oerf.platform.observability.ai_trace.record_invocation`, which
records sanitized metadata only.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

from oerf.platform.evidence.analysis import AnalysisRejected, validate as validate_analysis
from oerf.platform.evidence.policy import (
    EvidencePolicy,
    dynamo_readers,
    resolve_policy,
)
from oerf.platform.observability.ai_trace import TraceRejected, record_invocation
from oerf.platform.observability.logging import get_logger, log

logger = get_logger(__name__)
logger.setLevel(logging.INFO)

BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
INCIDENT_TABLE_NAME = os.environ.get("INCIDENT_TABLE_NAME", "")
TENANT_TABLE_NAME = os.environ.get("TENANT_TABLE_NAME", "")
USE_MOCK_BEDROCK = os.environ.get("USE_MOCK_BEDROCK", "true").lower() == "true"
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "")

#: Every evidence object lives under the calling tenant's prefix.
KEY_PREFIX = "tenants/{tenant_id}/"

_s3 = None
_bedrock = None
_dynamodb = None
_dynamodb_client = None


def get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def get_bedrock():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client(
            "bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1")
        )
    return _bedrock


def get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def get_dynamodb_client():
    """The low-level client the tenant readers share.

    It is the same client ``get_s3`` and ``get_bedrock`` follow: built on first
    use, never at import time, so a test can inject fakes.
    """
    global _dynamodb_client
    if _dynamodb_client is None:
        _dynamodb_client = boto3.client("dynamodb")
    return _dynamodb_client


VISION_SYSTEM_PROMPT = """You are an expert cold-chain diagnostics AI for
refrigerated transport (reefer) systems.
Analyze the provided photograph of the reefer control panel / digital display carefully.
Extract the following exact fields in JSON:
{
    "setpoint_temp_c": <float or null>,
    "ambient_temp_c": <float or null>,
    "fault_codes": [<string array of detected error codes, e.g. "AL02", "AL04", or empty []>],
    "alarm_status": "<ACTIVE|OFF|UNKNOWN>",
    "door_alarm_status": "<OPEN|CLOSED|UNKNOWN>",
    "power_status": "<RUNNING|STANDBY|SHUTDOWN|UNKNOWN>",
    "confidence_score": <float between 0.0 and 1.0>,
    "display_readout_summary": "<brief text description of visible display>"
}
Return ONLY valid raw JSON with no markdown backticks or explanation."""

_EXTRACT_INSTRUCTION = (
    "Please extract the telemetry from this reefer display according to your instructions."
)


def _required_text(value: Any) -> str | None:
    """Return a stripped string, or None when the value is missing or blank."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _image_format(key: str) -> str:
    """Map an object key suffix to a Converse image format."""
    ext = key.lower().rsplit(".", 1)[-1] if "." in key else ""
    if ext == "png":
        return "png"
    if ext == "webp":
        return "webp"
    return "jpeg"


def _analysis_from_text(text_content: str) -> dict[str, Any]:
    """Parse the model text. The field set is the prompt schema."""
    if "```json" in text_content:
        text_content = text_content.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text_content:
        text_content = text_content.split("```", 1)[1].split("```", 1)[0].strip()
    analysis = json.loads(text_content)
    if not isinstance(analysis, dict):
        raise ValueError("model text was not a JSON object")
    return analysis


def tenant_key(tenant_id: str, key: str) -> bool:
    """True when ``key`` is inside the tenant's evidence prefix.

    Tenant prefixes are the S3 half of tenant isolation: a message for one
    tenant cannot make the handler read another tenant's object.
    """

    return key.startswith(KEY_PREFIX.format(tenant_id=tenant_id))


def _policy(tenant_id: str) -> EvidencePolicy:
    """The tenant's evidence policy, read fresh on each invocation."""

    if not TENANT_TABLE_NAME:
        return resolve_policy(tenant_id)
    tenant, config = dynamo_readers(get_dynamodb_client(), TENANT_TABLE_NAME)
    return resolve_policy(tenant_id, tenant=tenant, config=config)


def _record(
    *,
    latency_ms: int,
    response: dict[str, Any] | None,
    outcome: str,
    segment: Any = None,
) -> None:
    """Write the sanitized model-call trace. Tracing never fails the call."""

    usage = (response or {}).get("usage") or {}
    metadata = {
        "model_id": BEDROCK_MODEL_ID,
        "latency_ms": max(latency_ms, 0),
        "input_tokens": _token_count(usage, "inputTokens"),
        "output_tokens": _token_count(usage, "outputTokens"),
        "guardrail_action": _guardrail_action(response),
        "outcome": outcome,
    }
    try:
        record_invocation(metadata, segment=segment)
    except TraceRejected as exc:
        # The metadata is a fixed sanitized shape, so this is a programming
        # error rather than leak protection. Drop the trace, keep the reading.
        logger.warning("refusing the model-call trace: %s", exc)


def _token_count(usage: Any, name: str) -> int:
    if not isinstance(usage, dict):
        return 0
    value = usage.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _guardrail_action(response: dict[str, Any] | None) -> str:
    """The guardrail verdict as a short token, never as trace text."""

    metadata = (response or {}).get("metadata") or {}
    if metadata.get("stopReason") == "guardrail_intervened":
        return "BLOCKED"
    return "NONE"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point for evidence image analysis.

    Expected event payload:
    {
        "bucket": "evidence-bucket-name",
        "key": "tenants/T1/photos/reefer_display.jpg",
        "tenant_id": "T1",
        "incident_id": "INC-001"
    }

    The key must sit under ``tenants/{tenant_id}/``. The reading is stored only
    when it is well formed, and the stored record carries the verification so a
    reader can ask a human when the model is unsure or claims an unknown code.
    """
    bucket = event.get("bucket", "")
    key = event.get("key", "")
    tenant_id = _required_text(event.get("tenant_id"))
    incident_id = event.get("incident_id", "unknown")
    if tenant_id is None:
        logger.error("Rejected evidence analysis: tenant_required")
        return {"success": False, "error": "tenant_required", "fallback_required": True}

    if not tenant_key(tenant_id, key):
        logger.warning("Refusing evidence key outside the tenant prefix")
        return {
            "success": False,
            "error": "evidence key is outside the tenant prefix",
            "fallback_required": True,
        }

    policy = _policy(tenant_id)

    if USE_MOCK_BEDROCK:
        mock_result = {
            "setpoint_temp_c": 4.0,
            "ambient_temp_c": 12.8,
            "fault_codes": ["AL02"],
            "alarm_status": "ACTIVE",
            "door_alarm_status": "CLOSED",
            "power_status": "RUNNING",
            "confidence_score": 0.95,
            "display_readout_summary": (
                "Simulated analysis: High temp alarm AL02 active, "
                "ambient 12.8C vs setpoint 4.0C"
            ),
        }
        reading = validate_analysis(
            mock_result,
            known_fault_codes=policy.known_fault_codes,
            min_confidence=policy.min_confidence,
        )
        log(
            logger,
            logging.INFO,
            "evidence_analyzed",
            tenant_id=tenant_id,
            incident_id=incident_id if isinstance(incident_id, str) else None,
            outcome="mock",
        )
        return {
            "success": True,
            "analysis": reading.analysis,
            "verification": reading.verification,
            "source": "mock",
        }

    started = time.monotonic()
    response: dict[str, Any] | None = None
    try:
        s3_obj = get_s3().get_object(Bucket=bucket, Key=key)
        image_bytes = s3_obj["Body"].read()
        converse = {
            "modelId": BEDROCK_MODEL_ID,
            "system": [{"text": VISION_SYSTEM_PROMPT}],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "image": {
                                "format": _image_format(key),
                                "source": {"bytes": image_bytes},
                            }
                        },
                        {"text": _EXTRACT_INSTRUCTION},
                    ],
                }
            ],
            "inferenceConfig": {"maxTokens": 1024},
        }
        if GUARDRAIL_ID:
            # Trace stays off. A guardrail trace can carry unmasked values.
            converse["guardrailConfig"] = {
                "guardrailIdentifier": GUARDRAIL_ID,
                "guardrailVersion": GUARDRAIL_VERSION,
                "trace": "disabled",
            }
        response = get_bedrock().converse(**converse)
        blocks = response.get("output", {}).get("message", {}).get("content") or []
        text_content = next(
            (block.get("text", "") for block in blocks if isinstance(block, dict)),
            "",
        )
        reading = validate_analysis(
            _analysis_from_text(text_content),
            known_fault_codes=policy.known_fault_codes,
            min_confidence=policy.min_confidence,
        )
    except (ClientError, ValueError, KeyError, json.JSONDecodeError, AnalysisRejected) as exc:
        _record(latency_ms=_elapsed_ms(started), response=response, outcome="ERROR")
        log(
            logger,
            logging.ERROR,
            "evidence_analysis_failed",
            tenant_id=tenant_id,
            incident_id=incident_id if isinstance(incident_id, str) else None,
            outcome="error",
        )
        return {
            "success": False,
            "error": str(exc),
            "fallback_required": True,
        }

    _record(latency_ms=_elapsed_ms(started), response=response, outcome="SUCCESS")

    if INCIDENT_TABLE_NAME:
        get_dynamodb().Table(INCIDENT_TABLE_NAME).update_item(
            Key={"pk": f"TENANT#{tenant_id}", "sk": f"INCIDENT#{incident_id}"},
            UpdateExpression=(
                "SET evidence_analysis = :ea, evidence_verification = :ev, "
                "last_analyzed_key = :k"
            ),
            ExpressionAttributeValues={
                ":ea": reading.analysis,
                ":ev": reading.verification,
                ":k": key,
            },
        )

    log(
        logger,
        logging.INFO,
        "evidence_analyzed",
        tenant_id=tenant_id,
        incident_id=incident_id if isinstance(incident_id, str) else None,
        outcome="analyzed",
    )
    return {
        "success": True,
        "analysis": reading.analysis,
        "verification": reading.verification,
        "requires_confirmation": reading.requires_confirmation,
    }


def incident_id_of(event: dict[str, Any]) -> str:
    """The incident id a log line may name, and nothing else from the event."""

    value = event.get("incident_id")
    return str(value) if value else "unknown"


def _elapsed_ms(started: float) -> int:
    """Milliseconds since ``started``, clamped at zero for a coarse clock."""

    return max(int((time.monotonic() - started) * 1000), 0)
