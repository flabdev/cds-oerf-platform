"""Tenant-scoped incident reads and the dashboard action queue.

Reads never return a task token. The queue send is the only write, and it
goes to SQS. Incident state is not changed here.
"""

from __future__ import annotations

import json
from typing import Any

from oerf.platform.incidents.repository import TRANSITIONS
from oerf.platform.storage.dynamo import incident_pk, key, s, text
from oerf.platform.tenancy.context import TenantContext

_PRIVATE = frozenset({"task_token", "phone", "e164", "email", "recipient"})
_PAGE = 50


class DynamoIncidentQuery:
    """List and detail from the incident table. The client is injected."""

    def __init__(self, client: Any, table_name: str) -> None:
        self._client = client
        self._table = table_name

    def list_incidents(
        self,
        tenant_id: str,
        *,
        limit: int = _PAGE,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        context = TenantContext(tenant_id=tenant_id)
        rows = [
            _summary(item)
            for item in self._metas(context.tenant_id)
        ]
        rows.sort(key=lambda item: item["incident_id"])
        start = 0
        if cursor:
            start = next(
                (index + 1 for index, item in enumerate(rows) if item["incident_id"] == cursor),
                len(rows),
            )
        size = min(max(limit, 1), _PAGE)
        page = rows[start : start + size]
        next_cursor = None
        if start + size < len(rows) and page:
            next_cursor = page[-1]["incident_id"]
        return {"incidents": page, "next_cursor": next_cursor}

    def get_incident(self, tenant_id: str, incident_id: str) -> dict[str, Any] | None:
        context = TenantContext(tenant_id=tenant_id)
        incident = incident_id.strip()
        loaded = self._client.get_item(
            TableName=self._table,
            Key=key(incident_pk(context.tenant_id, incident), "META"),
        )
        item = loaded.get("Item")
        if not item or text(item.get("tenant_id", {"S": ""})) != context.tenant_id:
            return None
        return {
            "incident_id": text(item["incident_id"]),
            "tenant_id": text(item["tenant_id"]),
            "shipment_id": text(item["shipment_id"]),
            "state": text(item["state"]),
            "reason": _optional_text(item, "reason") or "",
            "opened_at": _optional_text(item, "opened_at") or "",
            "config_version": _optional_number(item, "config_version"),
            "config_snapshot": _json_field(item, "config_snapshot"),
        }

    def _metas(self, tenant_id: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for state in sorted(TRANSITIONS):
            response = self._client.query(
                TableName=self._table,
                IndexName="GSI1",
                KeyConditionExpression="#pk = :pk",
                ExpressionAttributeNames={"#pk": "GSI1PK"},
                ExpressionAttributeValues={":pk": s(f"TENANT#{tenant_id}#STATUS#{state}")},
            )
            found.extend(response.get("Items", []))
        return found


class SqsActionQueue:
    """Send one message. A failure raises and the caller does not report success.

    FIFO ids are set only when the queue URL ends in ``.fifo``. A standard
    queue leaves duplicates to the inbound handler's ``INMSG#`` dedupe.
    """

    def __init__(self, client: Any, queue_url: str) -> None:
        self._client = client
        self._url = queue_url

    def send(self, *, body: dict[str, Any], dedup_id: str) -> bool:
        params: dict[str, Any] = {
            "QueueUrl": self._url,
            "MessageBody": json.dumps(body),
        }
        if self._url.endswith(".fifo"):
            params["MessageDeduplicationId"] = dedup_id
            params["MessageGroupId"] = str(body.get("tenant_id") or "dashboard")
        self._client.send_message(**params)
        return True


def _summary(item: dict[str, Any]) -> dict[str, Any]:
    state = text(item["state"])
    version = _optional_number(item, "version")
    return {
        "incident_id": text(item["incident_id"]),
        "state": state,
        "stage": _optional_text(item, "stage") or state,
        "version": 1 if version is None else int(version),
    }


def _optional_text(item: dict[str, Any], name: str) -> str | None:
    if name not in item:
        return None
    return text(item[name])


def _optional_number(item: dict[str, Any], name: str) -> int | float | None:
    if name not in item or "N" not in item[name]:
        return None
    raw = item[name]["N"]
    if "." in raw:
        return float(raw)
    return int(raw)


def _json_field(item: dict[str, Any], name: str) -> Any:
    if name not in item:
        return None
    parsed = json.loads(text(item[name]))
    return _public(parsed)


def _public(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items() if key not in _PRIVATE}
    if isinstance(value, list):
        return [_public(item) for item in value]
    return value
