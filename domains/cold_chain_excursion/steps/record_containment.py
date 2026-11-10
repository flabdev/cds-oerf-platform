"""``RecordContainment`` step entry point."""

from __future__ import annotations

from typing import Any

from oerf.platform.agent.runtime import run_step

PHASE = "record_containment"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return run_step(PHASE, event, context)
