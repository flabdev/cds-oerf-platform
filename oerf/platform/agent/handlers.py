"""The cold-chain step handlers and the state-machine dispatch map.

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
from oerf.platform.cadence.policy import (
    DurationError,
    load_policies,
    parse_duration,
    select_policy,
)
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


@handles("OPEN")
def open_incident(envelope: StepEnvelope, dependencies: StepDependencies) -> StepEnvelope:
    """Record the incident ingest opened, and pin the configuration it decides on.

    ``OPEN`` was the last cold-chain state still served by the workflow stub. The
    ingest rule has already created the incident and taken the shipment lock, so
    this step only confirms the incident really is ``OPEN``, records the pinned
    configuration the run will decide on, and hands the same envelope to
    ``GatherContext``. It does not move the incident, so a retried invocation
    replays the stored response instead of writing twice.

    The state-machine wiring that points ``OPEN`` at a real function is C0's job;
    this handler is the deterministic behaviour it will invoke.
    """
    replay = _replay(envelope, dependencies)
    if replay is not None:
        return replay

    workflow = dict(envelope.workflow)
    incident = incident_for(envelope, dependencies)
    if incident.state != "OPEN":
        raise StepError(
            f"incident {incident.incident_id} is {incident.state}; "
            f"step {envelope.step} must run while it is OPEN"
        )
    config = pinned_config(incident, dependencies)
    workflow["opened_at"] = incident.opened_at
    workflow["config_version"] = config.version
    workflow["config_source"] = config.source
    outcome = decision(
        "open.record_incident",
        config=config,
        inputs={"shipment_id": envelope.shipment_id, "reason": incident.reason},
        status="open",
    )
    return apply(
        WorkflowStep(
            step=envelope.step,
            workflow=workflow,
            decision=outcome,
            result={"status": "open"},
            incident_state="OPEN",
            current_stage="OPEN",
        ),
        envelope,
        dependencies,
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


_DIAGNOSE_ROLE = "driver"
_DIAGNOSE_ASK = "diagnose"


def _facility_candidate(row: Mapping[str, Any]) -> dict[str, Any]:
    """Copy one catalog row. Drive time and identity stay the values on the row."""

    facility_id = text(row, "facility_id") or text(row, "id")
    drive = field(row, "drive_min")
    if facility_id is None:
        raise MissingStepInput("a facility row has no facility_id")
    if isinstance(drive, bool) or not isinstance(drive, (int, float)):
        raise MissingStepInput("a facility row has no drive_min")
    if "capacity_available" not in row or "approved" not in row:
        raise MissingStepInput("a facility row is missing capacity or approval")
    candidate = {
        "id": facility_id,
        "facility_id": facility_id,
        "drive_min": float(drive),
        "capacity_available": bool(row["capacity_available"]),
        "approved": bool(row["approved"]),
    }
    contact_id = text(row, "contact_id")
    if contact_id is not None:
        candidate["contact_id"] = contact_id
    return candidate


@handles("Diagnose")
def diagnose(envelope: StepEnvelope, dependencies: StepDependencies) -> StepEnvelope:
    """Name the driver engagement and load the tenant's facilities.

    The policy id is the pinned cadence entry for this role and ask. Facility
    ids and drive times are the tenant table's. This step does not move the
    incident. The driver engagement runs next, and ``Assess`` ranks after it.
    """

    replay = _replay(envelope, dependencies)
    if replay is not None:
        return replay
    if dependencies.facilities is None:
        raise MissingStepInput("Diagnose needs the facility catalog")

    workflow = dict(envelope.workflow)
    incident = incident_for(envelope, dependencies)
    config = pinned_config(incident, dependencies)
    policy = select_policy(
        _DIAGNOSE_ROLE,
        _DIAGNOSE_ASK,
        dict(config.resolved.cadence),
        load_policies(),
    )
    facilities = [
        _facility_candidate(row)
        for row in dependencies.facilities.list_facilities(envelope.tenant_id)
    ]
    gathered = dict(mapping(workflow, "context") or envelope.context)
    gathered["facilities"] = facilities
    workflow["context"] = gathered
    if _ship_time(workflow, gathered) is None:
        trigger = mapping(workflow, "trigger") or {}
        stamp = text(trigger, "timestamp")
        if stamp is None:
            raise MissingStepInput("Diagnose needs the trigger timestamp to set ship_time")
        workflow["ship_time"] = stamp
    workflow["diagnosis"] = {
        "engagement_id": f"ENG-{envelope.incident_id}-{_DIAGNOSE_ROLE}-{_DIAGNOSE_ASK}",
        "policy_id": policy.policy_id,
        "role": _DIAGNOSE_ROLE,
        "ask": _DIAGNOSE_ASK,
    }
    outcome = decision(
        "diagnose.prepare",
        config=config,
        inputs={"role": _DIAGNOSE_ROLE, "ask": _DIAGNOSE_ASK},
        policy_id=policy.policy_id,
    )
    return apply(
        WorkflowStep(
            step=envelope.step,
            workflow=workflow,
            context=gathered,
            decision=outcome,
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


def _recovery_options(
    context: Mapping[str, Any], include_continue: bool, used_minutes: float = 0.0
) -> list[RecoveryOption]:
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
        shipment = mapping(context, "shipment")
        eta = field(shipment, "eta_min_at_excursion")
        continue_drive_min = max(0.0, float(eta) - used_minutes) if eta is not None else 0.0
        options.append(
            RecoveryOption(id="continue", drive_min=continue_drive_min, kind="continue")
        )
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


def _engagement_intent(workflow: Mapping[str, Any]) -> str | None:
    """The driver's intent, when the engagement has already finished.

    The child stores its output at ``workflow.diagnosis.engagement_result``.
    An answered child puts ``intent`` on that object. Exhaustion has none.
    """

    diagnosis = mapping(workflow, "diagnosis") or {}
    result = mapping(diagnosis, "engagement_result") or {}
    found = text(result, "intent")
    if found:
        return found
    return text(mapping(result, "response"), "intent")


def _stopped_rise(assessment: Any) -> float | None:
    """The pinned faster rise for a stopped unit, when the config has one."""

    rate = getattr(assessment, "stopped_rise_rate_c_per_min", None)
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        return None
    return float(rate)


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
    intent = _engagement_intent(workflow)
    stopped = intent == "unit_stopped"
    rise_rate = _stopped_rise(assessment) if stopped else None
    try:
        projection = AssessmentContext(
            upper_c=float(upper),
            allowance_minutes=float(field(context, "allowance_minutes")),
            used_minutes=elapsed,
            temperature_c=float(temperature),
            unload_min=float(assessment.unload_min),
            rise_rate_c_per_min=float(rise_rate or assessment.rise_rate_c_per_min),
            cooldown_rate_c_per_min=float(assessment.cooldown_rate_c_per_min),
            min_spare_allowance_min=float(assessment.min_spare_allowance_min),
        )
        options = _recovery_options(
            context, assessment.include_continue_option, used_minutes=elapsed
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise MissingStepInput(f"the gathered context cannot be assessed: {exc}") from exc

    ranked = rank_options(projection, options, continue_feasible=not stopped)
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
            **({"intent": intent} if intent else {}),
            **({"rise_rate_c_per_min": float(rise_rate)} if rise_rate is not None else {}),
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

#: Every status the definition's ``ApprovalDecision`` understands.
_APPROVAL_STATUSES = frozenset({"approved", "auto_proceed", "rejected", "cancelled"})
_APPROVAL_APPROVE = frozenset({"approve", "approved", "accept", "accepted", "yes"})
_APPROVAL_REJECT = frozenset({"reject", "rejected", "deny", "denied", "no"})
_APPROVAL_CANCEL = frozenset({"cancel", "cancelled", "canceled"})

#: Partner-confirmation answers a facility reply may carry.
_FACILITY_ACCEPT = frozenset({"accept", "accepted", "confirm", "confirmed", "yes", "answer"})
_FACILITY_DECLINE = frozenset({"decline", "declined", "reject", "rejected", "no", "busy"})

#: The facility engagement SecureFacility contacts and the ask it makes (C13).
_FACILITY_ROLE = "facility"
_FACILITY_ASK = "accept"


def _approval_sources(
    workflow: Mapping[str, Any], envelope: StepEnvelope
) -> list[Mapping[str, Any]]:
    """Where the approver's decision may sit in the workflow or the envelope."""
    sources: list[Mapping[str, Any]] = []
    for key in ("approval_response",):
        nested = mapping(workflow, key)
        if nested:
            sources.append(nested)
    projection = getattr(envelope, "approval", None)
    if isinstance(projection, Mapping):
        sources.append(dict(projection))
    return sources


