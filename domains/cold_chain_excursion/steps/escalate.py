"""``Escalate`` step entry point.

Writes ``ESCALATED`` when the stored state has that edge, and leaves the
shipment lock in place. A second call does not write another timeline event.

IAM target: get, put, and update on the incident table. No delete.
"""

from __future__ import annotations

from typing import Any

from oerf.platform.agent.envelope import narrow_task_event
from oerf.platform.agent.runtime import run_step

#: The phase this module owns. A task carrying a different phase's step is
#: rejected instead of running under this module's role.
PHASE = "escalate"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Return the replacement envelope for the ``Escalate`` step."""
    return run_step(PHASE, narrow_task_event(event), context)
