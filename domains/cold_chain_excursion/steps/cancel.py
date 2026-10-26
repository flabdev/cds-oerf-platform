"""``Cancel`` step entry point.

Writes ``CANCELLED`` and the incident repository releases the shipment lock.
An incident that is already ``CANCELLED`` or ``CLOSED`` is left untouched.

IAM target: get, put, update, and delete on the incident table.
"""

from __future__ import annotations

from typing import Any

from oerf.platform.agent.envelope import narrow_task_event
from oerf.platform.agent.runtime import run_step

#: The phase this module owns. A task carrying a different phase's step is
#: rejected instead of running under this module's role.
PHASE = "cancel"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Return the replacement envelope for the ``Cancel`` step."""
    return run_step(PHASE, narrow_task_event(event), context)
