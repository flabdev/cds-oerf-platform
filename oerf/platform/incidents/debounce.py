"""Count consecutive breaches for one tenant, shipment, and device.

A safe reading clears the streak. The streak also expires after ten minutes.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from oerf.platform.storage.dynamo import (
    debounce_pk,
    epoch_seconds,
    is_conditional_failure,
    key,
    n,
    number,
    required_text,
    s,
)
from oerf.platform.tenancy.context import TenantContext

TTL = timedelta(minutes=10)


class DebounceStore(Protocol):
    def record_breach(self, *, tenant_id: str, shipment_id: str, device_id: str) -> int:
        """Increase the streak and return the new count."""

    def clear(self, *, tenant_id: str, shipment_id: str, device_id: str) -> None:
        """Drop the streak after an in-range reading."""


def observe(
    store: DebounceStore,
    *,
    tenant_id: str,
    shipment_id: str,
    device_id: str,
    in_breach: bool,
) -> int:
    """Record a breach, or clear the streak when the reading is back in range."""

    if not in_breach:
        store.clear(tenant_id=tenant_id, shipment_id=shipment_id, device_id=device_id)
        return 0
    return store.record_breach(tenant_id=tenant_id, shipment_id=shipment_id, device_id=device_id)


class MemoryDebounce:
    """In-memory streak counter."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._items: dict[tuple[str, str], tuple[int, datetime]] = {}
        self._guard = threading.Lock()
        self._clock = clock or (lambda: datetime.now(UTC))

    def record_breach(self, *, tenant_id: str, shipment_id: str, device_id: str) -> int:
        identity = _identity(tenant_id, shipment_id, device_id)
        with self._guard:
            now = self._clock()
            current = self._items.get(identity)
            count = 1 if current is None or current[1] <= now else current[0] + 1
            self._items[identity] = (count, now + TTL)
            return count

    def clear(self, *, tenant_id: str, shipment_id: str, device_id: str) -> None:
        identity = _identity(tenant_id, shipment_id, device_id)
        with self._guard:
            self._items.pop(identity, None)


class DynamoDebounce:
    """Streak counter on the platform table. The client is injected."""

    def __init__(
        self,
        client: Any,
        table_name: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._table = table_name
        self._clock = clock or (lambda: datetime.now(UTC))

    def record_breach(self, *, tenant_id: str, shipment_id: str, device_id: str) -> int:
        context = TenantContext(tenant_id=tenant_id)
        shipment = required_text(shipment_id, "shipment_id")
        device = required_text(device_id, "device_id")
        pk = debounce_pk(context.tenant_id, shipment, device)
        item_key = key(pk, "META")
        names = {
            "#pk": "pk",
            "#count": "count",
            "#expires_at": "expires_at",
            "#tenant_id": "tenant_id",
            "#shipment_id": "shipment_id",
            "#device_id": "device_id",
        }
        for _ in range(5):
            now = self._clock()
            expires = epoch_seconds(now + TTL)
            values = {
                ":zero": n(0),
                ":one": n(1),
                ":now": n(epoch_seconds(now)),
                ":expires": n(expires),
                ":tenant": s(context.tenant_id),
                ":shipment": s(shipment),
                ":device": s(device),
            }
            try:
                updated = self._client.update_item(
                    TableName=self._table,
                    Key=item_key,
                    UpdateExpression=(
                        "SET #count = if_not_exists(#count, :zero) + :one, "
                        "#expires_at = :expires, #tenant_id = :tenant, "
                        "#shipment_id = :shipment, #device_id = :device"
                    ),
                    ConditionExpression="attribute_exists(#pk) AND #expires_at > :now",
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues=values,
                    ReturnValues="UPDATED_NEW",
                )
            except Exception as exc:
                if not is_conditional_failure(exc):
                    raise
            else:
                return number(updated["Attributes"]["count"])
            try:
                self._client.put_item(
                    TableName=self._table,
                    Item={
                        **item_key,
                        "count": n(1),
                        "expires_at": n(expires),
                        "tenant_id": s(context.tenant_id),
                        "shipment_id": s(shipment),
                        "device_id": s(device),
                    },
                    ConditionExpression="attribute_not_exists(#pk) OR #expires_at <= :now",
                    ExpressionAttributeNames={"#pk": "pk", "#expires_at": "expires_at"},
                    ExpressionAttributeValues={":now": n(epoch_seconds(now))},
                )
            except Exception as exc:
                if not is_conditional_failure(exc):
                    raise
                continue
            return 1
        raise RuntimeError("debounce update conflicted")

    def clear(self, *, tenant_id: str, shipment_id: str, device_id: str) -> None:
        context = TenantContext(tenant_id=tenant_id)
        shipment = required_text(shipment_id, "shipment_id")
        device = required_text(device_id, "device_id")
        self._client.delete_item(
            TableName=self._table,
            Key=key(debounce_pk(context.tenant_id, shipment, device), "META"),
        )


def _identity(tenant_id: str, shipment_id: str, device_id: str) -> tuple[str, str]:
    context = TenantContext(tenant_id=tenant_id)
    shipment = required_text(shipment_id, "shipment_id")
    device = required_text(device_id, "device_id")
    return (debounce_pk(context.tenant_id, shipment, device), "META")
