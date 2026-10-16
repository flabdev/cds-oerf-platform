"""REST API Action Handler for Human Approval & Incident Ingestion (ADR-004, ADR-009).

Endpoints:
- POST /incidents/{id}/action : Submit human operator decision (Approve/Reject/Override).
- POST /incidents/trigger     : Ingest new excursion event and launch Step Functions.
- GET  /incidents/{id}        : Retrieve incident state.

Enforces trusted authorizer claims and tenant isolation (ADR-011).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

import boto3

from oerf.platform.api.claims import (
    DEFAULT_ROLE,
    GROUPS_CLAIM,
    PLATFORM_ADMIN_GROUP,
    TENANT_CLAIM,
    parse_cognito_groups,
    resolve_role,
)

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


def cors_response(status_code: int, body: Any) -> dict[str, Any]:
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


def extract_auth_context(event: dict[str, Any]) -> dict[str, Any]:
    """Extract and validate authorizer context from API Gateway JWT claims."""
    auth_ctx = event.get("requestContext", {}).get("authorizer", {})
    jwt_claims = auth_ctx.get("jwt", {}).get("claims", {})

    if not jwt_claims:
        jwt_claims = auth_ctx.get("lambda", {})

    if not jwt_claims:
        return {
            "tenant_id": None,
            "role": DEFAULT_ROLE,
            "is_platform_admin": False,
            "is_authenticated": False,
            "sub": None,
        }

    tenant_id = jwt_claims.get(TENANT_CLAIM) or jwt_claims.get("tenant_id")
    # The authorizer forwards cognito:groups as a string claim, so it is
    # normalised before the role is resolved.
    groups = parse_cognito_groups(jwt_claims.get(GROUPS_CLAIM))
    role = resolve_role(groups)

    return {
        "tenant_id": tenant_id,
        "role": role,
        "is_platform_admin": role == PLATFORM_ADMIN_GROUP,
        "is_authenticated": True,
        "sub": jwt_claims.get("sub"),
    }


def handle_trigger_incident(tenant_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Ingest new sensor alarm and trigger Step Functions execution."""
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
            {
                "time": now,
                "event": "INCIDENT_CREATED",
                "details": f"Temperature alarm triggered: {sensor_reading}",
            }
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
                input=json.dumps(
                    {
                        "tenant_id": tenant_id,
                        "incident_id": incident_id,
                        "shipment_id": shipment_id,
                    }
                ),
            )
            execution_arn = resp.get("executionArn", "")
        except Exception as exc:
            logger.error("Failed to start Step Functions execution: %s", exc)

    return cors_response(
        201,
        {"incident_id": incident_id, "status": "OPEN", "execution_arn": execution_arn},
    )


