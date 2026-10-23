"""Deterministic HTML. Every section is present. Private contact fields are omitted."""

from __future__ import annotations

from html import escape
from typing import Any

from oerf.platform.report.model import SECTIONS, ReportDocument

_PRIVATE = frozenset({"phone", "e164", "email", "task_token", "recipient"})
_MISSING = "Not recorded"


def render_html(document: ReportDocument) -> str:
    """Return the source document. The same input always produces the same HTML."""

    parts = [
        "<!DOCTYPE html>",
        "<html><head><meta charset=\"utf-8\"><title>",
        f"Incident {escape(document.incident_id)}",
        "</title></head><body>",
        "<h1>OERF incident report</h1>",
    ]
    for section in SECTIONS:
        parts.append(f"<section id=\"{section}\"><h2>{escape(section)}</h2>")
        parts.append(_section_body(document, section))
        parts.append("</section>")
    parts.append("</body></html>")
    return "".join(parts)


def _section_body(document: ReportDocument, section: str) -> str:
    if section == "incident":
        return _text(f"{document.tenant_id} {document.incident_id} {document.state}")
    if section == "shipment":
        return _text(document.shipment_id)
    if section == "temperature_timeline":
        return _list(document.temperature_timeline)
    if section == "state_timeline":
        return _list(document.state_timeline)
    if section == "diagnosis":
        return _text(document.diagnosis)
    if section == "evidence":
        visible = {key: value for key, value in document.evidence.items() if key not in _PRIVATE}
        return _text(_pairs(visible) if visible else None)
    if section == "options":
        return _list(_option_line(option) for option in document.options)
    if section == "approval":
        return _text(document.approval)
    if section == "facility":
        return _text(document.facility)
    if section == "actions":
        return _list(document.actions)
    if section == "containment":
        return _text(document.containment)
    if section == "qa_disposition":
        return _text(document.qa_disposition)
    return _decision(document)


def _decision(document: ReportDocument) -> str:
    trace = document.decision_trace
    if trace is None:
        return _text(None)
    lines = [
        f"used_minutes={trace.used_minutes}",
        f"temperature_c={trace.temperature_c}",
        f"allowance_minutes={trace.allowance_minutes}",
        f"upper_c={trace.upper_c}",
        f"unload_min={trace.unload_min}",
        f"rise_rate_c_per_min={trace.rise_rate_c_per_min}",
        f"cooldown_rate_c_per_min={trace.cooldown_rate_c_per_min}",
        f"min_spare_allowance_min={trace.min_spare_allowance_min}",
        f"recommended={trace.recommended or _MISSING}",
    ]
    lines.extend(_option_line(option) for option in trace.options)
    return _list(lines)


def _option_line(option) -> str:
    flag = "feasible" if option.feasible else "infeasible"
    return (
        f"{option.id} drive_min={option.drive_min} "
        f"projected_minutes={option.projected_minutes} "
        f"spare_minutes={option.spare_minutes} {flag}"
    )


def _pairs(values: dict[str, Any]) -> str:
    return "; ".join(f"{key}={values[key]}" for key in sorted(values))


def _list(items) -> str:
    rows = [str(item) for item in items if str(item).strip()]
    if not rows:
        return f"<p>{_MISSING}</p>"
    body = "".join(f"<li>{escape(row)}</li>" for row in rows)
    return f"<ul>{body}</ul>"


def _text(value: str | None) -> str:
    shown = value.strip() if isinstance(value, str) and value.strip() else _MISSING
    return f"<p>{escape(shown)}</p>"
