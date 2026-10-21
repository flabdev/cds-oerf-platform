"""What a step handler needs that is not in the envelope.

The envelope carries tenant, incident, and workflow. Everything else - the
incident store, the step-result store, the config repository, the clock - is
injected here, so a handler never reads an environment variable or builds an
AWS client. The incident record is the source of truth for the configuration a
running incident was opened with, so the pinned snapshot travels with the
incident instead of being re-resolved from the current active config.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, ValidationError

from oerf.platform.incidents.repository import (
    TERMINAL_STATES,
    TRANSITIONS,
    IllegalTransition,
    Incident,
    IncidentStore,
    StaleIncidentUpdate,
    ensure_transition,
)
from oerf.platform.tenancy.config_resolver import ConfigError, ResolvedConfig, get_config
from oerf.platform.tenancy.context import TenantContext

DOMAIN = "cold_chain_excursion"

#: ``current_stage`` per step. A stage that is not an incident state (the QA
#: gate) is reported without a transition.
STAGE_BY_STEP: dict[str, str] = {
    "OPEN": "OPEN",
    "GatherContext": "DIAGNOSING",
    "Diagnose": "DIAGNOSING",
    "Assess": "ASSESSING",
    "Approval": "AWAITING_APPROVAL",
    "SecureFacility": "SECURING_FACILITY",
    "Act": "IN_TRANSIT",
    "Verify": "VERIFYING",
    "HandleReopen": "REOPENED",
    "GenerateReport": "AWAITING_DISPOSITION",
    "QA": "AWAITING_DISPOSITION",
    "Close": "CLOSED",
    "RecordFailure": "ESCALATED",
    "Escalate": "ESCALATED",
}


@dataclass(frozen=True)
class StepPosition:
    """Where a step may start, and what it owns once it ran.

    ``accepts`` holds every state the step may run from: the state a
    predecessor leaves behind, plus any state the step may legitimately be
    resumed from. It is a set rather than one state because the two ends of an
    edge are not always the same step's business - a recovery is assessed
    before it is approved, and an approval is outstanding before the facility
    is engaged - so a step only claims the states it can actually serve.

    ``target`` is the state the step leaves the incident in. ``None`` marks a
    gate: it reports a stage and a decision without moving the incident. A step
    whose work passes through an intermediate state - ``Act`` secures the
    facility before the shipment moves - names only the end state; the legal
    hops in between are walked and recorded one at a time.
    """

    accepts: frozenset[str]
    target: str | None = None


#: Any state at all, for the steps that answer an escalation from wherever it
#: happens.
_ANY_STATE = frozenset(TRANSITIONS)

#: Incident state each step owns.
STEP_POSITIONS: dict[str, StepPosition] = {
    "OPEN": StepPosition(frozenset({"OPEN"}), "OPEN"),
    "GatherContext": StepPosition(frozenset({"OPEN"}), "DIAGNOSING"),
    "Diagnose": StepPosition(frozenset({"DIAGNOSING"})),
    "Assess": StepPosition(frozenset({"DIAGNOSING"}), "ASSESSING"),
    "Approval": StepPosition(frozenset({"ASSESSING", "AWAITING_APPROVAL"}), "AWAITING_APPROVAL"),
    "SecureFacility": StepPosition(
        frozenset({"ASSESSING", "AWAITING_APPROVAL"}), "SECURING_FACILITY"
    ),
    "Act": StepPosition(
        frozenset({"ASSESSING", "AWAITING_APPROVAL", "SECURING_FACILITY"}), "IN_TRANSIT"
    ),
    "Verify": StepPosition(frozenset({"IN_TRANSIT", "VERIFYING", "REOPENED"}), "CONTAINED"),
    "HandleReopen": StepPosition(frozenset({"VERIFYING", "CONTAINED", "REOPENED"}), "REOPENED"),
    "GenerateReport": StepPosition(
        frozenset({"CONTAINED", "AWAITING_DISPOSITION"}), "AWAITING_DISPOSITION"
    ),
    "QA": StepPosition(frozenset({"CONTAINED", "AWAITING_DISPOSITION"})),
    "Close": StepPosition(frozenset({"AWAITING_DISPOSITION"}), "CLOSED"),
    "RecordFailure": StepPosition(_ANY_STATE),
    "Escalate": StepPosition(_ANY_STATE),
}

#: Steps that report a stage without moving the incident.
STAGE_ONLY_STEPS = frozenset(
    step for step, position in STEP_POSITIONS.items() if position.target is None
)

#: States that mean a step already ran, so re-running it is a replay.
_DOWNSTREAM: dict[str, frozenset[str]] = {
    "GatherContext": frozenset({"DIAGNOSING", "ASSESSING"}),
    "Diagnose": frozenset({"DIAGNOSING", "ASSESSING"}),
    "Assess": frozenset({"ASSESSING", "AWAITING_APPROVAL", "SECURING_FACILITY", "ESCALATED"}),
    "SecureFacility": frozenset({"SECURING_FACILITY", "IN_TRANSIT"}),
    "Act": frozenset(
        {"IN_TRANSIT", "VERIFYING", "CONTAINED", "REOPENED", "AWAITING_DISPOSITION", "CLOSED"}
    ),
    "Verify": frozenset({"CONTAINED", "REOPENED", "AWAITING_DISPOSITION", "CLOSED"}),
    "QA": frozenset({"AWAITING_DISPOSITION", "CLOSED"}),
    "Close": frozenset({"CLOSED"}),
}


class StepError(Exception):
    """Base error for a workflow step."""


class MissingStepInput(StepError):
    """The envelope or workflow is missing something this step cannot guess."""


class StaleStepState(StepError):
    """The incident is no longer in a state this step may move."""


class ConfigPinError(StepError):
    """Neither a pinned snapshot nor the live config could be resolved."""


@runtime_checkable
class StepResultPort(Protocol):
    """The slice of ``StepResultStore`` a handler uses."""

    def get(self, *, tenant_id: str, incident_id: str, step: str) -> Any:
        """Return the stored record, or ``None`` when the step never finished."""

    def put(
        self,
        *,
        tenant_id: str,
        incident_id: str,
        step: str,
        decision: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        step_output: dict[str, Any] | None = None,
    ) -> Any:
        """Record a first outcome and return the record that is now current."""


@runtime_checkable
class ConfigReader(Protocol):
    """The config source ``get_config`` resolves through, or ``None`` for files."""

    def list_tenant_ids(self) -> set[str]:
        """Every tenant that has configuration."""

    def subscribed_domains(self, tenant_id: str) -> set[str]:
        """The domains a tenant is subscribed to."""

    def read_defaults(self, domain: str) -> dict[str, Any]:
        """The published domain defaults."""

    def read_schema(self, domain: str) -> dict[str, Any]:
        """The domain schema."""

    def read_tenant_config(self, tenant_id: str, domain: str) -> dict[str, Any]:
        """The tenant's active overrides."""


