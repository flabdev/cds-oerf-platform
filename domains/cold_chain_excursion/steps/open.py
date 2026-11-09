"""``OPEN`` step entry point. The ingest-created incident is pinned and recorded."""

from __future__ import annotations

from typing import Any

from oerf.platform.agent.runtime import run_step

PHASE = "open"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return run_step(PHASE, event, context)
