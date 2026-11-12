"""``SecureFacility`` step entry point."""

from __future__ import annotations

from typing import Any

from oerf.platform.agent.runtime import run_step

PHASE = "secure_facility"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    step_name = event.get("step_name") or event.get("step")
    if step_name == "SecureFacilityFallback":
        phase = "secure_facility_fallback"
    elif step_name == "FacilityDecision":
        phase = "facility_decision"
    else:
        phase = PHASE
    return run_step(phase, event, context)
