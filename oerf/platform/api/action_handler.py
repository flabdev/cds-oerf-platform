"""REST API Action Handler for Human Approval & Incident Ingestion (ADR-004, ADR-009).

Endpoints:
- POST /incidents/{id}/action : Submit human operator decision (Approve/Reject/Override).
- POST /incidents/trigger     : Ingest new excursion event and launch Step Functions.
- GET  /incidents/{id}        : Retrieve incident state.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Dict

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

INCIDENT_TABLE_NAME = os.environ.get("INCIDENT_TABLE_NAME", "")
ROUTING_TABLE_NAME = os.environ.get("ROUTING_TABLE_NAME", "")
STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN", "")

_dynamodb = None
_sfn = None


def get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def get_sfn():
    global _sfn
    if _sfn is None:
        _sfn = boto3.client("stepfunctions")
    return _sfn


def cors_response(status_code: int, body: Any) -> Dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "OPTIONS,POST,GET",
        },
        "body": json.dumps(body) if not isinstance(body, str) else body,
    }


def handle_trigger_incident(body: Dict[str, Any]) -> Dict[str, Any]:
    """Ingest new sensor alarm and trigger Step Functions execution."""
    tenant_id = body.get("tenant_id", "T1")
    incident_id = body.get("incident_id") or f"INC-{uuid.uuid4().hex[:8].upper()}"
    shipment_id = body.get("shipment_id", "SHP-001")
    sensor_reading = body.get("sensor_reading", {"temp_c": 12.5, "alarm": "HIGH_TEMP"})

    now = int(time.time())
    item = {
        "pk": f"TENANT#{tenant_id}",
        "sk": f"INCIDENT#{incident_id}",
        "tenant_id": tenant_id,
        "incident_id": incident_id,
        "shipment_id": shipment_id,
        "status": "OPEN",
        "created_at": now,
        "updated_at": now,
        "sensor_reading": sensor_reading,
        "timeline": [
            {"time": now, "event": "INCIDENT_CREATED", "details": f"Temperature alarm triggered: {sensor_reading}"}
        ],
    }

    if INCIDENT_TABLE_NAME:
        get_dynamodb().Table(INCIDENT_TABLE_NAME).put_item(Item=item)

    execution_arn = ""
    if STATE_MACHINE_ARN:
        try:
            sfn = get_sfn()
            resp = sfn.start_execution(
                stateMachineArn=STATE_MACHINE_ARN,
                name=f"{tenant_id}_{incident_id}_{now}",
                input=json.dumps({"tenant_id": tenant_id, "incident_id": incident_id, "shipment_id": shipment_id}),
            )
            execution_arn = resp.get("executionArn", "")
        except Exception as exc:
            logger.error("Failed to start Step Functions execution: %s", exc)

    return cors_response(201, {"incident_id": incident_id, "status": "OPEN", "execution_arn": execution_arn})


def handle_operator_action(tenant_id: str, incident_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Process human operator action (APPROVE, REJECT, OVERRIDE) and unpause Step Functions."""
    action = body.get("action", "APPROVE").upper()
    operator_role = body.get("operator_role", "ops_user")
    notes = body.get("notes", "")
    now = int(time.time())

    # Update incident timeline
    if INCIDENT_TABLE_NAME:
        table = get_dynamodb().Table(INCIDENT_TABLE_NAME)
        table.update_item(
            Key={"pk": f"TENANT#{tenant_id}", "sk": f"INCIDENT#{incident_id}"},
            UpdateExpression="SET #st = :st, updated_at = :u, timeline = list_append(if_not_exists(timeline, :empty), :tl)",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":st": f"ACTION_{action}",
                ":u": now,
                ":empty": [],
                ":tl": [{"time": now, "event": f"HUMAN_{action}", "operator": operator_role, "notes": notes}],
            },
        )

    # Resume Step Functions if task_token provided or stored in RoutingTable
    task_token = body.get("task_token")
    if not task_token and ROUTING_TABLE_NAME:
        # Check if active task token is stored in routing table
        try:
            routing_table = get_dynamodb().Table(ROUTING_TABLE_NAME)
            resp = routing_table.get_item(
                Key={"pk": f"TENANT#{tenant_id}#INCIDENT#{incident_id}", "sk": "ACTIVE_TASK_TOKEN"}
            )
            task_token = resp.get("Item", {}).get("task_token")
        except Exception as exc:
            logger.warning("Could not retrieve task token: %s", exc)

    if task_token:
        try:
            get_sfn().send_task_success(
                taskToken=task_token,
                output=json.dumps({"action": action, "operator_role": operator_role, "notes": notes}),
            )
            logger.info("Sent task success for incident %s", incident_id)
        except Exception as exc:
            logger.error("Failed to send task success: %s", exc)

    return cors_response(200, {"success": True, "action": action, "incident_id": incident_id})


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """HTTP API Gateway router."""
    http_method = event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("path") or event.get("rawPath", "")
    logger.info("%s %s", http_method, path)

    if http_method == "OPTIONS":
        return cors_response(200, {})

    try:
        body = json.loads(event.get("body", "{}")) if event.get("body") else {}
    except Exception:
        body = {}

    path_params = event.get("pathParameters") or {}
    tenant_id = body.get("tenant_id") or event.get("queryStringParameters", {}).get("tenant_id", "T1")

    # Route: POST /incidents/trigger
    if path.endswith("/incidents/trigger") and http_method == "POST":
        return handle_trigger_incident(body)

    # Route: POST /incidents/{id}/action
    if "/action" in path and http_method == "POST":
        incident_id = path_params.get("id") or path.split("/")[-2]
        return handle_operator_action(tenant_id, incident_id, body)

    # Route: GET /incidents/{id}
    if http_method == "GET" and path_params.get("id"):
        incident_id = path_params["id"]
        if INCIDENT_TABLE_NAME:
            table = get_dynamodb().Table(INCIDENT_TABLE_NAME)
            resp = table.get_item(Key={"pk": f"TENANT#{tenant_id}", "sk": f"INCIDENT#{incident_id}"})
            item = resp.get("Item")
            if item:
                return cors_response(200, item)
        return cors_response(404, {"error": "Incident not found"})

    return cors_response(404, {"error": "Route not found", "path": path})
