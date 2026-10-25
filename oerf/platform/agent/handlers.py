"""The six cold-chain step handlers and the state-machine dispatch map.

Each handler receives the accumulator envelope, calls domain ports, and returns
a complete replacement envelope. Deterministic rules decide; AI may only
suggest, and its output is recorded separately under ``provenance.suggestion``.

A repeated ``(incident_id, step)`` invocation does not repeat work. The first
completed call stores its whole response under the key, and every later call
replays that stored response, so a Step Functions retry cannot duplicate a
state transition, a message, or a decision write.

Two interfaces are owned elsewhere and are noted here so they are not missed:

* ``docs/interfaces.md`` (OERF-304) freezes these module and function paths.
* The definition (OERF-203/204) reads the gate values a handler returns at the
  state root: ``$.decision.needs_approval``, ``$.approval.status``,
  ``$.facility.accepted``, ``$.containment.status`` and ``$.qa.disposition``.
  ``StepEnvelope`` declares those four gates as optional root fields and
  ``carriers.finish`` mirrors them out of ``workflow``, which stays the
  accumulator that carries them from step to step.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from oerf.platform.agent.carriers import (
    WorkflowStep,
    apply,
    decision,
    field,
    finish,
    mapping,
    text,
)
from oerf.platform.agent.context import (
    MissingStepInput,
    StepDependencies,
    StepError,
    incident_for,
    pinned_config,
    stage_for,
)
from oerf.platform.agent.envelope import StepEnvelope
from oerf.platform.agent.idempotency import StepRecord
from oerf.platform.cadence.policy import DurationError, parse_duration
from oerf.platform.incidents.repository import (
    TERMINAL_STATES,
    TRANSITIONS,
    IllegalTransition,
    StaleIncidentUpdate,
)
from oerf.platform.rules.allowance import calculate_allowance
from oerf.platform.rules.options import (
    AssessmentContext,
    RecoveryOption,
    rank_options,
)
from oerf.platform.tenancy.config_resolver import ClosureConfig, ConfigInvalid, VerificationConfig

Handler = Callable[[StepEnvelope, StepDependencies], StepEnvelope]

#: State-machine ``step`` value to the function that serves it.
HANDLERS: dict[str, Handler] = {}


def handles(*steps: str) -> Callable[[Handler], Handler]:
    """Register a handler under one or more state-machine step values."""

    def register(handler: Handler) -> Handler:
        for step in steps:
            HANDLERS[step] = handler
        return handler

    return register


def _replayed(envelope: StepEnvelope, record: StepRecord) -> StepEnvelope | None:
    """Rebuild a finished step's response, or ``None`` when nothing was stored."""
    if record is None or record.step_output is None:
        return None
    try:
        stored = StepEnvelope.model_validate(record.step_output)
    except ValidationError:
        return None
    return stored.model_copy(
        update={
            "step": envelope.step,
            "tenant_id": envelope.tenant_id,
            "incident_id": envelope.incident_id,
            "shipment_id": envelope.shipment_id,
            "task_token": envelope.task_token,
        }
    )


def _required_mapping(source: Mapping[str, Any] | None, key: str, step: str) -> dict[str, Any]:
    """Read a required nested mapping, or fail the step with a usable message."""
    found = mapping(source, key)
    if not found:
        raise MissingStepInput(f"step {step} needs '{key}' in the workflow")
    return found


def _replay(envelope: StepEnvelope, dependencies: StepDependencies) -> StepEnvelope | None:
    """The response this step already produced, when it ran before.

    A step asks this first, so a retried invocation returns the stored outcome
    instead of repeating a transition, a message, or a decision write.
    """
    return _replayed(
        envelope,
        dependencies.results.get(
            tenant_id=envelope.tenant_id, incident_id=envelope.incident_id, step=envelope.step
        ),
    )


