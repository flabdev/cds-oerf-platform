"""Incident Resolution Summary and CAPA Report Generator (ADR-006).

Compiles the audit ledger, Bedrock Vision findings, and human approvals
into a structured Markdown audit document, stores it in ReportBucket,
and emails an executive summary via Amazon SES.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REPORT_BUCKET_NAME = os.environ.get("REPORT_BUCKET_NAME", "")
INCIDENT_TABLE_NAME = os.environ.get("INCIDENT_TABLE_NAME", "")
SES_SOURCE_EMAIL = os.environ.get("SES_SOURCE_EMAIL", "")

_s3 = None
_dynamodb = None
_ses = None


def get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def get_ses():
    global _ses
    if _ses is None:
        _ses = boto3.client("sesv2")
    return _ses


def format_report_markdown(incident: dict[str, Any]) -> str:
    """Render a clean GxP-compliant incident report."""
    tenant_id = incident.get("tenant_id", "Unknown")
    incident_id = incident.get("incident_id", "Unknown")
    shipment_id = incident.get("shipment_id", "Unknown")
    status = incident.get("status", "RESOLVED")
    created_epoch = incident.get("created_at", time.time())
    created_at = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(created_epoch))
    closed_at = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())

    evidence = incident.get("evidence_analysis", {})
    timeline = incident.get("timeline", [])

    timeline_rows = ""
    for entry in timeline:
        t_str = time.strftime("%H:%M:%S", time.gmtime(entry.get("time", time.time())))
        event = entry.get("event", "UPDATE")
        details = entry.get("details") or entry.get("notes") or ""
        timeline_rows += f"| {t_str} | {event} | {details} |\n"

    return f"""# OERF Incident Resolution Report
**Incident ID**: `{incident_id}`  
**Tenant**: `{tenant_id}` | **Shipment**: `{shipment_id}`  
**Status**: `{status}`  
**Opened**: {created_at} | **Closed**: {closed_at}  

---

## 1. Initial Sensor Alarm
- **Sensor Reading**: `{json.dumps(incident.get('sensor_reading', {}))}`

## 2. Multimodal AI Evidence Extraction (Bedrock Vision)
- **Setpoint Temp**: `{evidence.get('setpoint_temp_c', 'N/A')} °C`
- **Ambient Temp**: `{evidence.get('ambient_temp_c', 'N/A')} °C`
- **Fault Codes**: `{evidence.get('fault_codes', [])}`
- **Alarm Status**: `{evidence.get('alarm_status', 'N/A')}`
- **Door Status**: `{evidence.get('door_alarm_status', 'N/A')}`
- **Readout Summary**: {evidence.get('display_readout_summary', 'None')}

## 3. Resolution & Human Approval Timeline
| Time (UTC) | Event | Details / Notes |
|------------|-------|-----------------|
{timeline_rows}

---
*Report generated automatically by OERF Platform (Operational Exception Resolution Framework).*
"""


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point for report generation.
    
    Expected event payload:
    {
        "tenant_id": "T1",
        "incident_id": "INC-001",
        "qa_email": "qa-manager@pharma.com"
    }
    """
    logger.info("Generating report for: %s", json.dumps(event))

    tenant_id = event.get("tenant_id", "T1")
    incident_id = event.get("incident_id", "unknown")
    qa_email = event.get("qa_email")

    # Fetch incident details from table
    incident = {}
    if INCIDENT_TABLE_NAME:
        table = get_dynamodb().Table(INCIDENT_TABLE_NAME)
        resp = table.get_item(Key={"pk": f"TENANT#{tenant_id}", "sk": f"INCIDENT#{incident_id}"})
        incident = resp.get("Item", {})

    if not incident:
        incident = {"tenant_id": tenant_id, "incident_id": incident_id, "status": "RESOLVED"}

    markdown_report = format_report_markdown(incident)
    report_key = f"{tenant_id}/{incident_id}/resolution_report.md"

    # Save to ReportBucket
    s3_uri = ""
    if REPORT_BUCKET_NAME:
        try:
            s3 = get_s3()
            s3.put_object(
                Bucket=REPORT_BUCKET_NAME,
                Key=report_key,
                Body=markdown_report.encode("utf-8"),
                ContentType="text/markdown",
            )
            s3_uri = f"s3://{REPORT_BUCKET_NAME}/{report_key}"
            logger.info("Saved report to %s", s3_uri)
        except Exception as exc:
            logger.error("Failed to save report to S3: %s", exc)

    # Optional email summary to QA
    if qa_email and SES_SOURCE_EMAIL:
        try:
            ses = get_ses()
            ses.send_email(
                FromEmailAddress=SES_SOURCE_EMAIL,
                Destination={"ToAddresses": [qa_email]},
                Content={
                    "Simple": {
                        "Subject": {
                            "Data": (
                                f"OERF Incident Report: {incident_id} "
                                f"[{incident.get('status', 'RESOLVED')}]"
                            )
                        },
                        "Body": {"Text": {"Data": markdown_report}},
                    }
                },
            )
            logger.info("Emailed report to %s", qa_email)
        except ClientError as exc:
            logger.warning("Failed to email report via SES: %s", exc)

    return {"success": True, "report_key": report_key, "s3_uri": s3_uri}
