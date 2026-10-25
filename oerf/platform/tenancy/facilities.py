"""Facility rows on the tenant table. Ids and drive times come from the row."""

from __future__ import annotations

from typing import Any, Protocol

from oerf.platform.storage.dynamo import s
from oerf.platform.tenancy.context import TenantContext


class FacilityCatalog(Protocol):
    def list_facilities(self, tenant_id: str) -> list[dict[str, Any]]:
        """Return this tenant's facility rows. Another tenant's key does not match."""


class MemoryFacilityCatalog:
    """Rows already decoded, for tests and local runs."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def list_facilities(self, tenant_id: str) -> list[dict[str, Any]]:
        context = TenantContext(tenant_id=tenant_id)
        return [
            dict(row)
            for row in self._rows
            if str(row.get("tenant_id") or context.tenant_id) == context.tenant_id
        ]


class DynamoFacilityCatalog:
    """``TENANT#{tenant}`` / ``FACILITY#…`` on the tenant table."""

    def __init__(self, client: Any, table_name: str) -> None:
        self._client = client
        self._table = table_name

    def list_facilities(self, tenant_id: str) -> list[dict[str, Any]]:
        context = TenantContext(tenant_id=tenant_id)
        rows: list[dict[str, Any]] = []
        start: dict[str, Any] | None = None
        while True:
            kwargs: dict[str, Any] = {
                "TableName": self._table,
                "KeyConditionExpression": "#pk = :pk AND begins_with(#sk, :prefix)",
                "ExpressionAttributeNames": {"#pk": "pk", "#sk": "sk"},
                "ExpressionAttributeValues": {
                    ":pk": s(f"TENANT#{context.tenant_id}"),
                    ":prefix": s("FACILITY#"),
                },
            }
            if start is not None:
                kwargs["ExclusiveStartKey"] = start
            response = self._client.query(**kwargs)
            rows.extend(_row(item) for item in response.get("Items", []))
            start = response.get("LastEvaluatedKey")
            if not start:
                return rows


def _row(item: dict[str, Any]) -> dict[str, Any]:
    """Decode one facility item. Missing drive time stays missing."""

    row: dict[str, Any] = {"tenant_id": _text(item, "tenant_id")}
    facility_id = _text(item, "facility_id")
    if facility_id:
        row["facility_id"] = facility_id
    drive = _number(item, "drive_min")
    if drive is not None:
        row["drive_min"] = drive
    capacity = _bool(item, "capacity_available")
    if capacity is not None:
        row["capacity_available"] = capacity
    approved = _bool(item, "approved")
    if approved is not None:
        row["approved"] = approved
    contact_id = _text(item, "contact_id")
    if contact_id:
        row["contact_id"] = contact_id
    return row


def _text(item: dict[str, Any], name: str) -> str:
    attribute = item.get(name)
    if not isinstance(attribute, dict):
        return ""
    value = attribute.get("S")
    return value.strip() if isinstance(value, str) else ""


def _number(item: dict[str, Any], name: str) -> float | None:
    attribute = item.get(name)
    if not isinstance(attribute, dict) or "N" not in attribute:
        return None
    return float(attribute["N"])


def _bool(item: dict[str, Any], name: str) -> bool | None:
    attribute = item.get(name)
    if not isinstance(attribute, dict) or "BOOL" not in attribute:
        return None
    return bool(attribute["BOOL"])
