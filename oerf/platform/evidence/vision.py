"""Multimodal Bedrock Vision Evidence Extractor (ADR-006).

Invokes Anthropic Claude 3.5 Sonnet on photos of reefer temperature displays,
7-segment control panels, and alarm indicators stored in S3 EvidenceBucket.
Extracts structured telemetry for deterministic SOP rule evaluation.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Dict

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID",
    "anthropic.claude-3-5-sonnet-20241022-v2:0",
)
FALLBACK_MODEL_ID = "anthropic.claude-3-haiku-20240307-v1:0"
INCIDENT_TABLE_NAME = os.environ.get("INCIDENT_TABLE_NAME", "")
USE_MOCK_BEDROCK = os.environ.get("USE_MOCK_BEDROCK", "false").lower() == "true"

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
        _bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _bedrock


def get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


VISION_SYSTEM_PROMPT = """You are an expert cold-chain diagnostics AI for refrigerated transport (reefer) systems.
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


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
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
            "display_readout_summary": "Simulated analysis: High temp alarm AL02 active, ambient 12.8C vs setpoint 4.0C",
        }
        return {"success": True, "analysis": mock_result, "source": "mock"}

    try:
        s3 = get_s3()
        s3_obj = s3.get_object(Bucket=bucket, Key=key)
        image_bytes = s3_obj["Body"].read()
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

        # Detect image format from key extension
        ext = key.lower().split(".")[-1]
        media_type = "image/jpeg"
        if ext == "png":
            media_type = "image/png"
        elif ext == "webp":
            media_type = "image/webp"

        payload = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "system": VISION_SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": "Please extract the telemetry from this reefer display according to your instructions.",
                        },
                    ],
                }
            ],
        }

        bedrock = get_bedrock()
        try:
            response = bedrock.invoke_model(
                modelId=BEDROCK_MODEL_ID,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(payload),
            )
        except ClientError as exc:
            logger.warning("Primary Bedrock model %s failed, attempting fallback %s: %s", BEDROCK_MODEL_ID, FALLBACK_MODEL_ID, exc)
            response = bedrock.invoke_model(
                modelId=FALLBACK_MODEL_ID,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(payload),
            )

        resp_body = json.loads(response["body"].read().decode("utf-8"))
        text_content = resp_body.get("content", [{}])[0].get("text", "{}")
        # Strip potential markdown code blocks
        if "```json" in text_content:
            text_content = text_content.split("```json")[1].split("```")[0].strip()
        elif "```" in text_content:
            text_content = text_content.split("```")[1].split("```")[0].strip()

        analysis = json.loads(text_content)

        # Update Incident record if table is provided
        if INCIDENT_TABLE_NAME:
            table = get_dynamodb().Table(INCIDENT_TABLE_NAME)
            table.update_item(
                Key={"pk": f"TENANT#{tenant_id}", "sk": f"INCIDENT#{incident_id}"},
                UpdateExpression="SET evidence_analysis = :ea, last_analyzed_key = :k",
                ExpressionAttributeValues={":ea": analysis, ":k": key},
            )

        return {"success": True, "analysis": analysis}

    except Exception as exc:
        logger.error("Failed to analyze evidence image: %s", exc)
        return {
            "success": False,
            "error": str(exc),
            "fallback_required": True,
        }
