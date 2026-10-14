"""WebSocket API Connection & Subscription Handler (ADR-010).

Manages client subscriptions for live incident dashboard updates.
Routes: $connect, $disconnect, subscribe.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

ROUTING_TABLE_NAME = os.environ.get("ROUTING_TABLE_NAME", "")
_dynamodb = None


def get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Lambda entry point for WebSocket events."""
    route_key = event.get("requestContext", {}).get("routeKey", "$default")
    connection_id = event.get("requestContext", {}).get("connectionId", "")

    logger.info("WebSocket route: %s | connectionId: %s", route_key, connection_id)

    if not ROUTING_TABLE_NAME or not connection_id:
        return {"statusCode": 200, "body": "OK"}

    table = get_dynamodb().Table(ROUTING_TABLE_NAME)
    ttl = int(time.time()) + 86400  # 24-hour TTL

    if route_key == "$connect":
        table.put_item(
            Item={
                "pk": f"WS#{connection_id}",
                "sk": "METADATA",
                "connection_id": connection_id,
                "connected_at": int(time.time()),
                "ttl": ttl,
            }
        )
        return {"statusCode": 200, "body": "Connected"}

    elif route_key == "$disconnect":
        # Remove connection metadata
        table.delete_item(Key={"pk": f"WS#{connection_id}", "sk": "METADATA"})
        return {"statusCode": 200, "body": "Disconnected"}

    elif route_key == "subscribe":
        try:
            body = json.loads(event.get("body", "{}"))
        except Exception:
            body = {}

        tenant_id = body.get("tenant_id", "default")
        incident_id = body.get("incident_id")

        # Save subscription
        table.put_item(
            Item={
                "pk": f"WS_SUB#{tenant_id}",
                "sk": f"CONN#{connection_id}",
                "connection_id": connection_id,
                "tenant_id": tenant_id,
                "incident_id": incident_id or "ALL",
                "ttl": ttl,
            }
        )
        return {"statusCode": 200, "body": json.dumps({"status": "Subscribed", "tenant_id": tenant_id})}

    return {"statusCode": 200, "body": "OK"}
