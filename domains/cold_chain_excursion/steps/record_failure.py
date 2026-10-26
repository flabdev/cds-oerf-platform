"""``RecordFailure`` step entry point.

The failure path records that a step failed and leaves ``incident_state`` as
it arrived, so ``FailureRoute`` still chooses Escalate or Cancel.

IAM target: get and put on the incident table. No update, no delete, no
tenant-table read.
"""

from __future__ import annotations

from typing import Any

from oerf.platform.agent.envelope import narrow_task_event
from oerf.platform.agent.runtime import run_step

#: The phase this module owns. A task carrying a different phase's step is
#: rejected instead of running under this module's role.
PHASE = "record_failure"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Return the replacement envelope for the ``RecordFailure`` step."""
    return run_step(PHASE, narrow_task_event(event), context)