class PinnedConfig(BaseModel):
    """The configuration an incident was opened with, typed for the rules."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    resolved: ResolvedConfig
    version: int | None = None
    source: str = "pinned_snapshot"

    def provenance(self) -> dict[str, Any]:
        """Which configuration decided this step, for the audit trail."""
        return {
            "config_version": self.version,
            "config_source": self.source,
        }


@dataclass(frozen=True)
class StepDependencies:
    """Everything a handler is given besides the envelope.

    ``incidents`` and ``results`` are required. ``config`` is only needed when
    an incident has no pinned snapshot, which happens for local runs, and for
    incidents opened before a snapshot was written.
    """

    incidents: IncidentStore
    results: StepResultPort
    config: ConfigReader | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def now(self) -> datetime:
        return self.clock()

    def stamp(self) -> str:
        """The current time as the ISO-Z string the platform stores."""
        return self.now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def incident_for(envelope: Any, dependencies: StepDependencies) -> Incident:
    """Read the incident this envelope belongs to, tenant-scoped."""
    context = TenantContext(tenant_id=envelope.tenant_id)
    incident = dependencies.incidents.get(context.tenant_id, envelope.incident_id)
    if incident.shipment_id != envelope.shipment_id:
        raise StaleStepState(
            f"incident {envelope.incident_id} belongs to shipment "
            f"{incident.shipment_id}, not {envelope.shipment_id}"
        )
    return incident


def pinned_config(incident: Incident, dependencies: StepDependencies) -> PinnedConfig:
    """Return the snapshot the incident opened with, falling back to live config.

    A snapshot written at open time wins even after an admin publishes a new
    version, so a running incident keeps deciding on the configuration a
    reviewer saw.
    """
    snapshot = incident.config_snapshot
    if snapshot:
        resolved = _resolved(snapshot)
        version = incident.config_version
        if version is None and isinstance(snapshot, Mapping):
            raw = snapshot.get("version")
            version = int(raw) if isinstance(raw, (int, float)) else None
        return PinnedConfig(resolved=resolved, version=version)
    try:
        resolved = get_config(incident.tenant_id, DOMAIN, repository=dependencies.config)
    except ConfigError as exc:
        raise ConfigPinError(
            f"incident {incident.incident_id} has no config snapshot and the live "
            f"configuration could not be resolved: {exc}"
        ) from exc
    return PinnedConfig(resolved=resolved, version=resolved.version, source="live_config")


def _resolved(snapshot: Mapping[str, Any]) -> ResolvedConfig:
    try:
        return ResolvedConfig.model_validate(dict(snapshot))
    except ValidationError as exc:
        raise ConfigPinError(
            f"the pinned config snapshot is not a complete domain configuration: {exc}"
        ) from exc


def stage_for(step: str) -> str:
    """The ``current_stage`` a step reports."""
    return STAGE_BY_STEP.get(step, step.upper())


def advance(
    envelope: Any,
    dependencies: StepDependencies,
    *,
    to_state: str | None = None,
    hold: bool = False,
) -> dict[str, Any]:
    """Move the incident to the state this step owns, unless it is already there.

    Returns a small record of what happened: the state before, the state after,
    and whether this call moved it. A step that already ran - the retry case -
    reports ``transitioned=False, replayed=True`` and leaves the incident
    alone. Where the incident already moved past this step, the stored decision
    is what the workflow continues with, so the later state is preserved.

    A step may own more than one edge: ``Act`` secures the facility before the
    shipment moves, and ``Verify`` re-checks a containment that was reopened.
    Those edges are walked one legal hop at a time, so every hop still lands on
    the incident timeline.

    ``hold`` is for a step that reports a verdict without moving the incident,
    such as a verification that is still waiting for the reading to settle. It
    keeps the incident exactly where it is, including when the step's position
    names a target state.
    """
    step = envelope.step
    incident = incident_for(envelope, dependencies)
    position = STEP_POSITIONS.get(step, StepPosition(frozenset({incident.state})))
    target_state = None if hold else (to_state or position.target)

    if target_state is None or target_state == incident.state:
        # A gate, or the step already moved the incident: report, change nothing.
        return _advance_record(
            incident.state, incident.state, replayed=incident.state not in position.accepts
        )

    if incident.state not in position.accepts:
        if incident.state in _downstream(step):
            return _advance_record(incident.state, incident.state, replayed=True)
        raise StaleStepState(
            f"incident {incident.incident_id} is {incident.state}, so step {step} "
            f"cannot start from {' or '.join(sorted(position.accepts))}"
        )

    return _walk(envelope, dependencies, from_state=incident.state, to_state=target_state)


def _walk(
    envelope: Any,
    dependencies: StepDependencies,
    *,
    from_state: str,
    to_state: str,
) -> dict[str, Any]:
    """Apply every hop this step owns, then report where the incident ended up."""
    current = from_state
    try:
        for hop in _route(from_state, to_state):
            ensure_transition(current, hop)
            current = dependencies.incidents.transition(
                envelope.tenant_id,
                envelope.incident_id,
                from_state=current,
                to_state=hop,
            ).state
    except IllegalTransition as exc:
        raise StepError(str(exc)) from exc
    except StaleIncidentUpdate:
        # A concurrent writer moved the incident first. The stored decision
        # still governs, so report a replay instead of failing the step.
        latest = incident_for(envelope, dependencies)
        if latest.state in TERMINAL_STATES or latest.state in _downstream(envelope.step):
            return _advance_record(latest.state, latest.state, replayed=True)
        raise
    return _advance_record(from_state, current)


def _route(from_state: str, to_state: str) -> list[str]:
    """The shortest chain of allowed edges from one state to the next.

    ``Act`` secures the facility before the shipment moves, so a step can span
    an intermediate state. Every hop is still checked against the repository's
    transitions before it is applied.
    """
    frontier: deque[tuple[str, list[str]]] = deque([(from_state, [])])
    visited = {from_state}
    while frontier:
        state, path = frontier.popleft()
        for hop in sorted(TRANSITIONS.get(state, frozenset())):
            if hop in visited:
                continue
            if hop == to_state:
                return [*path, hop]
            visited.add(hop)
            frontier.append((hop, [*path, hop]))
    raise StepError(f"the incident cannot move from {from_state} to {to_state}")


def _advance_record(previous: str, state: str, *, replayed: bool = False) -> dict[str, Any]:
    return {
        "previous_state": previous,
        "incident_state": state,
        "transitioned": previous != state,
        "replayed": replayed,
    }


def _downstream(step: str) -> frozenset[str]:
    return _DOWNSTREAM.get(step, TERMINAL_STATES)