def _approval_verdict(
    workflow: Mapping[str, Any], envelope: StepEnvelope
) -> dict[str, Any] | None:
    """The approver's decision and any option they chose, or ``None`` if absent.

    The approval is a human reply, so it arrives on the envelope rather than
    being derived here. Several shapes are accepted so the app stack is not
    forced into one nesting: a boolean ``approved``, or a ``status``/``decision``
    string that maps onto a value ``ApprovalDecision`` reads.
    """
    for source in _approval_sources(workflow, envelope):
        option_id = text(source, "option_id") or text(source, "option")
        approved = field(source, "approved")
        if isinstance(approved, bool):
            return {"status": "approved" if approved else "rejected", "option_id": option_id}
        for key in ("status", "decision"):
            value = text(source, key)
            if value is None:
                continue
            folded = value.strip().lower()
            if folded in _APPROVAL_STATUSES:
                return {"status": folded, "option_id": option_id}
            if folded in _APPROVAL_APPROVE:
                return {"status": "approved", "option_id": option_id}
            if folded in _APPROVAL_REJECT:
                return {"status": "rejected", "option_id": option_id}
            if folded in _APPROVAL_CANCEL:
                return {"status": "cancelled", "option_id": option_id}
    return None


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


@handles("Approval")
def approval(envelope: StepEnvelope, dependencies: StepDependencies) -> StepEnvelope:
    """Record the approver's decision, or auto-proceed within the pinned limits.

    The definition routes to ``Approval`` only when ``Assess`` found a feasible
    recovery that needs a human. The ops manager's reply names the outcome, and
    ``ApprovalDecision`` reads ``$.approval.status``: an approval or an
    ``auto_proceed`` continues to ``SecureFacility``, a cancellation cancels, and
    a rejection or an absent status escalates. When the pinned configuration lets
    a within-limits recovery proceed without a human and ``Assess`` already routed
    to ``auto_proceed``, this step records ``auto_proceed`` so the run does not sit
    waiting for a reply it does not need.

    This step neither sends nor resumes anything: the approval send that stores
    the task token on ``ENG#`` and the inbound resume are the router's and C0's
    wiring. It records the engagement the send will use, so the definition and
    the router agree on the id and the ``approval_fast`` policy.
    """
    replay = _replay(envelope, dependencies)
    if replay is not None:
        return replay

    workflow = dict(envelope.workflow)
    incident = incident_for(envelope, dependencies)
    config = pinned_config(incident, dependencies)
    resolved = config.resolved
    approval_config = resolved.approval
    approver_role = approval_config.approver_role
    policy = select_policy(
        approver_role,
        "approve",
        dict(resolved.cadence),
        load_policies(),
    )
    assessment = mapping(workflow, "assessment") or {}
    within_limits = _within_pre_approved_limits(resolved)
    verdict = _approval_verdict(workflow, envelope)
    if verdict is None:
        if (
            approval_config.auto_proceed_if_within_limits
            and within_limits
            and text(assessment, "route") == "auto_proceed"
        ):
            verdict = {
                "status": "auto_proceed",
                "option_id": text(assessment, "recommended_option"),
            }
        else:
            raise MissingStepInput(
                "Approval needs the approver's decision, or an auto-proceed within limits"
            )
    request = {
        "engagement_id": f"ENG-{envelope.incident_id}-{approver_role}-approve",
        "policy_id": policy.policy_id,
        "role": approver_role,
        "ask": "approve",
        "escalation_chain": list(approval_config.escalation_chain),
    }
    record = {
        "status": verdict["status"],
        "option_id": verdict.get("option_id"),
        "approver_role": approver_role,
        "engagement_id": request["engagement_id"],
        "policy_id": policy.policy_id,
        "within_pre_approved_limits": within_limits,
        "decided_at": dependencies.stamp(),
        "config_version": config.version,
        "config_source": config.source,
    }
    outcome = decision(
        "approval.record",
        config=config,
        inputs={"approver_role": approver_role, "route": text(assessment, "route")},
        status=record["status"],
        option_id=record["option_id"],
        auto_proceed=record["status"] == "auto_proceed",
    )
    workflow["approval"] = record
    workflow["approval_request"] = request
    return apply(
        WorkflowStep(
            step=envelope.step,
            workflow=workflow,
            decision=outcome,
            result={"status": record["status"], "option_id": record["option_id"]},
            incident_state="AWAITING_APPROVAL",
            current_stage="AWAITING_APPROVAL",
        ),
        envelope,
        dependencies,
    )


