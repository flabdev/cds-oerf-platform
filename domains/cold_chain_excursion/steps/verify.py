"""``Verify`` step entry point.

Frozen path (``docs/interfaces.md`` section 8, owner OERF-202):
``domains.cold_chain_excursion.steps.verify.handler``.

IAM target: read the incident and readings. Write containment state.
"""

from __future__ import annotations

from typing import Any

from oerf.platform.agent.runtime import run_step

#: The phase this module owns. A task carrying a different phase's step is
#: rejected instead of running under this module's role.
PHASE = "verify"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Return the replacement envelope for the ``Verify`` step."""
    return run_step(PHASE, event, context)
