"""``QA`` step entry point.

Frozen path (``docs/interfaces.md`` section 8, owner OERF-202):
``domains.cold_chain_excursion.steps.qa.handler``.

IAM target: read the incident and the report pointer. No SES.
"""

from __future__ import annotations

from typing import Any

from oerf.platform.agent.runtime import build_dependencies, run_step

#: The phase this module owns. A task carrying a different phase's step is
#: rejected instead of running under this module's role.
PHASE = "qa"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Persist the callback token before the task waits for a human verdict."""
    if event.get("step_name", event.get("step")) == "QA" and event.get("task_token"):
        from oerf.platform.agent.envelope import parse_envelope

        envelope = parse_envelope(event).model_copy(update={"task_token": event["task_token"]})
        dependencies = build_dependencies()
        if dependencies.qa_reviews is None:
            raise RuntimeError("QA review store is not configured")
        dependencies.qa_reviews.put(
            tenant_id=envelope.tenant_id,
            incident_id=envelope.incident_id,
            task_token=envelope.task_token,
        )
        return envelope.model_copy(update={"task_token": None}).to_event()
    return run_step(PHASE, event, context)
