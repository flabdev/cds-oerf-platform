"""In-memory open-incident lock.

The key matches the shipment lock DynamoDB will use later. Create succeeds
only when that key is absent.
"""

from __future__ import annotations

import threading
from typing import Any

from pydantic import BaseModel, ConfigDict

from oerf.platform.tenancy.context import TenantContext


class LockResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    created: bool
    incident_id: str
    reason: str | None = None


class OpenIncidentLock:
    """At most one open incident per tenant and shipment."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], str] = {}
        self._guard = threading.Lock()

    def acquire_lock(self, tenant_id: str, shipment_id: str, incident_id: str) -> LockResult:
        context = TenantContext(tenant_id=tenant_id)
        shipment = _required(shipment_id, "shipment_id")
        incident = _required(incident_id, "incident_id")
        key = _key(context.tenant_id, shipment)
        with self._guard:
            existing = self._items.get(key)
            if existing is not None:
                return LockResult(
                    created=False,
                    reason="OPEN_INCIDENT_EXISTS",
                    incident_id=existing,
                )
            self._items[key] = incident
            return LockResult(created=True, incident_id=incident)

    def release_lock(self, tenant_id: str, shipment_id: str) -> None:
        context = TenantContext(tenant_id=tenant_id)
        key = _key(context.tenant_id, _required(shipment_id, "shipment_id"))
        with self._guard:
            self._items.pop(key, None)

    def get_lock(self, tenant_id: str, shipment_id: str) -> dict[str, Any] | None:
        context = TenantContext(tenant_id=tenant_id)
        key = _key(context.tenant_id, _required(shipment_id, "shipment_id"))
        with self._guard:
            incident_id = self._items.get(key)
        if incident_id is None:
            return None
        return {"pk": key[0], "sk": key[1], "incident_id": incident_id}


def _key(tenant_id: str, shipment_id: str) -> tuple[str, str]:
    return (f"TENANT#{tenant_id}#SHIP#{shipment_id}", "OPEN_INCIDENT")


def _required(value: str, name: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{name} must be a non-empty string")
    return stripped
