"""DynamoDB Stream Broadcaster for Live Dashboard Updates (ADR-010).

Triggered by changes to IncidentTable. Broadcasts updates to all subscribed
WebSocket connections for the incident's tenant.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

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


def deserialize_dynamodb_image(image: Dict[str, Any]) -> Dict[str, Any]:
    """Convert DynamoDB Stream image to standard Python dict."""
    return {k: _deserializer.deserialize(v) for k, v in image.items()}


def get_tenant_connections(tenant_id: str) -> List[str]:
    """Fetch active connection IDs subscribed to this tenant."""
    if not ROUTING_TABLE_NAME:
        return []

    table = get_dynamodb().Table(ROUTING_TABLE_NAME)
    try:
        response = table.query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": f"WS_SUB#{tenant_id}"},
        )
        return [item["connection_id"] for item in response.get("Items", []) if "connection_id" in item]
    except Exception as exc:
        logger.error("Failed to query WebSocket connections: %s", exc)
        return []


def handler(event: Dict[str, Any], context: Any) -> None:
    """Process DynamoDB stream records and broadcast to WebSocket clients."""
    records = event.get("Records", [])
    logger.info("Processing %d DynamoDB stream records", len(records))

    apigw = get_apigw()
    if not apigw:
        logger.warning("WEBSOCKET_ENDPOINT not configured, skipping broadcast")
        return

    table = get_dynamodb().Table(ROUTING_TABLE_NAME) if ROUTING_TABLE_NAME else None

    for record in records:
        event_name = record.get("eventName")  # INSERT, MODIFY, REMOVE
        new_image_raw = record.get("dynamodb", {}).get("NewImage")

        if not new_image_raw:
            continue

        item = deserialize_dynamodb_image(new_image_raw)
        tenant_id = item.get("tenant_id") or item.get("pk", "").replace("TENANT#", "")
        incident_id = item.get("incident_id") or item.get("sk", "").replace("INCIDENT#", "")

        if not tenant_id:
            continue

        broadcast_payload = json.dumps(
            {
                "type": "INCIDENT_UPDATE",
                "eventName": event_name,
                "tenant_id": tenant_id,
                "incident_id": incident_id,
                "status": item.get("status"),
                "timeline": item.get("timeline", []),
                "evidence_analysis": item.get("evidence_analysis"),
                "updated_at": item.get("updated_at"),
            }
        ).encode("utf-8")

        connection_ids = get_tenant_connections(tenant_id)
        for conn_id in connection_ids:
            try:
                apigw.post_to_connection(ConnectionId=conn_id, Data=broadcast_payload)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "GoneException":
                    logger.info("Pruning disconnected client %s", conn_id)
                    if table:
                        table.delete_item(Key={"pk": f"WS_SUB#{tenant_id}", "sk": f"CONN#{conn_id}"})
                else:
                    logger.error("Failed to push to connection %s: %s", conn_id, exc)
