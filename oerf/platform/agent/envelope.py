"""The accumulator envelope every cold-chain step handler reads and returns.

The state machine hands each task the whole workflow document under the
``workflow`` key and replaces the state with whatever the task returns, so a
handler receives the current envelope and must return a complete replacement:
the identifiers it was given, the ``workflow`` and ``context`` it may have
extended, and the stage, completed step, and decision that the following
``Choice`` and ``Task`` states read.

The identifiers stay on the envelope rather than only inside ``workflow`` so
that a handler in the middle of the workflow can call a tenant-scoped port
without re-reading the execution input.

Four gate values are also mirrored onto the root of the response. The
definition's ``Choice`` states read ``$.approval.status``, ``$.facility.accepted``,
``$.containment.status`` and ``$.qa.disposition`` from the state itself, and the
state is this envelope, so those four keys are declared here and projected up
from ``workflow`` by ``carriers.finish``. ``workflow`` stays the accumulator: the
mirror is a copy of the value the step wrote there, carried forward by every
later step so a gate cannot disappear mid-workflow.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

OPEN = "OPEN"
GATHER_CONTEXT = "GatherContext"
DIAGNOSE = "Diagnose"
ASSESS = "Assess"
APPROVAL = "Approval"
SECURE_FACILITY = "SecureFacility"
ACT = "Act"
VERIFY = "Verify"
HANDLE_REOPEN = "HandleReopen"
GENERATE_REPORT = "GenerateReport"
QA = "QA"
CLOSE = "Close"
RECORD_FAILURE = "RecordFailure"
ESCALATE = "Escalate"
CANCEL = "Cancel"

IDENTIFIERS = ("tenant_id", "incident_id", "shipment_id")

#: Gate values the definition's ``Choice`` states read at the state root. Each
#: is mirrored from the same key in ``workflow``, and stays optional: a step
#: that never touched a gate returns no mirror, so the ``Choice`` sees an absent
#: path and takes its ``Default``.
MIRRORS = ("approval", "facility", "containment", "qa")

#: Every step the definition can dispatch. The six handlers OERF-202 delivers
#: cover a subset of these; the rest arrive with OERF-203, OERF-206 and OERF-207.
STEPS = frozenset(
    {
        OPEN,
        GATHER_CONTEXT,
        DIAGNOSE,
        ASSESS,
        APPROVAL,
        SECURE_FACILITY,
        ACT,
        VERIFY,
        HANDLE_REOPEN,
        GENERATE_REPORT,
        QA,
        CLOSE,
        RECORD_FAILURE,
        ESCALATE,
        CANCEL,
    }
)

#: Keys a task event may carry into ``parse_envelope``. Catch results such as
#: ``failure`` and stub flags such as ``no_response`` are dropped so
#: ``extra=forbid`` still rejects a misspelled identifier.
TASK_EVENT_KEYS = frozenset(
    {
        "step",
        "step_name",
        "tenant_id",
        "incident_id",
        "shipment_id",
        "workflow",
        "context",
        "incident_state",
        "current_stage",
        "last_completed_step",
        "decision",
        "approval",
        "facility",
        "containment",
        "qa",
        "task_token",
    }
)


class EnvelopeError(ValueError):
    """The event is not a usable workflow envelope."""


class StepEnvelope(BaseModel):
    """One hop of the workflow: what a handler is given and what it returns."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    step: str = Field(validation_alias=AliasChoices("step", "step_name"))
    tenant_id: str
    incident_id: str
    shipment_id: str
    workflow: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    incident_state: str | None = None
    current_stage: str | None = None
    last_completed_step: str | None = None
    decision: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None
    facility: dict[str, Any] | None = None
    containment: dict[str, Any] | None = None
    qa: dict[str, Any] | None = None
    task_token: str | None = None

    @field_validator("step")
    @classmethod
    def _known_step(cls, value: str) -> str:
        if value not in STEPS:
            raise ValueError(f"unknown workflow step {value!r}")
        return value

    @field_validator(*IDENTIFIERS)
    @classmethod
    def _identifier(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must be a non-empty string")
        return text

    def to_event(self) -> dict[str, Any]:
        """Return the Lambda response body for this envelope.

        Unset optionals are dropped so a ``Choice`` state sees an absent path
        rather than a null one.
        """
        return self.model_dump(exclude_none=True)


def narrow_task_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Drop keys ``StepEnvelope`` does not declare.

    The state machine wraps the whole state as ``workflow``. A ``Catch`` adds
    ``failure`` on that state. Filtering the wrapper, and the nested state when
    the identifiers are inside it, leaves the accumulator ``workflow`` intact.
    """

    if not isinstance(event, Mapping):
        raise EnvelopeError(f"event must be a mapping, not {type(event).__name__}")
    narrowed = {key: value for key, value in event.items() if key in TASK_EVENT_KEYS}
    nested = narrowed.get("workflow")
    if isinstance(nested, Mapping) and not any(name in narrowed for name in IDENTIFIERS):
        narrowed["workflow"] = {
            key: value for key, value in nested.items() if key in TASK_EVENT_KEYS
        }
    return narrowed


def parse_envelope(event: Mapping[str, Any]) -> StepEnvelope:
    """Build an envelope from a state-machine task event.

    Two shapes are accepted:

    * the state-machine convention, ``{"step": ..., "workflow": <envelope>}``,
      which is what the definition's ``"workflow.$": "$"`` payload produces;
    * a flat call, ``{"step": ..., "tenant_id": ..., ...}``, for direct
      invocation and tests.

    The first task of an execution receives the start input under ``workflow``,
    so the identifiers are read from either place. A missing or blank
    identifier, a missing ``step``, an unknown ``step``, or an unexpected field
    raises :class:`EnvelopeError`.
    """
    if not isinstance(event, Mapping):
        raise EnvelopeError(f"event must be a mapping, not {type(event).__name__}")

    step = event.get("step") or event.get("step_name")
    if not step:
        raise EnvelopeError("event has no 'step' key")

    root: Mapping[str, Any] = event
    if not any(name in event for name in IDENTIFIERS):
        nested = event.get("workflow")
        if isinstance(nested, Mapping):
            root = nested

    payload = {key: value for key, value in root.items() if key not in ("step", "step_name")}
    payload["step"] = step
    try:
        return StepEnvelope.model_validate(payload)
    except ValidationError as error:
        raise EnvelopeError(f"invalid workflow envelope: {error}") from error
