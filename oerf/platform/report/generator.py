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
        step_workflows=_step_workflows(table, pk),
    )
    receipt = publish_report(document, _store(), pdf_backend="none")
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
    step_workflows: dict[str, dict[str, Any]] | None = None,
) -> ReportDocument:
    """Map canonical and persisted step data onto the report."""
    workflows = step_workflows or {}
    assess = workflows.get("Assess", {})
    workflow = _latest_workflow(workflows)
    context = _mapping(assess.get("context")) or _mapping(workflow.get("context")) or {}
    assessment = _mapping(assess.get("assessment")) or _mapping(workflow.get("assessment")) or {}
    diagnosis = _mapping(workflow.get("diagnosis")) or {}
    approval = _mapping(workflow.get("approval")) or {}
    facility = _mapping(workflow.get("facility")) or {}
    action = _mapping(workflow.get("action")) or {}
    containment = (
        _mapping(workflows.get("RecordContainment", {}).get("containment"))
        or _mapping(workflow.get("containment"))
        or {}
    )
    options = _options(assessment.get("options"))
    sensor_rows = readings or []
    return ReportDocument(
        tenant_id=tenant_id,
        incident_id=incident_id,
        shipment_id=_optional_text(incident.get("shipment_id")),
        state=_optional_text(incident.get("state")),
        generated_at=generated_at,
        temperature_timeline=[line for line in (_reading_line(row) for row in sensor_rows) if line],
        state_timeline=[line for line in (_transition_line(row) for row in timeline) if line],
        diagnosis=(
            _optional_text(diagnosis.get("summary"))
            or _optional_text(diagnosis.get("status"))
            or _optional_text(incident.get("diagnosis"))
            or _optional_text(incident.get("reason"))
        ),
        evidence=_evidence(incident.get("evidence")),
        options=options,
        approval=_optional_text(approval.get("status")),
        facility=_facility_line(facility),
        actions=_action_lines(action),
        containment=_containment_line(containment),
        qa_disposition=_optional_text(incident.get("qa_disposition")),
        decision_trace=_decision_trace(context, assessment, options)
        or _trace(incident.get("decision_trace")),
    )


def _step_workflows(table: Any, pk: str) -> dict[str, dict[str, Any]]:
    """Read persisted handler snapshots for one tenant-scoped incident."""
    parsed: dict[str, dict[str, Any]] = {}
    for row in _rows(table, pk, "STEP#"):
        step = str(row.get("step") or row.get("sk", "").removeprefix("STEP#"))
        raw = row.get("step_output")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                continue
        output = raw.get("workflow") if isinstance(raw, dict) else None
        if isinstance(output, dict):
            parsed[step] = output
    return parsed


def _latest_workflow(workflows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Merge snapshots in workflow order; later outputs carry prior values."""
    order = (
        "OPEN", "GatherContext", "Diagnose", "Assess", "Approval", "SecureFacility",
        "SecureFacilityFallback", "Act", "Verify", "RecordContainment", "BeginDisposition",
        "GenerateReport", "QA", "Close",
    )
    merged: dict[str, Any] = {}
    for step in order:
        snapshot = workflows.get(step)
        if snapshot:
            merged.update(snapshot)
    if "SecureFacilityFallback" in workflows:
        first_facility = _mapping(workflows.get("SecureFacility", {}).get("facility"))
        fallback_facility = _mapping(workflows["SecureFacilityFallback"].get("facility"))
        facility = fallback_facility.copy()
        if first_facility:
            history = fallback_facility.get("history")
            facility["history"] = [
                first_facility,
                *([_mapping(item) for item in history] if isinstance(history, list) else []),
            ]
        merged["facility"] = facility
    return merged


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _decision_trace(
    context: dict[str, Any], assessment: dict[str, Any], options: list[ReportOption]
) -> DecisionTrace | None:
    """Build a numeric trace only when every required rule input is available."""
    settings = _mapping(context.get("assessment"))
    trigger = _mapping(context.get("trigger"))
    limits = context.get("range_c")
    numeric = {
        "used_minutes": assessment.get("elapsed_minutes"),
        "temperature_c": trigger.get("temperature_c"),
        "allowance_minutes": context.get("allowance_minutes"),
        "upper_c": limits[1] if isinstance(limits, list | tuple) and len(limits) == 2 else None,
        "unload_min": settings.get("unload_min"),
        "rise_rate_c_per_min": settings.get("rise_rate_c_per_min"),
        "cooldown_rate_c_per_min": settings.get("cooldown_rate_c_per_min"),
        "min_spare_allowance_min": settings.get("min_spare_allowance_min"),
    }
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float, Decimal))
        for value in numeric.values()
    ):
        return None
    try:
        return DecisionTrace(
            **numeric,
            options=options,
            recommended=_optional_text(assessment.get("recommended_option")),
        )
    except Exception:
        return None


def _facility_line(facility: dict[str, Any]) -> str | None:
    history = facility.get("history")
    records = [_mapping(item) for item in history] if isinstance(history, list) else []
    records.append(facility)
    lines: list[str] = []
    for record in records:
        facility_id = _optional_text(record.get("facility_id"))
        if facility_id is None:
            continue
        status = "accepted" if record.get("accepted") is True else "declined"
        lines.append(f"{facility_id} ({status})")
    return "; ".join(lines) or None


def _action_lines(action: dict[str, Any]) -> list[str]:
    if not action:
        return []
    values = (
        _optional_text(action.get("kind")),
        _optional_text(action.get("facility_id")),
        _optional_text(action.get("status")),
    )
    line = " ".join(part for part in values if part)
    return [line] if line else []


def _containment_line(containment: dict[str, Any]) -> str | None:
    if not containment:
        return None
    values = [_optional_text(containment.get("status"))]
    temperature = containment.get("temperature_c")
    stable = containment.get("stable_minutes")
    if isinstance(temperature, (int, float, Decimal)) and not isinstance(temperature, bool):
        values.append(f"{temperature} C")
    if isinstance(stable, (int, float, Decimal)) and not isinstance(stable, bool):
        values.append(f"stable {stable} min")
    line = "; ".join(value for value in values if value)
    return line or None


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
