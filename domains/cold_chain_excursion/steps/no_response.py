"""``NoResponse`` step entry point for an expired approval wait."""

from __future__ import annotations

from typing import Any

from oerf.platform.agent.envelope import narrow_task_event
from oerf.platform.agent.runtime import run_step

PHASE = "no_response"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return run_step(PHASE, narrow_task_event(event), context)
