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
    text,
)
from oerf.platform.tenancy.context import TenantContext

TTL = timedelta(minutes=10)


class DebounceStore(Protocol):
    def record_breach(
        self,
        *,
        tenant_id: str,
        shipment_id: str,
        device_id: str,
        reading: dict[str, Any] | None = None,
    ) -> int:
        """Increase the streak and return the new count.

        The first reading of a streak is stored so ingest can write it onto
        the incident when the trigger fires.
        """

    def first_reading(
        self, *, tenant_id: str, shipment_id: str, device_id: str
    ) -> dict[str, Any] | None:
        """Return the first stored breach reading, if the streak is still live."""

    def clear(self, *, tenant_id: str, shipment_id: str, device_id: str) -> None:
        """Drop the streak after an in-range reading."""


def observe(
    store: DebounceStore,
    *,
    tenant_id: str,
    shipment_id: str,
    device_id: str,
    in_breach: bool,
    reading: dict[str, Any] | None = None,
) -> int:
    """Record a breach, or clear the streak when the reading is back in range."""

    if not in_breach:
        store.clear(tenant_id=tenant_id, shipment_id=shipment_id, device_id=device_id)
        return 0
    return store.record_breach(
        tenant_id=tenant_id,
        shipment_id=shipment_id,
        device_id=device_id,
        reading=reading,
    )


class MemoryDebounce:
    """In-memory streak counter."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._items: dict[tuple[str, str], tuple[int, datetime, dict[str, Any] | None]] = {}
        self._guard = threading.Lock()
        self._clock = clock or (lambda: datetime.now(UTC))

    def record_breach(
        self,
        *,
        tenant_id: str,
        shipment_id: str,
        device_id: str,
        reading: dict[str, Any] | None = None,
    ) -> int:
        identity = _identity(tenant_id, shipment_id, device_id)
        with self._guard:
            now = self._clock()
            current = self._items.get(identity)
            if current is None or current[1] <= now:
                stored = dict(reading) if reading else None
                self._items[identity] = (1, now + TTL, stored)
                return 1
            count = current[0] + 1
            self._items[identity] = (count, now + TTL, current[2])
            return count

    def first_reading(
        self, *, tenant_id: str, shipment_id: str, device_id: str
    ) -> dict[str, Any] | None:
        identity = _identity(tenant_id, shipment_id, device_id)
        with self._guard:
            current = self._items.get(identity)
            if current is None or current[1] <= self._clock() or current[2] is None:
                return None
            return dict(current[2])

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

    def record_breach(
        self,
        *,
        tenant_id: str,
        shipment_id: str,
        device_id: str,
        reading: dict[str, Any] | None = None,
    ) -> int:
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
            item = {
                **item_key,
                "count": n(1),
                "expires_at": n(expires),
                "tenant_id": s(context.tenant_id),
                "shipment_id": s(shipment),
                "device_id": s(device),
            }
            item.update(_reading_attributes(reading))
            try:
                self._client.put_item(
                    TableName=self._table,
                    Item=item,
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

    def first_reading(
        self, *, tenant_id: str, shipment_id: str, device_id: str
    ) -> dict[str, Any] | None:
        context = TenantContext(tenant_id=tenant_id)
        shipment = required_text(shipment_id, "shipment_id")
        device = required_text(device_id, "device_id")
        loaded = self._client.get_item(
            TableName=self._table,
            Key=key(debounce_pk(context.tenant_id, shipment, device), "META"),
        )
        item = loaded.get("Item") if isinstance(loaded, dict) else None
        if not item:
            return None
        event_id = text(item["first_event_id"]) if "first_event_id" in item else ""
        timestamp = text(item["first_ts"]) if "first_ts" in item else ""
        if not event_id or not timestamp:
            return None
        temperature = item.get("first_temperature_c", {}).get("N")
        sequence = item.get("first_seq", {}).get("N")
        if temperature is None or sequence is None:
            return None
        return {
            "event_id": event_id,
            "timestamp": timestamp,
            "temperature_c": float(temperature),
            "sequence": int(sequence),
        }

    def clear(self, *, tenant_id: str, shipment_id: str, device_id: str) -> None:
        context = TenantContext(tenant_id=tenant_id)
        shipment = required_text(shipment_id, "shipment_id")
        device = required_text(device_id, "device_id")
        self._client.delete_item(
            TableName=self._table,
            Key=key(debounce_pk(context.tenant_id, shipment, device), "META"),
        )


def _reading_attributes(reading: dict[str, Any] | None) -> dict[str, Any]:
    if not reading:
        return {}
    event_id = str(reading.get("event_id") or "").strip()
    timestamp = str(reading.get("timestamp") or "").strip()
    temperature = reading.get("temperature_c")
    sequence = reading.get("sequence")
    if not event_id or not timestamp:
        return {}
    if not isinstance(temperature, (int, float)) or isinstance(temperature, bool):
        return {}
    if not isinstance(sequence, int) or isinstance(sequence, bool):
        return {}
    return {
        "first_event_id": s(event_id),
        "first_ts": s(timestamp),
        "first_temperature_c": {"N": str(temperature)},
        "first_seq": n(sequence),
    }


def _identity(tenant_id: str, shipment_id: str, device_id: str) -> tuple[str, str]:
    context = TenantContext(tenant_id=tenant_id)
    shipment = required_text(shipment_id, "shipment_id")
    device = required_text(device_id, "device_id")
    return (debounce_pk(context.tenant_id, shipment, device), "META")
