"""``Diagnose`` step entry point.

Frozen path: ``domains.cold_chain_excursion.steps.diagnose.handler``.

Reads the pinned cadence map and the tenant facility rows. No messaging.
"""

from __future__ import annotations

from typing import Any

from oerf.platform.agent.runtime import run_step

PHASE = "diagnose"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Return the replacement envelope for the ``Diagnose`` step."""
    return run_step(PHASE, event, context)
