"""Incident report publisher (ADR-006).

HTML is the source document. A published version is immutable. Delivery of the
report to a person goes through the channel router, not this module.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

import boto3

from oerf.platform.observability.logging import get_logger, log
from oerf.platform.report.model import ReportDocument
from oerf.platform.report.service import publish_report
from oerf.platform.report.store import MemoryReportStore, S3ReportStore

logger = get_logger(__name__)
logger.setLevel(logging.INFO)

REPORT_BUCKET_NAME = os.environ.get("REPORT_BUCKET_NAME", "")
INCIDENT_TABLE_NAME = os.environ.get("INCIDENT_TABLE_NAME", "")

_s3 = None
_dynamodb = None


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


def format_report_markdown(incident: dict[str, Any]) -> str:
    """Render a clean GxP-compliant incident report."""
    tenant_id = incident.get("tenant_id") or ""
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
    tenant_id = _required_text(event.get("tenant_id"))
    incident_id = event.get("incident_id", "unknown")
    if tenant_id is None:
        logger.error("Rejected report: tenant_required")
        return {"success": False, "error": "tenant_required"}

    # Fetch incident details from table
    incident = {}
    if INCIDENT_TABLE_NAME:
        table = get_dynamodb().Table(INCIDENT_TABLE_NAME)
        resp = table.get_item(Key={"pk": f"TENANT#{tenant_id}", "sk": f"INCIDENT#{incident_id}"})
        incident = resp.get("Item", {})

    if not incident:
        incident = {"tenant_id": tenant_id, "incident_id": incident_id, "status": "RESOLVED"}

    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    document = ReportDocument(
        tenant_id=tenant_id,
        incident_id=incident_id,
        shipment_id=incident.get("shipment_id"),
        state=incident.get("state") or incident.get("status"),
        generated_at=generated_at,
        diagnosis=incident.get("diagnosis"),
        approval=incident.get("approval"),
        facility=incident.get("facility"),
        containment=incident.get("containment"),
        qa_disposition=incident.get("qa_disposition"),
    )
    receipt = publish_report(document, _store())
    s3_uri = f"s3://{REPORT_BUCKET_NAME}/{receipt.s3_key}" if REPORT_BUCKET_NAME else ""
    log(
        logger,
        logging.INFO,
        "report_published",
        tenant_id=tenant_id,
        incident_id=incident_id if isinstance(incident_id, str) else None,
        outcome="published",
        count=receipt.report_version,
    )
    return {
        "success": True,
        "report_key": receipt.s3_key,
        "s3_uri": s3_uri,
        "report_version": receipt.report_version,
        "sha256": receipt.sha256,
        "locked": receipt.locked,
        "generated_at": receipt.generated_at,
        "sections": receipt.sections,
    }


def _required_text(value: Any) -> str | None:
    """Return a stripped string, or None when the value is missing or blank."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _store():
    if REPORT_BUCKET_NAME:
        return S3ReportStore(get_s3(), REPORT_BUCKET_NAME)
    return MemoryReportStore()
