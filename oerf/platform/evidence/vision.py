"""Multimodal Bedrock vision evidence extractor (ADR-006, ADR-020).

Reads a reefer display photo from the evidence bucket and asks the configured
Bedrock model, default Amazon Nova Lite, for the telemetry JSON below.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
INCIDENT_TABLE_NAME = os.environ.get("INCIDENT_TABLE_NAME", "")
USE_MOCK_BEDROCK = os.environ.get("USE_MOCK_BEDROCK", "true").lower() == "true"
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "")

_s3 = None
_bedrock = None
_dynamodb = None


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


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point for evidence image analysis.

    Expected event payload:
    {
        "bucket": "evidence-bucket-name",
        "key": "T1/INC-001/photos/reefer_display.jpg",
        "tenant_id": "T1",
        "incident_id": "INC-001"
    }
    """
    logger.info("Analyzing evidence: %s", json.dumps(event))

    bucket = event.get("bucket", "")
    key = event.get("key", "")
    tenant_id = event.get("tenant_id", "default")
    incident_id = event.get("incident_id", "unknown")

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
        return {"success": True, "analysis": mock_result, "source": "mock"}

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
        analysis = _analysis_from_text(text_content)

        if INCIDENT_TABLE_NAME:
            get_dynamodb().Table(INCIDENT_TABLE_NAME).update_item(
                Key={"pk": f"TENANT#{tenant_id}", "sk": f"INCIDENT#{incident_id}"},
                UpdateExpression="SET evidence_analysis = :ea, last_analyzed_key = :k",
                ExpressionAttributeValues={":ea": analysis, ":k": key},
            )

        return {"success": True, "analysis": analysis}

    except (ClientError, ValueError, KeyError, json.JSONDecodeError) as exc:
        logger.error("Failed to analyze evidence image: %s", exc)
        return {
            "success": False,
            "error": str(exc),
            "fallback_required": True,
        }
