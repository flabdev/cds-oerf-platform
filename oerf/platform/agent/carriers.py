"""Shared pieces of a step outcome: the new envelope, and decision provenance.

A step returns a complete replacement envelope. The identifiers it was given
come back untouched, ``workflow`` and ``context`` carry everything it learned,
and ``incident_state``, ``current_stage``, ``last_completed_step`` and
``decision`` are what the following ``Choice`` and ``Task`` states read.

Every decision also records where it came from. Deterministic rules are the
only thing that may set a state, an option, an approval requirement, or a
disposition. AI may interpret and draft, so its output is kept in ``suggestion``
with ``ai_assisted`` true and ``authoritative`` false; nothing downstream may
act on it without a rule or a human agreeing first.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from oerf.platform.agent.context import PinnedConfig, StepDependencies, advance, stage_for
from oerf.platform.agent.envelope import MIRRORS, StepEnvelope

RULE_SOURCE = "deterministic_rules"

__all__ = [
    "RULE_SOURCE",
    "WORKFLOW_KEYS",
    "WorkflowStep",
    "apply",
    "decision",
    "field",
    "finish",
    "mapping",
    "provenance",
    "text",
]

#: Workflow keys a step owns. Everything else a step was handed survives.
WORKFLOW_KEYS = (
    "reason",
    "domain",
    "trigger",
    "incident_state",
    "current_stage",
    "last_completed_step",
    "decision",
    "context",
    "config_version",
    "config_source",
)


@dataclass(frozen=True)
class WorkflowStep:
    """One step's result before it becomes an envelope."""

    step: str
    workflow: dict[str, Any]
    decision: dict[str, Any] | None = None
    result: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] | None = None
    incident_state: str | None = None
    current_stage: str | None = None
    hold: bool = False


def provenance(
    rule: str,
    *,
    config: PinnedConfig,
    inputs: Mapping[str, Any] | None = None,
    ai: Any | None = None,
) -> dict[str, Any]:
    """An auditable record of which rule decided, on which configuration.

    ``ai`` is separate and never authoritative. It is stored so a reviewer can
    see what a suggestion said, not so a step can act on it.
    """
    record: dict[str, Any] = {
        "source": RULE_SOURCE,
        "rule": rule,
        "ai_assisted": False,
        **config.provenance(),
    }
    if inputs:
        record["inputs"] = dict(inputs)
    if ai is not None:
        record["ai_assisted"] = True
        record["suggestion"] = ai
    return record


def decision(
    rule: str,
    *,
    config: PinnedConfig,
    inputs: Mapping[str, Any] | None = None,
    ai: Any | None = None,
    **values: Any,
) -> dict[str, Any]:
    """A decision payload: the rule, its provenance, and its outcome.

    ``ai`` is a model suggestion. It is recorded in the provenance beside the
    rule and never changes the outcome the rule produced, so a reviewer can see
    what a suggestion said without the workflow acting on it.
    """
    return {
        "rule": rule,
        "provenance": provenance(rule, config=config, inputs=inputs, ai=ai),
        **values,
    }


def field(source: Mapping[str, Any] | None, key: str) -> Any:
    """Read a key from a mapping that may be absent."""
    return source.get(key) if isinstance(source, Mapping) else None


def text(source: Mapping[str, Any] | None, key: str) -> str | None:
    """Read a non-blank string from a mapping that may be absent."""
    value = field(source, key)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def mapping(source: Mapping[str, Any] | None, key: str) -> dict[str, Any] | None:
    """Read a nested mapping, copying it so the caller owns the result."""
    value = field(source, key)
    return deepcopy(dict(value)) if isinstance(value, Mapping) else None


def finish(step: WorkflowStep, envelope: StepEnvelope) -> StepEnvelope:
    """Build the replacement envelope.

    ``workflow`` is what the step assembled, ``context`` is what it gathered or
    the context it inherited, and the stage and completed step are this step's.
    The identifiers are the ones the step was given. The gate values the
    following ``Choice`` states read are mirrored from ``workflow``.
    """
    mirrors = _mirrors(step.workflow, envelope)
    return StepEnvelope(
        step=envelope.step,
        tenant_id=envelope.tenant_id,
        incident_id=envelope.incident_id,
        shipment_id=envelope.shipment_id,
        workflow=deepcopy(step.workflow),
        context=deepcopy(step.context if step.context is not None else envelope.context),
        incident_state=step.incident_state,
        current_stage=step.current_stage or stage_for(step.step),
        last_completed_step=step.step,
        decision=deepcopy(step.decision),
        **mirrors,
        task_token=envelope.task_token,
    )


def _mirrors(workflow: Mapping[str, Any], envelope: StepEnvelope) -> dict[str, dict[str, Any]]:
    """The root copies of the gate values a ``Choice`` state evaluates.

    A step writes a gate into ``workflow`` like any other finding, and the
    definition reads the same value at the state root. A step that did not touch
    a gate carries the value it was handed forward, so a gate an earlier step
    set is still readable after this one has run.
    """
    projected: dict[str, dict[str, Any]] = {}
    for name in MIRRORS:
        written = field(workflow, name)
        mirror = written if isinstance(written, Mapping) else getattr(envelope, name, None)
        if isinstance(mirror, Mapping):
            projected[name] = deepcopy(dict(mirror))
    return projected


def apply(
    step: WorkflowStep,
    envelope: StepEnvelope,
    dependencies: StepDependencies,
    *,
    remember: bool = True,
) -> StepEnvelope:
    """Move the incident this step owns, then store the outcome once.

    The order matters. The state change happens first, so a step that cannot
    move the incident never leaves a stored result behind, and the following
    ``Choice`` state never sees a decision that was not applied.

    ``remember`` is false for an outcome that is not yet a decision - a
    verification that is still waiting for the reading to settle. Such a step
    reports what it found and stores nothing, so the wait-and-re-check edge of
    the definition recomputes instead of replaying a stale "not yet".

    ``hold`` on the step keeps the incident where it is, for the same kind of
    verdict that reports without moving anything.
    """
    moved = advance(envelope, dependencies, to_state=step.incident_state, hold=step.hold)
    output = finish(
        WorkflowStep(
            step=step.step,
            workflow=step.workflow,
            decision=step.decision,
            result=step.result,
            context=step.context,
            incident_state=moved["incident_state"],
            current_stage=step.current_stage,
        ),
        envelope,
    )
    if not remember:
        return output
    dependencies.results.put(
        tenant_id=envelope.tenant_id,
        incident_id=envelope.incident_id,
        step=step.step,
        decision=deepcopy(step.decision),
        result=deepcopy({"transition": moved, **step.result}),
        step_output=deepcopy(output.to_event()),
    )
    return output
