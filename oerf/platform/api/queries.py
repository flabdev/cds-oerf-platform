"""Tenant-scoped incident reads and the dashboard action queue.

Reads never return a task token. The queue send is the only write, and it
goes to SQS. Incident state is not changed here.
"""

from __future__ import annotations

import json
from typing import Any

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
        view = _summary(item)
        view["timeline"] = self._timeline(context.tenant_id, incident)
        view["options"] = _json_field(item, "options") or []
        view["recommendation"] = _optional_text(item, "recommendation")
        view["approval"] = _optional_text(item, "approval")
        view["evidence"] = _public(_json_field(item, "evidence") or {})
        view["engagement_status"] = self._engagement_status(context.tenant_id, incident)
        return view

    def _metas(self, tenant_id: str) -> list[dict[str, Any]]:
        prefix = f"TENANT#{tenant_id}#INC#"
        response = self._client.scan(TableName=self._table)
        found = []
        for item in response.get("Items", []):
            pk = text(item.get("pk", {"S": ""}))
            sk = text(item.get("sk", {"S": ""}))
            if sk == "META" and pk.startswith(prefix):
                found.append(item)
        return found

    def _timeline(self, tenant_id: str, incident_id: str) -> list[dict[str, Any]]:
        response = self._client.query(
            TableName=self._table,
            KeyConditionExpression="#pk = :pk AND begins_with(#sk, :prefix)",
            ExpressionAttributeNames={"#pk": "pk", "#sk": "sk"},
            ExpressionAttributeValues={
                ":pk": s(incident_pk(tenant_id, incident_id)),
                ":prefix": s("EVT#"),
            },
        )
        events = []
        for item in response.get("Items", []):
            reason = item.get("reason")
            events.append(
                {
                    "sk": text(item["sk"]),
                    "previous_state": text(item["previous_state"]),
                    "state": text(item["state"]),
                    "reason": text(reason) if reason else None,
                }
            )
        return events

    def _engagement_status(self, tenant_id: str, incident_id: str) -> str | None:
        response = self._client.query(
            TableName=self._table,
            KeyConditionExpression="#pk = :pk AND begins_with(#sk, :prefix)",
            ExpressionAttributeNames={"#pk": "pk", "#sk": "sk"},
            ExpressionAttributeValues={
                ":pk": s(incident_pk(tenant_id, incident_id)),
                ":prefix": s("ENG#"),
            },
        )
        statuses = [text(item["status"]) for item in response.get("Items", []) if "status" in item]
        if "WAITING" in statuses:
            return "WAITING"
        return statuses[-1] if statuses else None


class SqsActionQueue:
    """FIFO send. A failure raises and the caller does not report success."""

    def __init__(self, client: Any, queue_url: str) -> None:
        self._client = client
        self._url = queue_url

    def send(self, *, body: dict[str, Any], dedup_id: str) -> bool:
        self._client.send_message(
            QueueUrl=self._url,
            MessageBody=json.dumps(body),
            MessageDeduplicationId=dedup_id,
            MessageGroupId=str(body.get("tenant_id") or "dashboard"),
        )
        return True


def _summary(item: dict[str, Any]) -> dict[str, Any]:
    state = text(item["state"])
    return {
        "incident_id": text(item["incident_id"]),
        "tenant_id": text(item["tenant_id"]),
        "state": state,
        "stage": _optional_text(item, "stage") or state,
        "shipment_id": text(item["shipment_id"]),
        "allowance_remaining": _optional_number(item, "allowance_remaining"),
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
