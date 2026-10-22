"""Validate a vision reading against the prompt schema and the tenant policy.

The model produces advisory values. Nothing here decides an outcome: it keeps
the reading well-formed, normalizes the fault codes, and separates a code the
tenant catalog knows from one only the model claims. An unreadable or
disagreeing reading is returned with ``requires_confirmation`` set, and the
caller asks a human.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: The exact field set the system prompt asks for (``vision.VISION_SYSTEM_PROMPT``).
FIELDS = (
    "setpoint_temp_c",
    "ambient_temp_c",
    "fault_codes",
    "alarm_status",
    "door_alarm_status",
    "power_status",
    "confidence_score",
    "display_readout_summary",
)
_ALARM = frozenset({"ACTIVE", "OFF", "UNKNOWN"})
_DOOR = frozenset({"OPEN", "CLOSED", "UNKNOWN"})
_POWER = frozenset({"RUNNING", "STANDBY", "SHUTDOWN", "UNKNOWN"})


class AnalysisRejected(ValueError):
    """The model output is not the JSON object the prompt schema describes."""


@dataclass(frozen=True)
class Reading:
    """One validated vision reading, with the tenant's verification verdict."""

    analysis: dict[str, Any]
    observed_fault_codes: list[str]
    verified_fault_codes: list[str]
    unverified_fault_codes: list[str]
    readable: bool
    confidence: float | None
    requires_confirmation: bool
    verification: dict[str, Any] = field(default_factory=dict)


def validate(
    candidate: Any,
    *,
    known_fault_codes: frozenset[str] = frozenset(),
    min_confidence: float = 0.0,
) -> Reading:
    """Validate ``candidate``, returning the stored analysis and its verdict.

    ``known_fault_codes`` is the tenant's ``FAULTCODE#`` catalog. The comparison
    is case-insensitive; ``verified_fault_codes`` are the codes the tenant
    catalog knows, and every observed code stays in ``observed_fault_codes``.
    """

    if not isinstance(candidate, dict):
        raise AnalysisRejected("model text was not a JSON object")
    missing = [name for name in FIELDS if name not in candidate]
    if missing:
        raise AnalysisRejected("model output is missing: " + ", ".join(missing))

    codes = _codes(candidate["fault_codes"])
    verified = [code for code in codes if code in known_fault_codes]
    unverified = [code for code in codes if code not in known_fault_codes]
    confidence = _confidence(candidate["confidence_score"])
    analysis = {
        "setpoint_temp_c": _temperature(candidate["setpoint_temp_c"]),
        "ambient_temp_c": _temperature(candidate["ambient_temp_c"]),
        "fault_codes": codes,
        "alarm_status": _token(candidate["alarm_status"], _ALARM, "alarm_status"),
        "door_alarm_status": _token(candidate["door_alarm_status"], _DOOR, "door_alarm_status"),
        "power_status": _token(candidate["power_status"], _POWER, "power_status"),
        "confidence_score": confidence,
        "display_readout_summary": _summary(candidate["display_readout_summary"]),
    }
    readable = analysis["setpoint_temp_c"] is not None or bool(codes)
    requires_confirmation = (
        not readable or confidence is None or confidence < min_confidence or bool(unverified)
    )
    verification = {
        "readable": readable,
        "verified_fault_codes": verified,
        "unverified_fault_codes": unverified,
        "min_confidence": min_confidence,
        "requires_confirmation": requires_confirmation,
    }
    return Reading(
        analysis=analysis,
        observed_fault_codes=codes,
        verified_fault_codes=verified,
        unverified_fault_codes=unverified,
        readable=readable,
        confidence=confidence,
        requires_confirmation=requires_confirmation,
        verification=verification,
    )


def _codes(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise AnalysisRejected("fault_codes must be a list of strings")
    codes: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise AnalysisRejected("fault_codes must be a list of strings")
        text = entry.strip().upper()
        if text and text not in codes:
            codes.append(text)
    return codes


def _temperature(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnalysisRejected("a temperature field must be a number or null")
    return float(value)


def _confidence(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnalysisRejected("confidence_score must be a number or null")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise AnalysisRejected("confidence_score must be between 0.0 and 1.0")
    return number


def _token(value: Any, allowed: frozenset[str], name: str) -> str:
    if not isinstance(value, str):
        raise AnalysisRejected(f"{name} must be a string")
    text = value.strip().upper()
    if text not in allowed:
        raise AnalysisRejected(f"{name} must be one of {', '.join(sorted(allowed))}")
    return text


def _summary(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AnalysisRejected("display_readout_summary must be a string")
    return value.strip()