@handles("GatherContext")
def gather_context(envelope: StepEnvelope, dependencies: StepDependencies) -> StepEnvelope:
    """Assemble the context the later deterministic rules read.

    Config comes from the incident's pinned snapshot, so the temperature range,
    the excursion allowance, and the assessment parameters are the ones this
    incident opened with. The trigger reading and the candidate facilities are
    the facts the step was handed; nothing here calls an external service.
    """
    replay = _replay(envelope, dependencies)
    if replay is not None:
        return replay

    workflow = dict(envelope.workflow)
    trigger = _required_mapping(workflow, "trigger", envelope.step)
    temperature = field(trigger, "temperature_c")
    if temperature is None:
        raise MissingStepInput("the trigger has no 'temperature_c' reading")
    incident = incident_for(envelope, dependencies)
    config = pinned_config(incident, dependencies)
    resolved = config.resolved
    shipment = mapping(workflow, "shipment") or {}
    profile_id = field(shipment, "product_profile_id") or field(shipment, "profile_id")
    if not profile_id:
        raise MissingStepInput("the workflow needs shipment.product_profile_id")
    try:
        profile = resolved.product_profile(str(profile_id))
    except ConfigInvalid as exc:
        raise MissingStepInput(f"the pinned config has no product profile {profile_id!r}") from exc

    context = {
        "shipment": shipment,
        "product_profile_id": str(profile_id),
        "range_c": list(profile.range_c),
        "allowance_minutes": profile.excursion_allowance_min,
        "assessment": resolved.assessment.model_dump(),
        "trigger": mapping(workflow, "trigger") or {},
        "facilities": field(workflow, "facilities") or field(envelope.context, "facilities") or [],
        "config_version": config.version,
        "config_source": config.source,
    }
    workflow["context"] = context
    workflow["config_version"] = config.version
    workflow["config_source"] = config.source
    return apply(
        WorkflowStep(
            step=envelope.step,
            workflow=workflow,
            context=context,
            incident_state="DIAGNOSING",
        ),
        envelope,
        dependencies,
    )


