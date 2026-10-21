"""WebSocket API Connection & Subscription Handler (ADR-010, ADR-011).

Manages client subscriptions for live incident dashboard updates.
Routes: $connect, $disconnect, subscribe.

The tenant a connection may stream is resolved by ``ws_authorizer`` from a
verified Cognito token at ``$connect`` and stored with the connection metadata.
Subscribe requests are scoped to that stored tenant, so a tenant id supplied in
the message payload can never widen a client's access to other tenants.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import boto3

from oerf.platform.api.claims import DEFAULT_ROLE

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

ROUTING_TABLE_NAME = os.environ.get("ROUTING_TABLE_NAME", "")
CONNECTION_TTL_SECONDS = 86400  # 24-hour TTL
MAX_INCIDENT_ID_LENGTH = 128

_dynamodb = None


def get_dynamodb():
    """Lazily create the DynamoDB resource."""
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def get_table():
    """Return the routing table, or ``None`` when it is not configured locally."""
    if not ROUTING_TABLE_NAME:
        return None
    return get_dynamodb().Table(ROUTING_TABLE_NAME)


def connection_ttl() -> int:
    """Return the epoch-second ``expires_at`` for connection and subscription rows."""
    return int(time.time()) + CONNECTION_TTL_SECONDS


def get_authorizer_context(event: dict[str, Any]) -> dict[str, Any]:
    """Return the trusted context produced by the WebSocket authorizer."""
    context = event.get("requestContext", {}).get("authorizer")
    return context if isinstance(context, dict) else {}


def get_connection_binding(table, connection_id: str) -> dict[str, Any]:
    """Return the binding the authorizer stored for an authorized connection."""
    try:
        response = table.get_item(Key={"pk": f"WS#{connection_id}", "sk": "METADATA"})
    except Exception as exc:
        logger.error("Failed to read metadata for connection %s: %s", connection_id, exc)
        return {}
    binding = response.get("Item")
    return binding if isinstance(binding, dict) else {}


def parse_subscription(event: dict[str, Any]) -> dict[str, str] | None:
    """Return the scope, or None when the body is not the contract shape."""

    try:
        body = json.loads(event.get("body") or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(body, dict) or set(body) - {"scope", "incident_id"}:
        return None
    scope = body.get("scope")
    if scope == "tenant":
        if "incident_id" in body:
            return None
        return {"scope": "tenant"}
    if scope != "incident":
        return None
    incident_id = body.get("incident_id")
    if not isinstance(incident_id, str):
        return None
    incident_id = incident_id.strip()
    if not incident_id or incident_id in {"ALL", "*"} or len(incident_id) > MAX_INCIDENT_ID_LENGTH:
        return None
    return {"scope": "incident", "incident_id": incident_id}


def handle_connect(table, event: dict[str, Any], connection_id: str, ttl: int) -> dict[str, Any]:
    """Record an authorized connection and confirm the tenant it is bound to."""
    authorizer = get_authorizer_context(event)
    binding = get_connection_binding(table, connection_id)

    tenant_id = str(authorizer.get("tenant_id") or binding.get("tenant_id") or "")
    bound_tenant = str(binding.get("tenant_id") or "")
    if not tenant_id:
        # Fail closed: an unauthorized connection must not receive a usable id.
        logger.warning("Rejecting connection %s with no authenticated tenant", connection_id)
        return {"statusCode": 401, "body": "Unauthorized"}

    if bound_tenant and bound_tenant != tenant_id:
        logger.error(
            "Rejecting connection %s: authorized tenant %s does not match bound tenant %s",
            connection_id,
            tenant_id,
            bound_tenant,
        )
        return {"statusCode": 401, "body": "Unauthorized"}

    table.put_item(
        Item={
            "pk": f"WS#{connection_id}",
            "sk": "METADATA",
            "connection_id": connection_id,
            "tenant_id": tenant_id,
            "role": str(authorizer.get("role") or binding.get("role") or DEFAULT_ROLE),
            "sub": str(authorizer.get("sub") or binding.get("sub") or ""),
            "connected_at": int(time.time()),
            "expires_at": ttl,
        }
    )
    return {"statusCode": 200, "body": "Connected"}


def handle_disconnect(table, connection_id: str) -> dict[str, Any]:
    """Remove the connection metadata and its subscription row."""
    binding = get_connection_binding(table, connection_id)
    tenant_id = binding.get("tenant_id")
    if tenant_id:
        table.delete_item(
            Key={"pk": f"WS_SUB#{tenant_id}", "sk": f"CONN#{connection_id}"}
        )
    table.delete_item(Key={"pk": f"WS#{connection_id}", "sk": "METADATA"})
    return {"statusCode": 200, "body": "Disconnected"}


def handle_subscribe(
    table, event: dict[str, Any], connection_id: str, ttl: int
) -> dict[str, Any]:
    """Subscribe a connection to its own tenant's incident stream."""
    binding = get_connection_binding(table, connection_id)
    tenant_id = binding.get("tenant_id")
    if not tenant_id:
        logger.warning("Rejecting subscribe for unknown connection %s", connection_id)
        return {"statusCode": 401, "body": "Unauthorized"}

    subscription = parse_subscription(event)
    if subscription is None:
        logger.warning("Rejecting subscribe for connection %s: invalid scope", connection_id)
        return {"statusCode": 400, "body": "Invalid subscription"}

    item = {
        "pk": f"WS_SUB#{tenant_id}",
        "sk": f"CONN#{connection_id}",
        "connection_id": connection_id,
        "tenant_id": tenant_id,
        "scope": subscription["scope"],
        "expires_at": ttl,
    }
    if subscription["scope"] == "incident":
        item["incident_id"] = subscription["incident_id"]
    table.put_item(Item=item)
    return {
        "statusCode": 200,
        "body": json.dumps({"status": "Subscribed", "tenant_id": tenant_id}),
    }


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point for WebSocket connect, disconnect and subscribe events."""
    request_context = event.get("requestContext", {})
    route_key = request_context.get("routeKey", "$default")
    connection_id = str(request_context.get("connectionId", ""))
    logger.info("WebSocket route: %s | connectionId: %s", route_key, connection_id)

    table = get_table()
    if table is None:
        # Local runs without a routing table are a no-op, as before.
        logger.warning("ROUTING_TABLE_NAME not configured, skipping %s", route_key)
        return {"statusCode": 200, "body": "OK"}

    if route_key == "$connect":
        if not connection_id:
            return {"statusCode": 401, "body": "Unauthorized"}
        return handle_connect(table, event, connection_id, connection_ttl())

    if route_key == "$disconnect":
        if not connection_id:
            return {"statusCode": 200, "body": "Disconnected"}
        return handle_disconnect(table, connection_id)

    if route_key == "subscribe":
        if not connection_id:
            return {"statusCode": 401, "body": "Unauthorized"}
        return handle_subscribe(table, event, connection_id, connection_ttl())

    return {"statusCode": 200, "body": "OK"}
