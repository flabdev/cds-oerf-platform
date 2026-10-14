"""Incident records and allowed state transitions.

Items use the incident-table keys. Creating an incident puts the shipment
lock and the incident record together. A later create for that shipment
appends to the open incident. A transition is applied only when the stored
state still matches ``from_state``. Closing or cancelling releases the lock.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from itertools import count

from pydantic import BaseModel, ConfigDict

from oerf.platform.incidents.lock import OpenIncidentLock
from oerf.platform.tenancy.context import TenantContext

TERMINAL_STATES = frozenset({"CLOSED", "CANCELLED"})
TRANSITIONS: dict[str, frozenset[str]] = {
    "OPEN": frozenset({"DIAGNOSING", "CANCELLED"}),
    "DIAGNOSING": frozenset({"ASSESSING", "CANCELLED"}),
    "ASSESSING": frozenset({"AWAITING_APPROVAL", "SECURING_FACILITY", "ESCALATED", "CANCELLED"}),
    "AWAITING_APPROVAL": frozenset({"SECURING_FACILITY", "ESCALATED", "CANCELLED"}),
    "SECURING_FACILITY": frozenset({"IN_TRANSIT", "ESCALATED", "CANCELLED"}),
    "ESCALATED": frozenset({"SECURING_FACILITY", "VERIFYING", "CANCELLED"}),
    "IN_TRANSIT": frozenset({"VERIFYING", "ESCALATED", "CANCELLED"}),
    "VERIFYING": frozenset({"CONTAINED", "REOPENED", "CANCELLED"}),
    "CONTAINED": frozenset({"REOPENED", "AWAITING_DISPOSITION", "CANCELLED"}),
    "REOPENED": frozenset({"VERIFYING", "CANCELLED"}),
    "AWAITING_DISPOSITION": frozenset(
        {"AWAITING_DISPOSITION", "REOPENED", "CLOSED", "CANCELLED"}
    ),
    "CLOSED": frozenset(),
    "CANCELLED": frozenset(),
}


class IncidentError(Exception):
    """Base error for incident persistence."""


class IncidentNotFound(IncidentError):
    """No incident exists for the requested key."""


class IllegalTransition(IncidentError):
    """The state machine does not allow this edge."""


class StaleIncidentUpdate(IncidentError):
    """The incident is no longer in ``from_state``."""


class Incident(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str
    tenant_id: str
    shipment_id: str
    state: str
    reason: str
    opened_at: str
    pk: str
    sk: str = "META"


class TimelineEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sk: str
    previous_state: str
    state: str
    reason: str | None = None


class TransitionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str
    previous_state: str
    state: str


class IncidentRepository:
    """In-memory incident store with conditional state changes."""

    def __init__(
        self,
        lock: OpenIncidentLock | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        next_id: Callable[[], str] | None = None,
    ) -> None:
        self.lock = lock or OpenIncidentLock()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._ids = count(1)
        self._next_id = next_id or (lambda: f"INC-{next(self._ids):06d}")
        self._incidents: dict[tuple[str, str], Incident] = {}
        self._events: dict[tuple[str, str], list[TimelineEvent]] = {}
        self._event_seq: dict[tuple[str, str], int] = {}
        self._guard = threading.Lock()

    def create(self, *, tenant_id: str, shipment_id: str, reason: str) -> Incident:
        """Open an incident, or append to the one already locked for this shipment.

        The lock acquire and the incident write share one critical section, so
        concurrent callers cannot both create an incident.
        """

        context = TenantContext(tenant_id=tenant_id)
        shipment = _required(shipment_id, "shipment_id")
        cause = _required(reason, "reason")
        with self._guard:
            incident_id = self._next_id()
            acquired = self.lock.acquire_lock(context.tenant_id, shipment, incident_id)
            if not acquired.created:
                return self._append_open(context.tenant_id, acquired.incident_id, cause)
            pk = f"TENANT#{context.tenant_id}#INC#{incident_id}"
            incident = Incident(
                incident_id=incident_id,
                tenant_id=context.tenant_id,
                shipment_id=shipment,
                state="OPEN",
                reason=cause,
                opened_at=_iso_z(self._clock()),
                pk=pk,
            )
            key = (pk, "META")
            self._incidents[key] = incident
            self._events[key] = []
            self._event_seq[key] = 0
            return incident

    def get(self, tenant_id: str, incident_id: str) -> Incident:
        context = TenantContext(tenant_id=tenant_id)
        key = _meta_key(context.tenant_id, _required(incident_id, "incident_id"))
        with self._guard:
            incident = self._incidents.get(key)
        if incident is None:
            raise IncidentNotFound(f"incident {incident_id!r} does not exist")
        return incident

    def events(self, tenant_id: str, incident_id: str) -> list[TimelineEvent]:
        incident = self.get(tenant_id, incident_id)
        key = (incident.pk, incident.sk)
        with self._guard:
            return list(self._events.get(key, []))

    def transition(
        self,
        tenant_id: str,
        incident_id: str,
        *,
        from_state: str,
        to_state: str,
    ) -> TransitionResult:
        allowed = TRANSITIONS.get(to_state)
        if from_state not in TRANSITIONS or allowed is None:
            raise IllegalTransition(f"unknown state in {from_state!r} -> {to_state!r}")
        if to_state not in TRANSITIONS[from_state]:
            raise IllegalTransition(f"{from_state} -> {to_state} is not allowed")

        context = TenantContext(tenant_id=tenant_id)
        key = _meta_key(context.tenant_id, _required(incident_id, "incident_id"))
        with self._guard:
            incident = self._incidents.get(key)
            if incident is None:
                raise IncidentNotFound(f"incident {incident_id!r} does not exist")
            if incident.state != from_state:
                raise StaleIncidentUpdate(
                    f"incident {incident_id} is {incident.state}, not {from_state}"
                )
            updated = incident.model_copy(update={"state": to_state})
            self._incidents[key] = updated
            sequence = self._event_seq[key] + 1
            self._event_seq[key] = sequence
            stamp = _iso_z(self._clock())
            self._events[key].append(
                TimelineEvent(
                    sk=f"EVT#{stamp}#{sequence:04d}",
                    previous_state=from_state,
                    state=to_state,
                )
            )
            shipment_id = updated.shipment_id

        if to_state in TERMINAL_STATES:
            self.lock.release_lock(context.tenant_id, shipment_id)
        return TransitionResult(
            incident_id=incident.incident_id,
            previous_state=from_state,
            state=to_state,
        )


    def _append_open(self, tenant_id: str, incident_id: str, reason: str) -> Incident:
        key = _meta_key(tenant_id, incident_id)
        incident = self._incidents.get(key)
        if incident is None:
            raise IncidentNotFound(f"incident {incident_id!r} does not exist")
        sequence = self._event_seq[key] + 1
        self._event_seq[key] = sequence
        stamp = _iso_z(self._clock())
        self._events[key].append(
            TimelineEvent(
                sk=f"EVT#{stamp}#{sequence:04d}",
                previous_state=incident.state,
                state=incident.state,
                reason=reason,
            )
        )
        return incident


def _meta_key(tenant_id: str, incident_id: str) -> tuple[str, str]:
    return (f"TENANT#{tenant_id}#INC#{incident_id}", "META")


def _required(value: str, name: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{name} must be a non-empty string")
    return stripped


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