def _moment(value: Any, what: str) -> datetime:
    """Read a timezone-aware ISO timestamp, or fail the step with a usable message."""
    if not isinstance(value, str):
        raise MissingStepInput(f"the workflow needs a '{what}' timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MissingStepInput(f"{what} {value!r} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise MissingStepInput(f"{what} {value!r} has no timezone")
    return parsed


def _minutes(since: datetime, until: datetime) -> float:
    """Elapsed minutes between two instants. Never negative, so a clock skew cannot."""
    return max(0.0, (until - since).total_seconds() / 60.0)


def _minutes_between(start: Any, end: datetime, *, what: str = "ship_time") -> float:
    """Elapsed minutes between an ISO timestamp and now. Never negative."""
    return _minutes(_moment(start, what), end)


def _range_c(context: Mapping[str, Any], step: str) -> tuple[float, float]:
    """The product's safe range, as the pinned profile states it."""
    limits = field(context, "range_c")
    if not isinstance(limits, Sequence) or isinstance(limits, (str, bytes)) or len(limits) != 2:
        raise MissingStepInput(f"step {step} needs the product's 'range_c' in the context")
    lower, upper = float(limits[0]), float(limits[1])
    if lower >= upper:
        raise MissingStepInput(f"step {step} read an inverted 'range_c' {limits!r}")
    return lower, upper


def _ship_time(workflow: Mapping[str, Any], context: Mapping[str, Any] | None) -> Any:
    return (
        field(workflow, "ship_time")
        or field(workflow, "departed_at")
        or field(context, "ship_time")
    )


def _recovery_options(context: Mapping[str, Any], include_continue: bool) -> list[RecoveryOption]:
    """Build rule inputs from the gathered context, rejecting junk candidates."""
    candidates = field(context, "facilities")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise MissingStepInput("the gathered context has no facility candidates")
    options: list[RecoveryOption] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise MissingStepInput("a facility candidate is not a mapping")
        options.append(
            RecoveryOption(
                id=str(field(candidate, "id") or field(candidate, "facility_id") or ""),
                drive_min=float(field(candidate, "drive_min") or 0.0),
                capacity_available=bool(field(candidate, "capacity_available")),
            )
        )
    if include_continue:
        options.append(RecoveryOption(id="continue", drive_min=0.0, kind="continue"))
    return options


def _within_pre_approved_limits(resolved: Any) -> bool:
    """Whether an approved recovery needs no human sign-off."""
    approval = resolved.approval
    if "recovery_option" not in approval.required_for:
        return True
    if not approval.auto_proceed_if_within_limits:
        return False
    limits = approval.pre_approved
    return limits.max_spend > 0 and limits.currency != ""


@handles("Assess")
def assess(envelope: StepEnvelope, dependencies: StepDependencies) -> StepEnvelope:
    """Rank deterministic recovery options and decide whether a human is needed.

    A facility is feasible when its projected excursion stays inside the pinned
    allowance. The recommendation and the approval requirement come from the
    rules, never from a model. Approval is required when a recovery option is
    in the configured ``required_for`` list and the pre-approved limits do not
    cover the situation; when nothing is feasible the incident escalates.
    """
    replay = _replay(envelope, dependencies)
    if replay is not None:
        return replay

    workflow = dict(envelope.workflow)
    context = _required_mapping(workflow, "context", envelope.step)
    incident = incident_for(envelope, dependencies)
    config = pinned_config(incident, dependencies)
    resolved = config.resolved
    assessment = resolved.assessment
    trigger = _required_mapping(context, "trigger", envelope.step)
    temperature = field(trigger, "temperature_c")
    if temperature is None:
        raise MissingStepInput("the gathered context has no trigger temperature")
    lower, upper = _range_c(context, envelope.step)
    elapsed = _minutes_between(_ship_time(workflow, context), dependencies.now())
    try:
        projection = AssessmentContext(
            upper_c=float(upper),
            allowance_minutes=float(field(context, "allowance_minutes")),
            used_minutes=elapsed,
            temperature_c=float(temperature),
            unload_min=float(assessment.unload_min),
            rise_rate_c_per_min=float(assessment.rise_rate_c_per_min),
            cooldown_rate_c_per_min=float(assessment.cooldown_rate_c_per_min),
            min_spare_allowance_min=float(assessment.min_spare_allowance_min),
        )
        options = _recovery_options(context, assessment.include_continue_option)
    except (TypeError, ValueError, ValidationError) as exc:
        raise MissingStepInput(f"the gathered context cannot be assessed: {exc}") from exc

    ranked = rank_options(projection, options)
    feasible = [option for option in ranked.options if option.feasible]
    needs_approval = bool(feasible) and not _within_pre_approved_limits(resolved)
    if not feasible:
        route = "escalate"
    elif needs_approval:
        route = "approval"
    else:
        route = "auto_proceed"

    outcome = decision(
        "assess.rank_options",
        config=config,
        inputs={
            "temperature_c": float(temperature),
            "range_c": [float(lower), float(upper)],
            "elapsed_minutes": elapsed,
            "allowance_minutes": float(field(context, "allowance_minutes")),
        },
        has_feasible_options=bool(feasible),
        needs_approval=needs_approval,
        recommended_option=ranked.recommended_option,
        within_pre_approved_limits=route == "auto_proceed",
        route=route,
        ranked_options=[option.model_dump() for option in ranked.options],
    )
    workflow["assessment"] = {
        "has_feasible_options": bool(feasible),
        "needs_approval": needs_approval,
        "recommended_option": ranked.recommended_option,
        "route": route,
        "elapsed_minutes": elapsed,
        "options": [option.model_dump() for option in ranked.options],
    }
    if not feasible:
        workflow["escalation"] = {
            "source": envelope.step,
            "reason": "NO_FEASIBLE_OPTION",
            "at": dependencies.stamp(),
        }
    return apply(
        WorkflowStep(
            step=envelope.step,
            workflow=workflow,
            decision=outcome,
            result={"ranked_options": [option.id for option in ranked.options]},
            incident_state="ESCALATED" if not feasible else None,
        ),
        envelope,
        dependencies,
    )


#: Approval statuses that let the workflow act on the recommended recovery.
APPROVED_STATUSES = ("approved", "auto_proceed")


def _gate(envelope: StepEnvelope, workflow: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    """Read a gate record from the workflow accumulator or the envelope root.

    The definition stores each gate at the state root, and the accumulator in
    ``workflow`` carries it forward. Reading both keeps a handler correct when
    the envelope arrives from either side, and an absent gate stays ``None`` so
    the ``Choice`` state that guards this step takes its ``Default`` edge.
    """
    carried = mapping(workflow, name)
    if carried:
        return carried
    projection = getattr(envelope, name, None)
    return dict(projection) if isinstance(projection, Mapping) else None


def _require_approved(workflow: Mapping[str, Any], envelope: StepEnvelope, step: str) -> None:
    """Refuse to act while the approval gate has not said yes.

    The definition only reaches ``Act`` through the approval gate, so this is
    the belt-and-braces half of that guarantee: a step that arrives with a
    denied, expired, or missing approval fails instead of diverting a shipment.
    """
    approval = _gate(envelope, workflow, "approval")
    if approval is None:
        return
    status = text(approval, "status")
    if status not in APPROVED_STATUSES:
        raise StepError(f"step {step} cannot act while the approval is {status or 'unset'}")


def _chosen_option(
    workflow: Mapping[str, Any], assessment: Mapping[str, Any], envelope: StepEnvelope, step: str
) -> dict[str, Any]:
    """The option this step acts on: the approved one, else the recommendation.

    An approver may name a different option than the rules recommended, so an
    explicit choice wins - but it must be one the rules ranked feasible. The
    ranking is the only source of feasibility; nothing is re-derived here.
    """
    ranked = field(assessment, "options")
    if not isinstance(ranked, Sequence) or isinstance(ranked, (str, bytes)):
        raise MissingStepInput(f"step {step} needs the ranked options in the workflow")
    by_id = {
        str(field(option, "id")): dict(option) for option in ranked if isinstance(option, Mapping)
    }
    if not by_id:
        raise MissingStepInput(f"step {step} needs the ranked options in the workflow")
    approval = _gate(envelope, workflow, "approval") or {}
    chosen = text(approval, "option_id") or text(assessment, "chosen_option")
    chosen = chosen or text(assessment, "recommended_option")
    if not chosen:
        raise MissingStepInput(f"step {step} has no option to act on")
    option = by_id.get(chosen)
    if option is None:
        raise MissingStepInput(
            f"step {step} cannot find option {chosen!r} among the ranked options"
        )
    if field(option, "feasible") is not True:
        raise StepError(f"option {chosen!r} is not feasible, so step {step} must not act on it")
    return option


def _facility_for(
    context: Mapping[str, Any], option: Mapping[str, Any], step: str
) -> dict[str, Any] | None:
    """The candidate a facility option names, or ``None`` when continuing."""
    if (text(option, "kind") or "facility") == "continue":
        return None
    facility_id = text(option, "id")
    candidates = field(context, "facilities")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise MissingStepInput(f"step {step} needs the facility candidates in the context")
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        named = field(candidate, "id") or field(candidate, "facility_id")
        if str(named) == facility_id:
            return dict(candidate)
    raise MissingStepInput(f"step {step} cannot find facility {facility_id!r} in the context")


def _require_pre_approved_facility(facility: Mapping[str, Any], resolved: Any, step: str) -> None:
    """Refuse a facility the pinned pre-approved limits do not cover."""
    if not resolved.approval.pre_approved.facility_must_be_approved:
        return
    if field(facility, "approved") is True:
        return
    named = text(facility, "facility_id") or text(facility, "id")
    raise StepError(f"facility {named!r} is not pre-approved, so step {step} must not use it")


@handles("Act")
def act(envelope: StepEnvelope, dependencies: StepDependencies) -> StepEnvelope:
    """Execute the approved recovery and put the shipment back in transit.

    The snapshot read here is the one the incident opened with, so the
    pre-approved limits and the approval gate are the ones the approver saw.
    The recorded action is the moment ``Verify`` measures its stable window
    from. Messaging the driver and the facility is engagement work owned by the
    cadence tickets; this handler records the decision and moves the incident.
    """
    replay = _replay(envelope, dependencies)
    if replay is not None:
        return replay

    workflow = dict(envelope.workflow)
    context = _required_mapping(workflow, "context", envelope.step)
    assessment = _required_mapping(workflow, "assessment", envelope.step)
    incident = incident_for(envelope, dependencies)
    config = pinned_config(incident, dependencies)
    _require_approved(workflow, envelope, envelope.step)

    option = _chosen_option(workflow, assessment, envelope, envelope.step)
    facility = _facility_for(context, option, envelope.step)
    if facility is not None:
        _require_pre_approved_facility(facility, config.resolved, envelope.step)
    facility_id = None
    if facility is not None:
        facility_id = text(facility, "facility_id") or text(facility, "id")
    action = {
        "status": "completed",
        "kind": text(option, "kind") or "facility",
        "option_id": text(option, "id"),
        "facility_id": facility_id,
        "drive_min": float(field(option, "drive_min") or 0.0),
        "at": dependencies.stamp(),
        "config_version": config.version,
        "config_source": config.source,
    }
    outcome = decision(
        "act.execute_recovery",
        config=config,
        inputs={"option_id": action["option_id"], "kind": action["kind"]},
        option_id=action["option_id"],
        facility_id=facility_id,
        status="dispatched",
    )
    workflow["action"] = action
    return apply(
        WorkflowStep(
            step=envelope.step,
            workflow=workflow,
            decision=outcome,
            result={"option_id": action["option_id"], "facility_id": facility_id},
            incident_state="IN_TRANSIT",
        ),
        envelope,
        dependencies,
    )


#: Containment verdicts the definition routes on. Anything else waits.
CONTAINED = "contained"
REOPENED = "reopened"
STABILIZING = "stabilizing"

#: Where a verification reading may sit in the workflow.
_READING_PATHS = (
    ("verification", "temperature_c"),
    (None, "temperature_c"),
    ("trigger", "temperature_c"),
)


def _reading(workflow: Mapping[str, Any], context: Mapping[str, Any], step: str) -> float:
    """The latest temperature reading, from the workflow or the gathered context."""
    for section, key in _READING_PATHS:
        source = workflow if section is None else mapping(workflow, section)
        found = field(source, key)
        if isinstance(found, bool) or not isinstance(found, (int, float)):
            continue
        return float(found)
    found = field(mapping(context, "trigger"), "temperature_c")
    if isinstance(found, bool) or not isinstance(found, (int, float)):
        raise MissingStepInput(f"step {step} needs a temperature reading in the workflow")
    return float(found)


def _minutes_duration(value: Any, what: str) -> float:
    """A configured ISO-8601 duration as minutes, or a usable step failure."""
    if not isinstance(value, str):
        raise MissingStepInput(f"the pinned config has no '{what}' duration")
    try:
        return parse_duration(value).total_seconds() / 60.0
    except DurationError as exc:
        raise MissingStepInput(f"{what} {value!r} is not a usable duration") from exc


def _containment_anchor(workflow: Mapping[str, Any], context: Mapping[str, Any]) -> datetime:
    """When the shipment started being held in range.

    The stable window is measured from the recovery action, because that is when
    the shipment began heading for controlled conditions. A workflow that never
    recorded an action - a replayed or partly stubbed run - falls back to the
    departure time, and a workflow with neither cannot be verified at all.
    """
    action = mapping(workflow, "action")
    candidates = (
        ("action.at", field(action, "at")),
        ("verification.started_at", field(mapping(workflow, "verification"), "started_at")),
        ("ship_time", _ship_time(workflow, context)),
    )
    for what, value in candidates:
        if isinstance(value, str):
            return _moment(value, what)
    raise MissingStepInput("Verify needs the action or departure time to measure stability from")


def _containment_status(
    *,
    in_range: bool,
    reading: float,
    upper: float,
    stable_minutes: float,
    stable_required: float,
    allowance_status: str,
    was_contained: bool,
    reopen_on_rise: bool,
) -> str:
    """The verdict, from the pinned window, the range, and the allowance.

    An in-range reading that held for the whole window is contained, whatever
    the allowance says. A reading that rose back above the range after a
    containment is a reopen when the config asks to reopen on a rise; a reading
    that stayed out until the allowance ran out reopens too, because the
    recovery can no longer be trusted and waiting longer decides nothing.
    """
    if in_range and stable_minutes >= stable_required:
        return CONTAINED
    if in_range:
        return STABILIZING
    if reopen_on_rise and was_contained and reading > upper:
        return REOPENED
    if allowance_status != "WITHIN_ALLOWANCE":
        return REOPENED
    return STABILIZING


@handles("Verify")
def verify(envelope: StepEnvelope, dependencies: StepDependencies) -> StepEnvelope:
    """Check that the recovery held the product inside its range.

    The verdict is a deterministic read of three pinned facts: the product
    range, the configured stable window, and how much excursion allowance is
    left. A reading back in range that stayed there for the whole window
    contains the incident; a reading that rose again after containment, or one
    that ran the allowance out, reopens it; anything else waits, and the
    definition comes back around after its wait state.

    An outcome that is not yet a verdict is reported but not stored, so the loop
    re-measures instead of replaying a "not yet" forever.
    """
    replay = _replay(envelope, dependencies)
    if replay is not None:
        return replay

    workflow = dict(envelope.workflow)
    context = _required_mapping(workflow, "context", envelope.step)
    incident = incident_for(envelope, dependencies)
    config = pinned_config(incident, dependencies)
    verification: VerificationConfig = config.resolved.verification
    lower, upper = _range_c(context, envelope.step)
    reading = _reading(workflow, context, envelope.step)
    now = dependencies.now()
    previous = _gate(envelope, workflow, "containment") or {}
    anchor = _containment_anchor(workflow, context)
    stable_minutes = _minutes(anchor, now)
    stable_required = _minutes_duration(
        verification.stable_in_range, "verification.stable_in_range"
    )
    in_range = lower <= reading <= upper
    allowance = calculate_allowance(
        float(field(context, "allowance_minutes")),
        _minutes_between(_ship_time(workflow, context), now),
    )
    status = _containment_status(
        in_range=in_range,
        reading=reading,
        upper=upper,
        stable_minutes=stable_minutes,
        stable_required=stable_required,
        allowance_status=allowance.status,
        was_contained=text(previous, "status") == CONTAINED,
        reopen_on_rise=verification.reopen_on_rise,
    )
    containment = {
        "status": status,
        "temperature_c": reading,
        "range_c": [lower, upper],
        "in_range": in_range,
        "stable_minutes": stable_minutes,
        "stable_required_min": stable_required,
        "previous_status": text(previous, "status"),
        "allowance": allowance.model_dump(),
        "checked_at": dependencies.stamp(),
        "config_version": config.version,
        "config_source": config.source,
    }
    outcome = decision(
        "verify.containment",
        config=config,
        inputs={
            "temperature_c": reading,
            "range_c": [lower, upper],
            "stable_minutes": stable_minutes,
            "allowance_status": allowance.status,
        },
        status=status,
        in_range=in_range,
        stable_required_min=stable_required,
        reopened=status == REOPENED,
    )
    workflow["containment"] = containment
    return apply(
        WorkflowStep(
            step=envelope.step,
            workflow=workflow,
            decision=outcome,
            result={"status": status, "temperature_c": reading},
            incident_state="CONTAINED",
            current_stage="CONTAINED" if status == CONTAINED else "VERIFYING",
            hold=status != CONTAINED,
        ),
        envelope,
        dependencies,
        remember=status == CONTAINED,
    )


#: Where the reviewer's verdict may sit in the workflow or the context.
_REVIEW_KEYS = ("qa_review", "review", "qa")


def _submitted_review(
    envelope: StepEnvelope, workflow: Mapping[str, Any], step: str
) -> dict[str, Any]:
    """The QA review the reviewer submitted, wherever the envelope carries it.

    The verdict is a human input, so it arrives on the envelope rather than
    being derived here. Several shapes are accepted so the app stack is not
    forced into one nesting: a review mapping, a flat set of fields, or the
    review inside the context the step inherited.
    """
    sources: list[Mapping[str, Any]] = [workflow, envelope.context]
    for source in (workflow, envelope.context):
        for key in _REVIEW_KEYS:
            nested = mapping(source, key)
            if nested:
                sources.append(nested)
    for source in sources:
        if "approved" in source or "disposition" in source:
            return dict(source)
    raise MissingStepInput(f"step {step} needs the reviewer's verdict on the envelope")


def _review_disposition(
    review: Mapping[str, Any], closure: ClosureConfig, step: str
) -> tuple[bool, str]:
    """The reviewer's decision and disposition, checked against the pinned list."""
    approved = field(review, "approved")
    if not isinstance(approved, bool):
        raise MissingStepInput(f"step {step} needs the reviewer's 'approved' as a boolean")
    disposition = text(review, "disposition") or text(review, "closure_disposition")
    if disposition is None:
        raise MissingStepInput(
            f"step {step} needs a disposition from {', '.join(closure.dispositions)}"
        )
    if disposition not in closure.dispositions:
        raise StepError(
            f"disposition {disposition!r} is not one of the configured "
            f"{', '.join(closure.dispositions)}"
        )
    return approved, disposition


@handles("QA")
def qa(envelope: StepEnvelope, dependencies: StepDependencies) -> StepEnvelope:
    """Record the reviewer's verdict and the disposition they chose.

    QA is a gate, not a state: it reports a stage and a decision without moving
    the incident, and the definition's ``Choice`` reads the verdict from
    ``qa.disposition``. The verdict is a boolean the reviewer supplied; the
    disposition must be one of the pinned closure dispositions, so a typo
    cannot reach the closure. A rejection records why, and the definition
    escalates.
    """
    replay = _replay(envelope, dependencies)
    if replay is not None:
        return replay

    workflow = dict(envelope.workflow)
    incident = incident_for(envelope, dependencies)
    config = pinned_config(incident, dependencies)
    closure: ClosureConfig = config.resolved.closure
    review = _submitted_review(envelope, workflow, envelope.step)
    approved, disposition = _review_disposition(review, closure, envelope.step)
    reviewer_role = text(review, "reviewer_role") or text(review, "role") or closure.closer_role
    reviewed_at = text(review, "at") or text(review, "reviewed_at") or dependencies.stamp()
    qa_record = {
        "disposition": "approved" if approved else "rejected",
        "closure_disposition": disposition,
        "approved": approved,
        "reviewer_role": reviewer_role,
        "reviewed_at": reviewed_at,
        "config_version": config.version,
        "config_source": config.source,
    }
    outcome = decision(
        "qa.review",
        config=config,
        inputs={"disposition": disposition, "reviewer_role": reviewer_role},
        approved=approved,
        disposition=qa_record["disposition"],
        closure_disposition=disposition,
    )
    workflow["qa"] = qa_record
    if not approved:
        workflow["escalation"] = {
            "source": envelope.step,
            "reason": "QA_REJECTED",
            "at": dependencies.stamp(),
        }
    return apply(
        WorkflowStep(
            step=envelope.step,
            workflow=workflow,
            decision=outcome,
            result={"disposition": qa_record["disposition"]},
        ),
        envelope,
        dependencies,
    )


@handles("Close")
def close(envelope: StepEnvelope, dependencies: StepDependencies) -> StepEnvelope:
    """Close the incident once QA approved it, with the disposition it chose.

    Closing needs the QA gate to be present and approved: a workflow that
    reaches here without it - a hand-driven run, or a replay from another
    system - fails instead of closing on trust. The disposition is re-checked
    against the pinned closure list, so the closure records a value the
    configuration still recognises.
    """
    replay = _replay(envelope, dependencies)
    if replay is not None:
        return replay

    workflow = dict(envelope.workflow)
    incident = incident_for(envelope, dependencies)
    config = pinned_config(incident, dependencies)
    closure: ClosureConfig = config.resolved.closure
    gate = _gate(envelope, workflow, "qa")
    if gate is None:
        raise MissingStepInput("Close needs the QA verdict in the workflow")
    if text(gate, "disposition") != "approved":
        raise StepError(
            f"step {envelope.step} cannot close while QA is {text(gate, 'disposition') or 'unset'}"
        )
    disposition = text(gate, "closure_disposition")
    if disposition not in closure.dispositions:
        raise StepError(
            f"disposition {disposition!r} is not one of the configured "
            f"{', '.join(closure.dispositions)}"
        )
    record = {
        "status": "closed",
        "disposition": disposition,
        "closer_role": closure.closer_role,
        "reviewed_by": text(gate, "reviewer_role") or closure.closer_role,
        "closed_at": dependencies.stamp(),
        "config_version": config.version,
        "config_source": config.source,
    }
    outcome = decision(
        "close.record_disposition",
        config=config,
        inputs={"disposition": disposition},
        status="closed",
        disposition=disposition,
    )
    workflow["closure"] = record
    workflow["closed"] = True
    return apply(
        WorkflowStep(
            step=envelope.step,
            workflow=workflow,
            decision=outcome,
            result={"disposition": disposition},
            incident_state="CLOSED",
        ),
        envelope,
        dependencies,
    )


def _remember(
    envelope: StepEnvelope,
    dependencies: StepDependencies,
    output: StepEnvelope,
    *,
    result: dict[str, Any],
) -> StepEnvelope:
    """Store the step outcome without moving the incident."""

    dependencies.results.put(
        tenant_id=envelope.tenant_id,
        incident_id=envelope.incident_id,
        step=envelope.step,
        result=result,
        step_output=output.to_event(),
    )
    return output


def _kept(
    envelope: StepEnvelope,
    *,
    incident_state: str | None,
    current_stage: str | None,
) -> StepEnvelope:
    """A replacement envelope that keeps the workflow and the given state."""

    return finish(
        WorkflowStep(
            step=envelope.step,
            workflow=dict(envelope.workflow),
            incident_state=incident_state,
            current_stage=current_stage,
        ),
        envelope,
    )


def _one_hop(
    envelope: StepEnvelope, dependencies: StepDependencies, target: str
) -> tuple[str, bool]:
    """Move the stored incident one legal edge, or report that it was not moved.

    Returns ``(state, wrote)``. A terminal incident, or one already at
    ``target``, is not written. Any other state without a direct edge to
    ``target`` is an error: this does not walk a path of several hops.
    """

    incident = incident_for(envelope, dependencies)
    if incident.state in TERMINAL_STATES or incident.state == target:
        return incident.state, False
    if target not in TRANSITIONS.get(incident.state, frozenset()):
        raise StepError(
            f"incident {incident.incident_id} is {incident.state}; "
            f"{envelope.step} cannot move it to {target}"
        )
    try:
        moved = dependencies.incidents.transition(
            envelope.tenant_id,
            envelope.incident_id,
            from_state=incident.state,
            to_state=target,
        )
    except IllegalTransition as exc:
        raise StepError(str(exc)) from exc
    except StaleIncidentUpdate:
        latest = incident_for(envelope, dependencies)
        if latest.state == target or latest.state in TERMINAL_STATES:
            return latest.state, False
        raise StepError(
            f"incident {incident.incident_id} is {latest.state}; "
            f"{envelope.step} cannot move it to {target}"
        ) from None
    return moved.state, True


def _apply_hop(
    envelope: StepEnvelope, dependencies: StepDependencies, target: str
) -> StepEnvelope:
    """Apply one hop and remember it, unless the incident was already terminal."""

    state, wrote = _one_hop(envelope, dependencies, target)
    output = _kept(envelope, incident_state=state, current_stage=state)
    if not wrote and state in TERMINAL_STATES:
        return output
    return _remember(
        envelope,
        dependencies,
        output,
        result={"incident_state": state, "transitioned": wrote},
    )


@handles("RecordFailure")
def record_failure(envelope: StepEnvelope, dependencies: StepDependencies) -> StepEnvelope:
    """Remember that a step failed, without moving the incident.

    ``FailureRoute`` reads ``incident_state`` from this response, so the value
    that arrived is the value that leaves. No timeline event is written and the
    shipment lock is left as it was.
    """

    replay = _replay(envelope, dependencies)
    if replay is not None:
        return replay
    output = _kept(
        envelope,
        incident_state=envelope.incident_state,
        current_stage=envelope.current_stage or stage_for(envelope.step),
    )
    return _remember(
        envelope,
        dependencies,
        output,
        result={"incident_state": envelope.incident_state, "transitioned": False},
    )


@handles("Escalate")
def escalate(envelope: StepEnvelope, dependencies: StepDependencies) -> StepEnvelope:
    """Move the incident to ``ESCALATED`` and keep the shipment lock.

    Assess may already have walked here. A second call does not write another
    timeline event. ``CLOSED`` and ``CANCELLED`` succeed with no write. Any
    other state that has no direct edge to ``ESCALATED`` fails.
    """

    replay = _replay(envelope, dependencies)
    if replay is not None:
        return replay
    return _apply_hop(envelope, dependencies, "ESCALATED")


@handles("Cancel")
def cancel(envelope: StepEnvelope, dependencies: StepDependencies) -> StepEnvelope:
    """Move the incident to ``CANCELLED`` and release the shipment lock.

    ``CLOSED`` and ``CANCELLED`` succeed with no write. Every other stored
    state has a direct edge to ``CANCELLED``.
    """

    replay = _replay(envelope, dependencies)
    if replay is not None:
        return replay
    return _apply_hop(envelope, dependencies, "CANCELLED")
