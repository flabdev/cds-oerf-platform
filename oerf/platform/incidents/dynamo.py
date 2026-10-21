"""DynamoDB incident store and shipment lock.

The client is injected. This module never constructs one.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from itertools import count
from typing import Any

from oerf.platform.incidents.lock import LockResult
from oerf.platform.incidents.repository import (
    TERMINAL_STATES,
    Incident,
    IncidentNotFound,
    StaleIncidentUpdate,
    TimelineEvent,
    TransitionResult,
    ensure_transition,
)
from oerf.platform.storage.dynamo import (
    Item,
    incident_pk,
    is_conditional_failure,
    iso_z,
    key,
    lock_pk,
    n,
    number,
    required_text,
    s,
    text,
)
from oerf.platform.tenancy.context import TenantContext

_NAMES = {"#pk": "pk"}


class DynamoOpenIncidentLock:
    """Conditional lock item on the incident table."""

    def __init__(self, client: Any, table_name: str) -> None:
        self._client = client
        self._table = table_name

    def acquire_lock(self, tenant_id: str, shipment_id: str, incident_id: str) -> LockResult:
        context = TenantContext(tenant_id=tenant_id)
        shipment = required_text(shipment_id, "shipment_id")
        incident = required_text(incident_id, "incident_id")
        pk = lock_pk(context.tenant_id, shipment)
        item: Item = {
            **key(pk, "OPEN_INCIDENT"),
            "incident_id": s(incident),
            "tenant_id": s(context.tenant_id),
            "shipment_id": s(shipment),
        }
        try:
            self._client.put_item(
                TableName=self._table,
                Item=item,
                ConditionExpression="attribute_not_exists(#pk)",
                ExpressionAttributeNames=_NAMES,
            )
        except Exception as exc:
            if not is_conditional_failure(exc):
                raise
            existing = self.get_lock(context.tenant_id, shipment)
            if existing is None:
                raise
            return LockResult(
                created=False,
                reason="OPEN_INCIDENT_EXISTS",
                incident_id=str(existing["incident_id"]),
            )
        return LockResult(created=True, incident_id=incident)

    def release_lock(self, tenant_id: str, shipment_id: str) -> None:
        context = TenantContext(tenant_id=tenant_id)
        shipment = required_text(shipment_id, "shipment_id")
        self._client.delete_item(
            TableName=self._table,
            Key=key(lock_pk(context.tenant_id, shipment), "OPEN_INCIDENT"),
        )

    def get_lock(self, tenant_id: str, shipment_id: str) -> dict[str, Any] | None:
        context = TenantContext(tenant_id=tenant_id)
        shipment = required_text(shipment_id, "shipment_id")
        pk = lock_pk(context.tenant_id, shipment)
        loaded = self._client.get_item(TableName=self._table, Key=key(pk, "OPEN_INCIDENT"))
        item = loaded.get("Item")
        if not item:
            return None
        return {
            "pk": pk,
            "sk": "OPEN_INCIDENT",
            "incident_id": text(item["incident_id"]),
            "tenant_id": text(item["tenant_id"]),
        }


class DynamoIncidentStore:
    """Incident meta, timeline, and shipment lock on one incident table."""

    def __init__(
        self,
        client: Any,
        table_name: str,
        *,
        clock: Callable[[], datetime] | None = None,
        next_id: Callable[[], str] | None = None,
    ) -> None:
        self._client = client
        self._table = table_name
        self.lock = DynamoOpenIncidentLock(client, table_name)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._ids = count(1)
        self._next_id = next_id or (lambda: f"INC-{next(self._ids):06d}")

    def create(
        self,
        *,
        tenant_id: str,
        shipment_id: str,
        reason: str,
        config_version: int | None = None,
        config_snapshot: dict[str, Any] | None = None,
    ) -> Incident:
        """Put the meta row, the opening event, and the lock in one transaction.

        A concurrent create that loses the lock appends to the incident already stored.
        """

        context = TenantContext(tenant_id=tenant_id)
        shipment = required_text(shipment_id, "shipment_id")
        cause = required_text(reason, "reason")
        incident_id = self._next_id()
        opened_at = iso_z(self._clock())
        pk = incident_pk(context.tenant_id, incident_id)
        meta = _meta_item(
            pk=pk,
            incident_id=incident_id,
            tenant_id=context.tenant_id,
            shipment_id=shipment,
            state="OPEN",
            reason=cause,
            opened_at=opened_at,
            event_seq=1,
            config_version=config_version,
            config_snapshot=config_snapshot,
        )
        try:
            self._client.transact_write_items(
                TransactItems=[
                    _put(self._table, meta),
                    _put(
                        self._table,
                        {
                            **key(lock_pk(context.tenant_id, shipment), "OPEN_INCIDENT"),
                            "incident_id": s(incident_id),
                            "tenant_id": s(context.tenant_id),
                            "shipment_id": s(shipment),
                        },
                    ),
                    _put(
                        self._table,
                        _event_item(
                            pk=pk,
                            sk=f"EVT#{opened_at}#0001",
                            tenant_id=context.tenant_id,
                            previous_state="",
                            state="OPEN",
                            reason=cause,
                        ),
                    ),
                ]
            )
        except Exception as exc:
            if not is_conditional_failure(exc):
                raise
            existing = self.lock.get_lock(context.tenant_id, shipment)
            if existing is None:
                raise
            return self._append(context.tenant_id, str(existing["incident_id"]), cause)
        return _incident(meta)

    def get(self, tenant_id: str, incident_id: str) -> Incident:
        context = TenantContext(tenant_id=tenant_id)
        item = self._load(context.tenant_id, required_text(incident_id, "incident_id"))
        if item is None:
            raise IncidentNotFound(f"incident {incident_id!r} does not exist")
        return _incident(item)

    def events(self, tenant_id: str, incident_id: str) -> list[TimelineEvent]:
        incident = self.get(tenant_id, incident_id)
        response = self._client.query(
            TableName=self._table,
            KeyConditionExpression="#pk = :pk AND begins_with(#sk, :prefix)",
            ExpressionAttributeNames={"#pk": "pk", "#sk": "sk"},
            ExpressionAttributeValues={":pk": s(incident.pk), ":prefix": s("EVT#")},
        )
        return [_timeline(item) for item in response.get("Items", [])]

    def transition(
        self,
        tenant_id: str,
        incident_id: str,
        *,
        from_state: str,
        to_state: str,
    ) -> TransitionResult:
        ensure_transition(from_state, to_state)
        context = TenantContext(tenant_id=tenant_id)
        incident_key = required_text(incident_id, "incident_id")
        current = self._load(context.tenant_id, incident_key)
        if current is None:
            raise IncidentNotFound(f"incident {incident_id!r} does not exist")
        sequence = number(current["event_seq"])
        nxt = sequence + 1
        stamp = iso_z(self._clock())
        pk = text(current["pk"])
        actions: list[dict[str, Any]] = [
            {
                "Update": {
                    "TableName": self._table,
                    "Key": key(pk, "META"),
                    "UpdateExpression": (
                        "SET #state = :state, #event_seq = #event_seq + :one, "
                        "#gsi1pk = :gsi1pk, #gsi1sk = :gsi1sk"
                    ),
                    "ConditionExpression": "#state = :from_state AND #event_seq = :current",
                    "ExpressionAttributeNames": {
                        "#state": "state",
                        "#event_seq": "event_seq",
                        "#gsi1pk": "GSI1PK",
                        "#gsi1sk": "GSI1SK",
                    },
                    "ExpressionAttributeValues": {
                        ":state": s(to_state),
                        ":from_state": s(from_state),
                        ":one": n(1),
                        ":current": n(sequence),
                        ":gsi1pk": s(f"TENANT#{context.tenant_id}#STATUS#{to_state}"),
                        ":gsi1sk": s(text(current["opened_at"])),
                    },
                }
            },
            _put(
                self._table,
                _event_item(
                    pk=pk,
                    sk=f"EVT#{stamp}#{nxt:04d}",
                    tenant_id=context.tenant_id,
                    previous_state=from_state,
                    state=to_state,
                    reason=None,
                ),
            ),
        ]
        if to_state in TERMINAL_STATES:
            actions.append(
                {
                    "Delete": {
                        "TableName": self._table,
                        "Key": key(
                            lock_pk(context.tenant_id, text(current["shipment_id"])),
                            "OPEN_INCIDENT",
                        ),
                    }
                }
            )
        try:
            self._client.transact_write_items(TransactItems=actions)
        except Exception as exc:
            if not is_conditional_failure(exc):
                raise
            latest = self._load(context.tenant_id, incident_key)
            if latest is None:
                raise IncidentNotFound(f"incident {incident_id!r} does not exist") from exc
            raise StaleIncidentUpdate(
                f"incident {incident_id} is {text(latest['state'])}, not {from_state}"
            ) from exc
        return TransitionResult(
            incident_id=incident_key,
            previous_state=from_state,
            state=to_state,
        )

    def _append(self, tenant_id: str, incident_id: str, reason: str) -> Incident:
        for _ in range(5):
            current = self._load(tenant_id, incident_id)
            if current is None:
                raise IncidentNotFound(f"incident {incident_id!r} does not exist")
            sequence = number(current["event_seq"])
            nxt = sequence + 1
            stamp = iso_z(self._clock())
            pk = text(current["pk"])
            try:
                self._client.transact_write_items(
                    TransactItems=[
                        {
                            "Update": {
                                "TableName": self._table,
                                "Key": key(pk, "META"),
                                "UpdateExpression": "SET #event_seq = #event_seq + :one",
                                "ConditionExpression": "#event_seq = :current",
                                "ExpressionAttributeNames": {"#event_seq": "event_seq"},
                                "ExpressionAttributeValues": {
                                    ":one": n(1),
                                    ":current": n(sequence),
                                },
                            }
                        },
                        _put(
                            self._table,
                            _event_item(
                                pk=pk,
                                sk=f"EVT#{stamp}#{nxt:04d}",
                                tenant_id=tenant_id,
                                previous_state=text(current["state"]),
                                state=text(current["state"]),
                                reason=reason,
                            ),
                        ),
                    ]
                )
            except Exception as exc:
                if not is_conditional_failure(exc):
                    raise
                continue
            return _incident(current)
        raise IncidentNotFound(f"incident {incident_id!r} could not be updated")

    def _load(self, tenant_id: str, incident_id: str) -> Item | None:
        loaded = self._client.get_item(
            TableName=self._table,
            Key=key(incident_pk(tenant_id, incident_id), "META"),
        )
        item = loaded.get("Item")
        return item if item else None


def _put(table: str, item: Item) -> dict[str, Any]:
    return {
        "Put": {
            "TableName": table,
            "Item": item,
            "ConditionExpression": "attribute_not_exists(#pk)",
            "ExpressionAttributeNames": _NAMES,
        }
    }


def _meta_item(
    *,
    pk: str,
    incident_id: str,
    tenant_id: str,
    shipment_id: str,
    state: str,
    reason: str,
    opened_at: str,
    event_seq: int,
    config_version: int | None = None,
    config_snapshot: dict[str, Any] | None = None,
) -> Item:
    item = {
        **key(pk, "META"),
        "incident_id": s(incident_id),
        "tenant_id": s(tenant_id),
        "shipment_id": s(shipment_id),
        "state": s(state),
        "reason": s(reason),
        "opened_at": s(opened_at),
        "event_seq": n(event_seq),
        "GSI1PK": s(f"TENANT#{tenant_id}#STATUS#{state}"),
        "GSI1SK": s(opened_at),
    }
    if config_version is not None:
        item["config_version"] = n(config_version)
    if config_snapshot is not None:
        item["config_snapshot"] = s(json.dumps(config_snapshot, sort_keys=True))
    return item


def _event_item(
    *,
    pk: str,
    sk: str,
    tenant_id: str,
    previous_state: str,
    state: str,
    reason: str | None,
) -> Item:
    item = {
        **key(pk, sk),
        "tenant_id": s(tenant_id),
        "previous_state": s(previous_state),
        "state": s(state),
    }
    if reason is not None:
        item["reason"] = s(reason)
    return item


def _incident(item: Item) -> Incident:
    return Incident(
        incident_id=text(item["incident_id"]),
        tenant_id=text(item["tenant_id"]),
        shipment_id=text(item["shipment_id"]),
        state=text(item["state"]),
        reason=text(item["reason"]),
        opened_at=text(item["opened_at"]),
        pk=text(item["pk"]),
        sk=text(item["sk"]),
        config_version=_optional_number(item, "config_version"),
        config_snapshot=_optional_json(item, "config_snapshot"),
    )


def _optional_number(item: Item, name: str) -> int | None:
    if name not in item:
        return None
    return number(item[name])


def _optional_json(item: Item, name: str) -> dict[str, Any] | None:
    if name not in item:
        return None
    parsed = json.loads(text(item[name]))
    return parsed if isinstance(parsed, dict) else None


def _timeline(item: Item) -> TimelineEvent:
    reason = item.get("reason")
    return TimelineEvent(
        sk=text(item["sk"]),
        previous_state=text(item["previous_state"]),
        state=text(item["state"]),
        reason=text(reason) if reason else None,
    )