def handle_operator_action(
    tenant_id: str,
    incident_id: str,
    body: dict[str, Any],
    operator_role: str = DEFAULT_ROLE,
) -> dict[str, Any]:
    """Process human operator action (APPROVE, REJECT, OVERRIDE) and unpause Step Functions."""
    action = body.get("action", "APPROVE").upper()
    notes = body.get("notes", "")
    now = int(time.time())

    # Update incident timeline
    if INCIDENT_TABLE_NAME:
        table = get_dynamodb().Table(INCIDENT_TABLE_NAME)
        table.update_item(
            Key={"pk": f"TENANT#{tenant_id}", "sk": f"INCIDENT#{incident_id}"},
            UpdateExpression=(
                "SET #st = :st, updated_at = :u, "
                "timeline = list_append(if_not_exists(timeline, :empty), :tl)"
            ),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":st": f"ACTION_{action}",
                ":u": now,
                ":empty": [],
                ":tl": [
                    {
                        "time": now,
                        "event": f"HUMAN_{action}",
                        "operator": operator_role,
                        "notes": notes,
                    }
                ],
            },
        )

    # Resume Step Functions if a task token was supplied or stored for this incident
    task_token = body.get("task_token")
    token_key = {
        "pk": f"TENANT#{tenant_id}#INCIDENT#{incident_id}",
        "sk": "ACTIVE_TASK_TOKEN",
    }
    stored_token = False
    routing_table = (
        get_dynamodb().Table(ROUTING_TABLE_NAME) if ROUTING_TABLE_NAME else None
    )

    if not task_token and routing_table is not None:
        try:
            item = routing_table.get_item(Key=token_key).get("Item")
        except Exception as exc:
            logger.warning("Could not retrieve task token: %s", exc)
            item = None

        if item:
            token_ttl = item.get("ttl", float("inf"))
            if now > token_ttl:
                logger.warning(
                    "Active task token for incident %s expired at %s", incident_id, token_ttl
                )
            else:
                task_token = item.get("task_token")
                stored_token = bool(task_token)

    if task_token:
        try:
            get_sfn().send_task_success(
                taskToken=task_token,
                output=json.dumps(
                    {"action": action, "operator_role": operator_role, "notes": notes}
                ),
            )
        except Exception as exc:
            # The callback is the only way to resume the workflow: report the
            # failure and keep the stored token so the action can be retried.
            logger.error("Failed to send task success for incident %s: %s", incident_id, exc)
            return cors_response(
                502,
                {
                    "success": False,
                    "error": "Failed to resume the incident workflow, please retry",
                    "incident_id": incident_id,
                    "action": action,
                },
            )

        logger.info("Sent task success for incident %s", incident_id)
        if stored_token and routing_table is not None:
            # Consume the stored token only once the callback is confirmed, so a
            # failed callback never leaves an unrecoverable workflow.
            try:
                routing_table.delete_item(Key=token_key)
            except Exception as del_err:
                logger.warning("Failed to delete consumed task token: %s", del_err)

    return cors_response(200, {"success": True, "action": action, "incident_id": incident_id})


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """HTTP API Gateway router with Cognito authorization and tenant isolation."""
    http_context = event.get("requestContext", {}).get("http", {})
    http_method = event.get("httpMethod") or http_context.get("method", "GET")
    path = event.get("path") or event.get("rawPath", "")
    logger.info("%s %s", http_method, path)

    if http_method == "OPTIONS":
        return cors_response(200, {})

    try:
        body = json.loads(event.get("body", "{}")) if event.get("body") else {}
    except Exception:
        body = {}

    path_params = event.get("pathParameters") or {}
    query_params = event.get("queryStringParameters") or {}

    # Extract authenticated context
    auth = extract_auth_context(event)
    requested_tenant = body.get("tenant_id") or query_params.get("tenant_id")

    if auth["is_authenticated"]:
        if not auth["is_platform_admin"]:
            # Normal tenant user: must match token's tenant
            auth_tenant = auth["tenant_id"]
            if not auth_tenant:
                return cors_response(
                    403,
                    {"error": "Forbidden: Authenticated token is missing a tenant claim"},
                )
            if requested_tenant and requested_tenant != auth_tenant:
                return cors_response(
                    403,
                    {
                        "error": (
                            f"Forbidden: Access to tenant '{requested_tenant}' "
                            "is not permitted for your user"
                        )
                    },
                )
            tenant_id = auth_tenant
        else:
            # Platform admin can access any specified tenant
            tenant_id = requested_tenant or auth.get("tenant_id") or "T1"
    else:
        # Fallback for unauthenticated dev/test requests
        tenant_id = requested_tenant or "T1"

    operator_role = auth.get("role", "ops_user")

    # Route: POST /incidents/trigger
    if path.endswith("/incidents/trigger") and http_method == "POST":
        return handle_trigger_incident(tenant_id, body)

    # Route: POST /incidents/{id}/action
    if "/action" in path and http_method == "POST":
        # Validate action permissions
        if auth["is_authenticated"] and operator_role not in (
            "ops_user",
            "tenant_admin",
            "platform_admin",
        ):
            return cors_response(
                403,
                {"error": "Forbidden: Insufficient permissions to perform operator action"},
            )

        incident_id = path_params.get("id") or path.split("/")[-2]
        return handle_operator_action(tenant_id, incident_id, body, operator_role=operator_role)

    # Route: GET /incidents/{id}
    if http_method == "GET" and path_params.get("id"):
        incident_id = path_params["id"]
        if INCIDENT_TABLE_NAME:
            table = get_dynamodb().Table(INCIDENT_TABLE_NAME)
            resp = table.get_item(
                Key={"pk": f"TENANT#{tenant_id}", "sk": f"INCIDENT#{incident_id}"}
            )
            item = resp.get("Item")
            if item:
                return cors_response(200, item)
        return cors_response(404, {"error": "Incident not found"})

    return cors_response(404, {"error": "Route not found", "path": path})
