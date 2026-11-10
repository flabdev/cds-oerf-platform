"""``Approval`` step entry point for the task-token approval gate."""

from __future__ import annotations

from typing import Any

from oerf.platform.agent.runtime import run_step

PHASE = "approval"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return run_step(PHASE, event, context)
