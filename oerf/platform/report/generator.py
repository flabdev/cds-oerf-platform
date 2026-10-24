"""Incident report publisher (ADR-006).

HTML is the source document. A published version is immutable. This module
reads the canonical incident item and does not send the report. Delivery goes
through the channel router.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3

from oerf.platform.observability.logging import get_logger, log
from oerf.platform.report.model import DecisionTrace, ReportDocument, ReportOption
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
    """Publish the HTML report for one stored incident.

    The event carries ``tenant_id`` and ``incident_id``. ``qa_email`` is ignored:
    this function does not send mail. A missing META row is a failure and does
    not publish a substitute report.
    """
    tenant_id = _required_text(event.get("tenant_id"))
    incident_id = _required_text(event.get("incident_id"))
    if tenant_id is None:
        logger.error("Rejected report: tenant_required")
        return {"success": False, "error": "tenant_required"}
    if incident_id is None:
        logger.error("Rejected report: incident_id_required")
        return {"success": False, "error": "incident_id_required"}
    if not INCIDENT_TABLE_NAME:
        logger.error("Rejected report: incident_table_required")
        return {"success": False, "error": "incident_table_required"}

    table = get_dynamodb().Table(INCIDENT_TABLE_NAME)
    pk = f"TENANT#{tenant_id}#INC#{incident_id}"
    loaded = table.get_item(Key={"pk": pk, "sk": "META"})
    incident = loaded.get("Item") if isinstance(loaded, dict) else None
    if not isinstance(incident, dict) or not incident:
        logger.error("Rejected report: incident_not_found")
        return {"success": False, "error": "incident_not_found"}
    if str(incident.get("tenant_id") or "") != tenant_id:
        logger.error("Rejected report: incident_not_found")
        return {"success": False, "error": "incident_not_found"}

    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    document = _document(
        tenant_id,
        incident_id,
        incident,
        _rows(table, pk, "EVT#"),
        generated_at,
        readings=_rows(table, pk, "READ#"),
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


def _document(
    tenant_id: str,
    incident_id: str,
    incident: dict[str, Any],
    timeline: list[dict[str, Any]],
    generated_at: str,
    *,
    readings: list[dict[str, Any]] | None = None,
) -> ReportDocument:
    """Map stored attributes onto the report. Absent sections stay empty.

    ``READ#`` rows supply ``ts`` and ``temperature_c``. ``EVT#`` rows are state
    transitions and stay in ``state_timeline``.
    """

    sensor_rows = readings or []
    return ReportDocument(
        tenant_id=tenant_id,
        incident_id=incident_id,
        shipment_id=_optional_text(incident.get("shipment_id")),
        state=_optional_text(incident.get("state")),
        generated_at=generated_at,
        temperature_timeline=[
            line for line in (_reading_line(entry) for entry in sensor_rows) if line
        ],
        state_timeline=[line for line in (_transition_line(entry) for entry in timeline) if line],
        diagnosis=_optional_text(incident.get("diagnosis") or incident.get("reason")),
        evidence=_evidence(incident.get("evidence")),
        options=_options(incident.get("options")),
        approval=_optional_text(incident.get("approval")),
        facility=_optional_text(incident.get("facility")),
        actions=_texts(incident.get("actions")),
        containment=_optional_text(incident.get("containment")),
        qa_disposition=_optional_text(incident.get("qa_disposition")),
        decision_trace=_trace(incident.get("decision_trace")),
    )


def _rows(table: Any, pk: str, prefix: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    exclusive_start_key = None
    while True:
        args: dict[str, Any] = {
            "KeyConditionExpression": "pk = :pk AND begins_with(sk, :prefix)",
            "ExpressionAttributeValues": {":pk": pk, ":prefix": prefix},
        }
        if exclusive_start_key is not None:
            args["ExclusiveStartKey"] = exclusive_start_key
        response = table.query(**args)
        if not isinstance(response, dict):
            return items
        items.extend(item for item in response.get("Items", []) if isinstance(item, dict))
        exclusive_start_key = response.get("LastEvaluatedKey")
        if not exclusive_start_key:
            return items


def _reading_line(entry: dict[str, Any]) -> str:
    """One persisted ``READ#`` row: the reading time and temperature, not a state.

    The boto3 resource deserializes DynamoDB numbers to ``Decimal``, so the
    temperature check must accept it alongside ``int`` and ``float``.
    """

    if str(entry.get("sk") or "").startswith("EVT#"):
        return ""
    ts = entry.get("ts") or entry.get("timestamp")
    temperature = entry.get("temperature_c")
    if not isinstance(ts, str) or not ts.strip():
        return ""
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float, Decimal)):
        return ""
    return f"{ts.strip()} {temperature} C"


def _transition_line(entry: dict[str, Any]) -> str:
    previous = str(entry.get("previous_state") or "").strip()
    state = str(entry.get("state") or "").strip()
    reason = str(entry.get("reason") or "").strip()
    change = f"{previous} -> {state}" if previous else state
    return " ".join(part for part in (str(entry.get("sk") or "").strip(), change, reason) if part)


def _evidence(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _options(value: Any) -> list[ReportOption]:
    if not isinstance(value, list):
        return []
    parsed: list[ReportOption] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            parsed.append(ReportOption.model_validate(item))
        except Exception:
            continue
    return parsed


def _texts(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _trace(value: Any) -> DecisionTrace | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict):
        return None
    try:
        return DecisionTrace.model_validate(value)
    except Exception:
        return None


def _optional_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _required_text(value: Any) -> str | None:
    """Return a stripped string, or None when the value is missing or blank."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _store():
    if REPORT_BUCKET_NAME:
        return S3ReportStore(get_s3(), REPORT_BUCKET_NAME)
    return MemoryReportStore()
