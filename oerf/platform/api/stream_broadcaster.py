"""DynamoDB Stream Broadcaster for Live Dashboard Updates (ADR-010).

Triggered by changes to IncidentTable. Broadcasts updates to all subscribed
WebSocket connections for the incident's tenant.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

ROUTING_TABLE_NAME = os.environ.get("ROUTING_TABLE_NAME", "")
WEBSOCKET_ENDPOINT = os.environ.get("WEBSOCKET_ENDPOINT", "")

_dynamodb = None
_apigw_client = None
_deserializer = TypeDeserializer()


def get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def get_apigw():
    global _apigw_client
    if _apigw_client is None and WEBSOCKET_ENDPOINT:
        _apigw_client = boto3.client(
            "apigatewaymanagementapi",
            endpoint_url=WEBSOCKET_ENDPOINT,
        )
    return _apigw_client


def deserialize_dynamodb_image(image: dict[str, Any]) -> dict[str, Any]:
    """Convert DynamoDB Stream image to standard Python dict."""
    return {k: _deserializer.deserialize(v) for k, v in image.items()}


def subscription_targets(items: list[dict[str, Any]], incident_id: str) -> list[tuple[str, str]]:
    """Return ``(connection_id, scope)`` for this incident.

    Tenant scope receives the summary. Incident scope receives the update only
    when the stored incident id matches. ``ALL`` and ``*`` receive nothing.
    """

    targets: list[tuple[str, str]] = []
    for item in items:
        connection_id = item.get("connection_id")
        scope = item.get("scope")
        if not isinstance(connection_id, str) or not connection_id:
            continue
        if scope == "tenant" and "incident_id" not in item:
            targets.append((connection_id, "tenant"))
            continue
        subscribed = item.get("incident_id")
        if (
            scope == "incident"
            and isinstance(subscribed, str)
            and subscribed not in {"", "ALL", "*"}
            and subscribed == incident_id
        ):
            targets.append((connection_id, "incident"))
    return targets


def dashboard_envelopes(
    item: dict[str, Any],
    *,
    emitted_at: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return the summary and incident envelopes, or None when the item has no identity."""

    tenant_id = item.get("tenant_id")
    incident_id = item.get("incident_id")
    if isinstance(item.get("sk"), str) and item["sk"].startswith("EVT#"):
        identity = _incident_identity(item.get("pk"))
        if identity is None:
            return None
        key_tenant, key_incident = identity
        if isinstance(tenant_id, str) and tenant_id.strip() != key_tenant:
            return None
        tenant_id = key_tenant
        incident_id = incident_id or key_incident
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        return None
    if not isinstance(incident_id, str) or not incident_id.strip():
        return None
    tenant_id = tenant_id.strip()
    incident_id = incident_id.strip()
    state = _text(item.get("state")) or _text(item.get("status"))
    if state is None:
        return None
    version = _version(item.get("version"))
    stage = _text(item.get("stage")) or state
    summary = {
        "event_type": "INCIDENT_SUMMARY",
        "tenant_id": tenant_id,
        "incident_id": incident_id,
        "version": version,
        "emitted_at": emitted_at,
        "scope": "tenant",
        "data": {
            "incident_id": incident_id,
            "state": state,
            "stage": stage,
            "version": version,
        },
    }
    data: dict[str, Any] = {
        "incident_id": incident_id,
        "state": state,
        "stage": stage,
        "version": version,
    }
    shipment_id = _text(item.get("shipment_id"))
    if shipment_id is not None:
        data["shipment_id"] = shipment_id
    updated_at = _text(item.get("updated_at"))
    if updated_at is not None:
        data["updated_at"] = updated_at
    config_version = _version(item.get("config_version"), default=None)
    if config_version is not None:
        data["config_version"] = config_version
    reading = _reading(item.get("latest_reading"))
    if reading is not None:
        data["latest_reading"] = reading
    if str(item.get("sk") or "").startswith("EVT#"):
        previous = _text(item.get("previous_state"))
        if previous is not None:
            data["from_state"] = previous
            data["to_state"] = state
            data["timeline"] = [{"type": "state_changed", "at": emitted_at, "state": state}]
    update = {
        "event_type": "INCIDENT_UPDATE",
        "tenant_id": tenant_id,
        "incident_id": incident_id,
        "version": version,
        "emitted_at": emitted_at,
        "scope": "incident",
        "data": data,
    }
    return summary, update


def get_tenant_connections(tenant_id: str, incident_id: str | None = None) -> list[tuple[str, str]]:
    """Fetch connections whose stored scope may receive this incident."""
    if not ROUTING_TABLE_NAME or not incident_id:
        return []

    table = get_dynamodb().Table(ROUTING_TABLE_NAME)
    try:
        response = table.query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": f"WS_SUB#{tenant_id}"},
        )
        return subscription_targets(response.get("Items", []), incident_id)
    except Exception as exc:
        logger.error("Failed to query WebSocket connections: %s", exc)
        return []


def handler(event: dict[str, Any], context: Any) -> None:
    """Process DynamoDB stream records and broadcast to WebSocket clients."""
    records = event.get("Records", [])
    logger.info("Processing %d DynamoDB stream records", len(records))

    apigw = get_apigw()
    if not apigw:
        logger.warning("WEBSOCKET_ENDPOINT not configured, skipping broadcast")
        return

    table = get_dynamodb().Table(ROUTING_TABLE_NAME) if ROUTING_TABLE_NAME else None

    for record in records:
        new_image_raw = record.get("dynamodb", {}).get("NewImage")

        if not new_image_raw:
            continue

        item = deserialize_dynamodb_image(new_image_raw)
        emitted_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        envelopes = dashboard_envelopes(item, emitted_at=emitted_at)
        if envelopes is None:
            continue
        summary, update = envelopes
        tenant_id = summary["tenant_id"]
        incident_id = summary["incident_id"]

        for conn_id, scope in get_tenant_connections(tenant_id, incident_id):
            payload = summary if scope == "tenant" else update
            try:
                apigw.post_to_connection(
                    ConnectionId=conn_id,
                    Data=json.dumps(payload).encode("utf-8"),
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "GoneException":
                    logger.info("Pruning disconnected client %s", conn_id)
                    if table:
                        table.delete_item(
                            Key={"pk": f"WS_SUB#{tenant_id}", "sk": f"CONN#{conn_id}"}
                        )
                else:
                    logger.error("Failed to push to connection %s: %s", conn_id, exc)


def _incident_identity(value: Any) -> tuple[str, str] | None:
    """Extract tenant and incident ids from canonical incident partition keys."""
    if not isinstance(value, str):
        return None
    parts = value.split("#")
    if len(parts) != 4 or parts[0] != "TENANT" or parts[2] != "INC":
        return None
    tenant_id, incident_id = parts[1], parts[3]
    if not tenant_id.strip() or not incident_id.strip():
        return None
    return tenant_id, incident_id


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip() and value.strip() not in {"ALL", "*"}:
        return value.strip()
    return None


def _version(value: Any, *, default: int | None = 1) -> int | None:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, Decimal) and value == int(value):
        return int(value)
    return default


def _reading(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    ts = _text(value.get("ts"))
    temperature = value.get("temperature_c")
    if ts is None or isinstance(temperature, bool):
        return None
    if not isinstance(temperature, (int, float, Decimal)):
        return None
    return {"ts": ts, "temperature_c": float(temperature)}
