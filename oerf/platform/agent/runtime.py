"""Composition root for the six cold-chain step entry points.

``docs/interfaces.md`` section 8 (OERF-304) freezes one module and one
``handler`` function per phase of the cold-chain definition. This module pairs
each frozen module with the state-machine step it owns and builds the ports it
needs:

===========================  =====================
Phase / module suffix        State-machine step
===========================  =====================
``context``                  ``GatherContext``
``assess``                   ``Assess``
``act``                      ``Act``
``verify``                   ``Verify``
``qa``                       ``QA``
``close``                    ``Close``
===========================  =====================

A module serves one phase and nothing else. It parses the accumulator envelope,
builds only the ports that phase may use, calls the registered handler, and
returns the replacement envelope as the task response. A state pointed at the
wrong module raises :class:`StepNotRouted`, so a task never runs another
phase's side effects under this phase's IAM role.

No AWS client exists at import time. :func:`dynamodb_client` builds one on first
use, and :func:`run_step` accepts ready-made ports, so tests, local runs, and
the agent tool layer (OERF-206) can call an entry point without AWS.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import boto3

from oerf.platform.agent.context import ConfigReader, StepDependencies
from oerf.platform.agent.dynamo_step_results import DynamoStepResultStore
from oerf.platform.agent.envelope import parse_envelope
from oerf.platform.agent.handlers import HANDLERS
from oerf.platform.incidents.dynamo import DynamoIncidentStore
from oerf.platform.observability.logging import get_logger, log
from oerf.platform.tenancy.dynamo_config import DynamoConfigRepository
from oerf.platform.tenancy.facilities import DynamoFacilityCatalog

logger = get_logger(__name__)
logger.setLevel(logging.INFO)

__all__ = [
    "MODULES",
    "PHASES",
    "ConfigurationError",
    "RuntimeSettings",
    "StepNotRouted",
    "UnknownPhase",
    "WiringError",
    "build_dependencies",
    "dynamodb_client",
    "module_for",
    "phase_for",
    "run_step",
]

#: Phase name to the state-machine ``step`` value that phase serves.
#: The phase name is also the frozen module suffix under
#: ``domains.cold_chain_excursion.steps``.
PHASES: dict[str, str] = {
    "context": "GatherContext",
    "diagnose": "Diagnose",
    "assess": "Assess",
    "act": "Act",
    "verify": "Verify",
    "qa": "QA",
    "close": "Close",
    "record_failure": "RecordFailure",
    "escalate": "Escalate",
    "cancel": "Cancel",
}

#: Phase name to the module path ``docs/interfaces.md`` freezes for it.
MODULES: dict[str, str] = {phase: f"domains.cold_chain_excursion.steps.{phase}" for phase in PHASES}


class WiringError(Exception):
    """A state-machine state reached an entry point that cannot serve it."""


class UnknownPhase(WiringError):
    """The caller asked for a phase this release does not have."""


class StepNotRouted(WiringError):
    """The event's step belongs to a different frozen entry point."""


class ConfigurationError(Exception):
    """The Lambda environment does not describe the tables a step needs."""


@dataclass(frozen=True)
class RuntimeSettings:
    """Table names one invocation may touch, read from the Lambda environment."""

    incident_table: str
    tenant_table: str = ""

    def __post_init__(self) -> None:
        if not self.incident_table.strip():
            raise ConfigurationError("incident_table must name the incident table")

    @classmethod
    def from_environment(cls) -> RuntimeSettings:
        """Read the deployment's table names, failing before any adapter is built."""
        incident = os.environ.get("INCIDENT_TABLE_NAME", "").strip()
        if not incident:
            raise ConfigurationError(
                "INCIDENT_TABLE_NAME is not set; the step container cannot reach the incident table"
            )
        return cls(
            incident_table=incident,
            tenant_table=os.environ.get("TENANT_TABLE_NAME", "").strip(),
        )


_client: Any | None = None


def dynamodb_client() -> Any:
    """The low-level DynamoDB client the adapters take, built on first use."""
    global _client
    if _client is None:
        _client = boto3.client("dynamodb")
    return _client


def build_dependencies(
    *,
    client: Any | None = None,
    settings: RuntimeSettings | None = None,
    clock: Callable[[], datetime] | None = None,
) -> StepDependencies:
    """The container: adapters over one client, one table pair, and one clock.

    Every port takes the same client and table because incidents, step results,
    and the shipment lock share the incident table. The config port is only
    built when a tenant table is configured: a step on an incident with a
    pinned snapshot never reads it, and a local run falls back to the packaged
    domain files.
    """
    resolved = settings or RuntimeSettings.from_environment()
    dynamo = client if client is not None else dynamodb_client()
    moment = clock or (lambda: datetime.now(UTC))
    config: ConfigReader | None = None
    facilities = None
    if resolved.tenant_table:
        config = DynamoConfigRepository(dynamo, resolved.tenant_table)
        facilities = DynamoFacilityCatalog(dynamo, resolved.tenant_table)
    return StepDependencies(
        incidents=DynamoIncidentStore(
            dynamo,
            resolved.incident_table,
            tenant_table=resolved.tenant_table or None,
            clock=moment,
        ),
        results=DynamoStepResultStore(dynamo, resolved.incident_table, clock=moment),
        config=config,
        facilities=facilities,
        clock=moment,
    )


def phase_for(step: str) -> str | None:
    """The phase that owns a state-machine step, or ``None`` if none does yet."""
    for phase, owned in PHASES.items():
        if owned == step:
            return phase
    return None


def module_for(step: str) -> str | None:
    """The frozen module path that owns a step, for wiring and error messages."""
    phase = phase_for(step)
    return MODULES[phase] if phase is not None else None


def run_step(
    phase: str,
    event: Mapping[str, Any],
    context: Any = None,
    *,
    dependencies: StepDependencies | None = None,
    settings: RuntimeSettings | None = None,
    client: Any | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Parse, route, run, and return one step's replacement envelope.

    ``dependencies`` is the injection point. Omit it in Lambda and the container
    builds the DynamoDB ports; pass it in tests to keep the call off AWS.
    """
    step = PHASES.get(phase)
    if step is None:
        raise UnknownPhase(f"unknown step phase {phase!r}; phases are {', '.join(sorted(PHASES))}")

    envelope = parse_envelope(event)
    if envelope.step != step:
        owner = module_for(envelope.step) or "the module that serves it in a later release"
        raise StepNotRouted(
            f"step {envelope.step!r} reached {MODULES[phase]}.handler, which owns "
            f"{step!r}; wire the state to {owner}"
        )

    ports = (
        dependencies
        if dependencies is not None
        else build_dependencies(settings=settings, client=client, clock=clock)
    )
    request_id = getattr(context, "aws_request_id", "-")
    log(
        logger,
        logging.INFO,
        (
            f"step {step} for incident {envelope.incident_id} "
            f"(tenant {envelope.tenant_id}, request {request_id})"
        ),
        tenant_id=envelope.tenant_id,
        incident_id=envelope.incident_id,
        outcome="started",
    )
    try:
        result = HANDLERS[step](envelope, ports)
    except Exception:
        # Step Functions routes the failure to RecordFailure; the log line names
        # the step so a retry is matched to the invocation that produced it.
        log(
            logger,
            logging.ERROR,
            f"step {step} failed for incident {envelope.incident_id}",
            tenant_id=envelope.tenant_id,
            incident_id=envelope.incident_id,
            outcome="error",
            count=1,
        )
        raise
    return result.to_event()