def _facility_accepted(
    confirmation: Mapping[str, Any], workflow: Mapping[str, Any]
) -> bool | None:
    """The partner's confirmed answer, or ``None`` when they have not replied.

    A confirmed decline is ``False`` and a confirmed accept is ``True``. An accept
    recorded without a partner confirmation - ``Act`` running straight after a
    pre-approved recommendation - is ``True`` as well, so a facility that never
    had to reply is not reported as declined.

    A ``facility`` record this step wrote earlier carries ``decided_at``; it is the
    outcome, not a partner reply, so it is skipped. That keeps a decline for one
    facility from being re-read as the answer for the next facility the ranking
    offers.
    """
    sources: list[Mapping[str, Any]] = [confirmation]
    carried = mapping(workflow, "facility") or {}
    if carried and "decided_at" not in carried:
        sources.append(carried)
    for source in sources:
        for key in ("accepted", "confirmed", "answer"):
            value = field(source, key)
            if isinstance(value, bool):
                return value
        intent = text(source, "intent") or text(source, "status")
        if intent is not None:
            folded = intent.strip().lower()
            if folded in _FACILITY_ACCEPT:
                return True
            if folded in _FACILITY_DECLINE:
                return False
    return None


def _next_feasible_facility(
    assessment: Mapping[str, Any], chosen: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The next ranked feasible facility option after the one just requested."""
    ranked = field(assessment, "options")
    if not isinstance(ranked, Sequence) or isinstance(ranked, (str, bytes)):
        return None
    chosen_id = text(chosen, "id")
    candidates = [
        dict(item)
        for item in ranked
        if isinstance(item, Mapping)
        and field(item, "feasible") is True
        and (text(item, "kind") or "facility") == "facility"
        and text(item, "id") != chosen_id
    ]
    if not candidates:
        return None
    current_rank = field(chosen, "rank")
    if isinstance(current_rank, int) and not isinstance(current_rank, bool):
        better = [
            item
            for item in candidates
            if isinstance(field(item, "rank"), int)
            and not isinstance(field(item, "rank"), bool)
            and field(item, "rank") > current_rank
        ]
        if better:
            candidates = better
    return min(candidates, key=lambda item: field(item, "rank") or 0)


def _option_facility_id(option: Mapping[str, Any] | None) -> str | None:
    """The facility id a ranked option names, or ``None``."""
    if option is None:
        return None
    return text(option, "facility_id") or text(option, "id")


@handles("SecureFacility", "SecureFacilityFallback")
def secure_facility(envelope: StepEnvelope, dependencies: StepDependencies) -> StepEnvelope:
    """Confirm the facility the recovery chose, or decline to the next one.

    ``FacilityAccepted?`` reads ``$.facility.accepted``. The recommended (or the
    approver's chosen) facility is accepted when it is pre-approved and the
    partner confirmation says so; otherwise this step records a decline together
    with the next feasible candidate the ranking already produced, so the workflow
    can request that facility instead of escalating on the first refusal. The
    ``facility_a_declines`` scenario is exactly this path: A refuses and the run
    continues with B. Only when no further feasible facility remains is there
    nothing to try, and the definition's ``FacilityAccepted?`` default escalates.

    This step does not send the request: the facility engagement send is the
    router's work, and wiring this handler into the ASL is C0's. It records the
    partner the send will target and the next candidate to try on a decline.
    """
    replay = _replay(envelope, dependencies)
    if replay is not None:
        return replay

    workflow = dict(envelope.workflow)
    if envelope.step == "SecureFacilityFallback":
        declined = mapping(workflow, "facility") or {}
        next_option_id = text(declined, "next_option_id")
        if text(declined, "kind") != "facility" or next_option_id is None:
            raise MissingStepInput(
                "SecureFacilityFallback needs the next feasible facility from the decline"
            )
        assessment = mapping(workflow, "assessment") or {}
        assessment["recommended_option"] = next_option_id
        workflow["assessment"] = assessment
        # The previous response belongs to the declined partner, not this facility.
        workflow.pop("facility_response", None)

    context = _required_mapping(workflow, "context", envelope.step)
    assessment = _required_mapping(workflow, "assessment", envelope.step)
    incident = incident_for(envelope, dependencies)
    config = pinned_config(incident, dependencies)
    option = _chosen_option(workflow, assessment, envelope, envelope.step)
    if envelope.step == "SecureFacilityFallback" and text(option, "id") != next_option_id:
        raise StepError("SecureFacilityFallback did not select the next feasible facility")
    facility = _facility_for(context, option, envelope.step)
    if facility is None:
        # A continue option never needs a facility partner to accept.
        record = {
            "accepted": True,
            "facility_id": None,
            "kind": "continue",
            "decided_at": dependencies.stamp(),
            "config_version": config.version,
            "config_source": config.source,
        }
        workflow["facility"] = record
        return apply(
            WorkflowStep(
                step=envelope.step,
                workflow=workflow,
                decision=decision(
                    "facility.accept",
                    config=config,
                    inputs={"kind": "continue"},
                    accepted=True,
                ),
                result={"accepted": True, "facility_id": None},
                incident_state="SECURING_FACILITY",
                current_stage="SECURING_FACILITY",
            ),
            envelope,
            dependencies,
        )

    facility_id = text(facility, "facility_id") or text(facility, "id")
    confirmation = mapping(workflow, "facility_response") or {}
    accepted = _facility_accepted(confirmation, workflow)
    if accepted is None:
        # No partner answer yet: request this facility. ``_require_pre_approved_facility``
        # still refuses a facility the pinned limits do not cover.
        _require_pre_approved_facility(facility, config.resolved, envelope.step)
        accepted = True
    next_option = _next_feasible_facility(assessment, option)
    record = {
        "accepted": bool(accepted),
        "facility_id": facility_id,
        "kind": "facility",
        "contact_id": text(facility, "contact_id"),
        "next_option_id": text(next_option, "id") if next_option is not None else None,
        "next_facility_id": _option_facility_id(next_option) if next_option is not None else None,
        "decided_at": dependencies.stamp(),
        "config_version": config.version,
        "config_source": config.source,
    }
    outcome = decision(
        "facility.accept",
        config=config,
        inputs={"facility_id": facility_id, "declined": accepted is False},
        accepted=record["accepted"],
        facility_id=facility_id,
        next_option_id=record["next_option_id"],
    )
    workflow["facility"] = record
    # C13: record the engagement the facility contact/wait will use. The nested
    # ``EngageFacilityAccept`` state starts ``engage_party_v1`` with this request,
    # which stores the task token on ``ENG#`` and sends ``facility_request`` to the
    # chosen facility. ``FacilityDecision`` turns the answer into the gate below.
    if record["kind"] == "facility" and record["contact_id"]:
        workflow["facility_request"] = {
            "engagement_id": f"ENG-{envelope.incident_id}-{_FACILITY_ROLE}-{_FACILITY_ASK}",
            "policy_id": "partner_request",
            "role": _FACILITY_ROLE,
            "ask": _FACILITY_ASK,
            "facility_id": facility_id,
            "contact_id": record["contact_id"],
            "next_option_id": record["next_option_id"],
        }
    return apply(
        WorkflowStep(
            step=envelope.step,
            workflow=workflow,
            decision=outcome,
            result={"accepted": record["accepted"], "facility_id": facility_id},
            incident_state="SECURING_FACILITY",
            current_stage="SECURING_FACILITY",
        ),
        envelope,
        dependencies,
    )


def _child_result(workflow: Mapping[str, Any], key: str) -> dict[str, Any]:
    """The inner result of a nested ``startExecution.sync:2`` child, or ``{}``.

    ``.sync:2`` returns ``{"ExecutionArn", "Status", "Output": {...}}`` on AWS,
    while the offline simulator flattens the child output to the state root, so
    both shapes are read here and a missing child stays empty.
    """

    engagement = mapping(workflow, key) or {}
    inner = mapping(engagement, "engagement_result")
    if inner:
        return inner
    output = field(engagement, "Output")
    if isinstance(output, Mapping):
        return dict(output)
    # The offline simulator may place the child's terminal payload directly
    # under the facility_engagement key instead of retaining the .sync:2 wrapper.
    if "status" in engagement or "response" in engagement:
        return dict(engagement)
    return {}


@handles("FacilityDecision")
def facility_decision(envelope: StepEnvelope, dependencies: StepDependencies) -> StepEnvelope:
    """Turn the facility engagement's answer into the ``$.facility`` gate.

    ``EngageFacilityAccept`` runs ``engage_party_v1`` for the facility role, which
    contacts the chosen facility and waits for Accept or Decline (the
    ``partner_request`` ladder, bounded by its own ``exhaust_after``). This step
    maps the child's terminal output onto ``FacilityAccepted?``:

    * ``answered`` with ``accept`` -> ``accepted`` true, so the run advances to ``Act``.
    * ``answered`` with ``decline`` -> ``accepted`` false and the next feasible
      option is preserved, so ``SecureFacilityFallback`` requests ``FAC-B`` (#66).
    * ``exhausted`` (no answer within the ladder), a timeout, or an unusable
      reply -> ``accepted`` false with no next option, so the definition escalates.

    The request the facility was contacted on is ``workflow.facility_request``; the
    facility gate ``SecureFacility`` wrote carries ``facility_id`` and
    ``next_option_id``.
    """

    replay = _replay(envelope, dependencies)
    if replay is not None:
        return replay

    workflow = dict(envelope.workflow)
    request = mapping(workflow, "facility_request") or {}
    if not request:
        raise MissingStepInput("FacilityDecision needs the facility engagement request")
    carried = dict(mapping(workflow, "facility") or {})
    facility_id = text(carried, "facility_id") or text(request, "facility_id")
    next_option_id = text(carried, "next_option_id") or text(request, "next_option_id")
    result = _child_result(workflow, "facility_engagement")
    status = text(result, "status")
    response = mapping(result, "response") or {}
    intent = text(result, "intent") or text(response, "intent")

    if status == "answered" and intent == "accept":
        accepted = True
        outcome_reason = "answered"
    elif status == "answered" and intent == "decline":
        accepted = False
        outcome_reason = "declined"
    else:
        # No answer within the ladder, a timeout, or an unusable reply escalates.
        accepted = False
        next_option_id = None
        outcome_reason = status or "no_answer"

    facility_response = {"status": status or "no_answer", "reason": outcome_reason}
    if intent is not None:
        facility_response["intent"] = intent
    record = {
        **carried,
        "accepted": accepted,
        "facility_id": facility_id,
        "kind": text(carried, "kind") or "facility",
        "next_option_id": next_option_id,
        "response": facility_response,
        "decided_at": dependencies.stamp(),
    }
    workflow["facility"] = record
    workflow["facility_response"] = facility_response
    config = pinned_config(incident_for(envelope, dependencies), dependencies)
    outcome = decision(
        "facility.accept",
        config=config,
        inputs={
            "facility_id": facility_id,
            "status": facility_response["status"],
            "accepted": accepted,
        },
        accepted=accepted,
        facility_id=facility_id,
        next_option_id=next_option_id,
    )
    return apply(
        WorkflowStep(
            step=envelope.step,
            workflow=workflow,
            decision=outcome,
            result={"accepted": accepted, "facility_id": facility_id},
            incident_state="SECURING_FACILITY",
            current_stage="SECURING_FACILITY",
        ),
        envelope,
        dependencies,
    )


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


def _latest_reading(
    workflow: Mapping[str, Any],
    context: Mapping[str, Any],
    dependencies: StepDependencies,
    envelope: StepEnvelope,
    limits: tuple[float, float],
) -> tuple[float, datetime, datetime]:
    """Return latest persisted temperature, its time, and current stable-window start."""
    readings = dependencies.incidents.readings(envelope.tenant_id, envelope.incident_id)
    if not readings:
        legacy = _reading(workflow, context, envelope.step)
        anchor = _containment_anchor(workflow, context)
        return legacy, dependencies.now(), anchor

    ordered = sorted(readings, key=lambda row: (text(row, "ts") or "", text(row, "sk") or ""))
    latest = ordered[-1]
    latest_temperature = field(latest, "temperature_c")
    if isinstance(latest_temperature, bool) or not isinstance(latest_temperature, (int, float)):
        raise MissingStepInput("the latest persisted reading has no numeric temperature_c")
    latest_at = _moment(text(latest, "ts"), "latest_reading.ts")
    lower, upper = limits
    stable_start = latest_at
    if lower <= float(latest_temperature) <= upper:
        for row in reversed(ordered[:-1]):
            temperature = field(row, "temperature_c")
            if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
                raise MissingStepInput("a persisted reading has no numeric temperature_c")
            if not lower <= float(temperature) <= upper:
                break
            stable_start = _moment(text(row, "ts"), "reading.ts")
    return float(latest_temperature), latest_at, stable_start


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

    The latest persisted sensor reading and the continuous interval of persisted
    in-range readings determine whether the configured stable window has elapsed.
    The excursion allowance can reopen an unsolved incident, and the configured
    attempt cap routes a run that cannot be resolved to escalation.

    Every verdict is returned to the state machine but not stored here: waits
    and retries re-read the current sensor facts rather than replaying stale data.
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
    now = dependencies.now()
    reading, reading_at, stable_start = _latest_reading(
        workflow,
        context,
        dependencies,
        envelope,
        (lower, upper),
    )
    previous = _gate(envelope, workflow, "containment") or {}
    stable_minutes = _minutes(stable_start, min(now, reading_at))
    attempt = int(field(previous, "attempt") or 0) + 1
    stable_required = _minutes_duration(
        verification.stable_in_range, "verification.stable_in_range"
    )
    in_range = lower <= reading <= upper
    allowance = calculate_allowance(
        float(field(context, "allowance_minutes")),
        _minutes_between(_ship_time(workflow, context), now),
    )
    attempt_cap_reached = attempt >= verification.max_attempts
    status = (
        "escalate"
        if attempt_cap_reached
        else _containment_status(
            in_range=in_range,
            reading=reading,
            upper=upper,
            stable_minutes=stable_minutes,
            stable_required=stable_required,
            allowance_status=allowance.status,
            was_contained=text(previous, "status") == CONTAINED,
            reopen_on_rise=verification.reopen_on_rise,
        )
    )
    containment = {
        "status": status,
        "temperature_c": reading,
        "range_c": [lower, upper],
        "in_range": in_range,
        "stable_minutes": stable_minutes,
        "stable_required_min": stable_required,
        "reading_at": reading_at.isoformat().replace("+00:00", "Z"),
        "attempt": attempt,
        "attempt_cap_reached": attempt_cap_reached,
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
        attempt=attempt,
        attempt_cap_reached=attempt_cap_reached,
        stable_required_min=stable_required,
        reopened=status == REOPENED,
    )
    workflow["containment"] = containment
    return apply(
        WorkflowStep(
            step=envelope.step,
            workflow=workflow,
            decision=outcome,
            result={"status": status, "temperature_c": reading, "attempt": attempt},
            incident_state="VERIFYING",
            current_stage="VERIFYING",
        ),
        envelope,
        dependencies,
        remember=False,
    )


@handles("RecordContainment")
def record_containment(envelope: StepEnvelope, dependencies: StepDependencies) -> StepEnvelope:
    """Commit a verified containment as the VERIFYING → CONTAINED transition."""
    replay = _replay(envelope, dependencies)
    if replay is not None:
        return replay

    workflow = dict(envelope.workflow)
    containment = _gate(envelope, workflow, "containment") or {}
    if text(containment, "status") != CONTAINED:
        raise MissingStepInput("RecordContainment needs a contained verification verdict")
    workflow["containment"] = {
        **containment,
        "recorded_at": dependencies.stamp(),
    }
    return apply(
        WorkflowStep(
            step=envelope.step,
            workflow=workflow,
            result={"status": CONTAINED},
            incident_state="CONTAINED",
            current_stage="CONTAINED",
        ),
        envelope,
        dependencies,
    )


@handles("BeginDisposition")
def begin_disposition(envelope: StepEnvelope, dependencies: StepDependencies) -> StepEnvelope:
    """Move a contained incident into the disposition review stage."""
    replay = _replay(envelope, dependencies)
    if replay is not None:
        return replay

    workflow = dict(envelope.workflow)
    containment = _gate(envelope, workflow, "containment") or {}
    if text(containment, "status") != CONTAINED:
        raise MissingStepInput("BeginDisposition needs a contained verification verdict")
    workflow["disposition_started_at"] = dependencies.stamp()
    return apply(
        WorkflowStep(
            step=envelope.step,
            workflow=workflow,
            result={"status": "awaiting_disposition"},
            incident_state="AWAITING_DISPOSITION",
            current_stage="AWAITING_DISPOSITION",
        ),
        envelope,
        dependencies,
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
    if envelope.qa_review is not None:
        sources.append(envelope.qa_review)
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


@handles("GenerateReport")
def generate_report(envelope: StepEnvelope, dependencies: StepDependencies) -> StepEnvelope:
    """Publish the incident report, then record its receipt on the workflow.

    The document is assembled by ``oerf.platform.report.generator`` from the
    stored incident row, its ``EVT#`` timeline, and its ``READ#`` readings, so
    the report reflects what happened, not what the run intended. The incident
    does not move: ``GenerateReport`` sits inside the disposition stage, and the
    definition's ``CancelAfterGenerateReport?`` gate is the only edge out.
    """
    replay = _replay(envelope, dependencies)
    if replay is not None:
        return replay

    if dependencies.reports is None:
        raise StepError(f"step {envelope.step} has no report publisher configured")
    published = dependencies.reports.publish(
        tenant_id=envelope.tenant_id, incident_id=envelope.incident_id
    )
    workflow = dict(envelope.workflow)
    workflow["report"] = {
        key: published[key]
        for key in (
            "report_key",
            "s3_uri",
            "report_version",
            "sha256",
            "locked",
            "generated_at",
            "sections",
        )
        if key in published
    }
    return apply(
        WorkflowStep(
            step=envelope.step,
            workflow=workflow,
            result={"status": "generated", "report_key": published.get("report_key")},
            current_stage="AWAITING_DISPOSITION",
        ),
        envelope,
        dependencies,
    )


@handles("HandleReopen")
def handle_reopen(envelope: StepEnvelope, dependencies: StepDependencies) -> StepEnvelope:
    """Move a rejected QA review back to REOPENED before verification resumes."""
    replay = _replay(envelope, dependencies)
    if replay is not None:
        return replay

    workflow = dict(envelope.workflow)
    containment = _gate(envelope, workflow, "containment") or {}
    if text(containment, "status") not in {"reopened", "contained"}:
        raise MissingStepInput("HandleReopen needs a reopened containment verdict")
    workflow["containment"] = {
        **containment,
        "status": "reopened",
        "reopened_at": dependencies.stamp(),
    }
    return apply(
        WorkflowStep(
            step=envelope.step,
            workflow=workflow,
            result={"status": "reopened"},
            incident_state="REOPENED",
            current_stage="REOPENED",
        ),
        envelope,
        dependencies,
    )


@handles("NoResponse")
def no_response(envelope: StepEnvelope, dependencies: StepDependencies) -> StepEnvelope:
    """Record that the approval wait expired, without changing incident state."""
    replay = _replay(envelope, dependencies)
    if replay is not None:
        return replay

    workflow = dict(envelope.workflow)
    approval = _gate(envelope, workflow, "approval") or {}
    workflow["approval"] = {
        **approval,
        "status": "no_response",
        "timed_out": True,
        "timed_out_at": dependencies.stamp(),
    }
    output = apply(
        WorkflowStep(
            step=envelope.step,
            workflow=workflow,
            result={"status": "no_response"},
            current_stage="AWAITING_APPROVAL",
        ),
        envelope,
        dependencies,
    )
    return output


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
