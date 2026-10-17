"""One stored result per ``(incident_id, step)``.

Step Functions retries a task after a Lambda timeout, and a retry must not
repeat a state transition, an outbound message, or a decision write. A handler
asks this store whether the step already finished and replays the stored result
instead of doing the work twice. The first write wins, so a retry can never
replace the decision a reviewer already saw.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from oerf.platform.tenancy.context import TenantContext


class StepRecord(BaseModel):
    """The stored outcome of one completed step."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    incident_id: str
    step: str
    completed_at: str
    decision: dict[str, Any] | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    step_output: dict[str, Any] | None = None


@runtime_checkable
class StepResultStore(Protocol):
    """Step results keyed by ``(tenant_id, incident_id, step)``."""

    def get(self, *, tenant_id: str, incident_id: str, step: str) -> StepRecord | None:
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
    ) -> StepRecord:
        """Record the outcome and return the record that is now current."""


class MemoryStepResultStore:
    """In-memory :class:`StepResultStore` for tests and local runs.

    The DynamoDB adapter behaves the same way: the first write for a key wins.
    """

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._guard = threading.Lock()
        self._records: dict[tuple[str, str, str], StepRecord] = {}

    def get(self, *, tenant_id: str, incident_id: str, step: str) -> StepRecord | None:
        with self._guard:
            return self._records.get(_key(tenant_id, incident_id, step))

    def put(
        self,
        *,
        tenant_id: str,
        incident_id: str,
        step: str,
        decision: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        step_output: dict[str, Any] | None = None,
    ) -> StepRecord:
        """Store the outcome. A repeat of a finished step returns the first record."""
        key = _key(tenant_id, incident_id, step)
        with self._guard:
            existing = self._records.get(key)
            if existing is not None:
                return existing
            record = StepRecord(
                tenant_id=key[0],
                incident_id=key[1],
                step=key[2],
                completed_at=_iso_z(self._clock()),
                decision=decision,
                result=dict(result or {}),
                step_output=deepcopy(step_output) if step_output is not None else None,
            )
            self._records[key] = record
            return record


def _key(tenant_id: str, incident_id: str, step: str) -> tuple[str, str, str]:
    context = TenantContext(tenant_id=tenant_id)
    return (
        context.tenant_id,
        _required(incident_id, "incident_id"),
        _required(step, "step"),
    )


def _required(value: str, name: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{name} must be a non-empty string")
    return stripped


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
