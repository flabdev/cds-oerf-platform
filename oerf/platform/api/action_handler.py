"""HTTP API for dashboard actions and incident reads.

POST /incidents/{id}/action queues the inbound message from the interface
contract. GET reads use TENANT#{tid}#INC#{iid}. This handler does not call
states:SendTaskSuccess.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from datetime import UTC, datetime
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
STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN", "")
INBOUND_QUEUE_URL = os.environ.get("INBOUND_QUEUE_URL") or os.environ.get("ACTION_QUEUE_URL", "")

_ACTIONS = frozenset({"confirm_transfer", "pick_option", "cancel"})
_ACTION_FIELDS = frozenset({"action", "action_id", "engagement_id", "option_id", "reason"})
_ACTION_ROLES = frozenset({"platform_admin", "tenant_admin", "ops_user"})
_NEEDS_ENGAGEMENT = frozenset({"confirm_transfer", "pick_option"})

_dynamodb = None
_sfn = None
_sqs = None


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


def get_sqs():
    global _sqs
    if _sqs is None:
        _sqs = boto3.client("sqs")
    return _sqs


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


def handle_dashboard_action(
    tenant_id: str,
    incident_id: str,
    body: dict[str, Any],
    user_id: str | None,
) -> dict[str, Any]:
    """Queue one dashboard action. The inbound handler resumes the workflow."""

    parsed = _action_body(body)
    if parsed is None:
        return cors_response(400, {"error": "invalid_body"})
    if not INBOUND_QUEUE_URL:
        return cors_response(503, {"error": "queue_unavailable"})
    message = {
        "tenant_id": tenant_id,
        "incident_id": incident_id,
        "engagement_id": parsed["engagement_id"],
        "channel": "dashboard",
        "provider_msg_id": None,
        "context_msg_id": None,
        "action_id": parsed["action_id"],
        "action": parsed["action"],
        "option_id": parsed["option_id"],
        "reason": parsed["reason"],
        "user_id": user_id,
        "type": "text",
        "text": None,
        "media_ref": None,
        "received_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        params: dict[str, Any] = {
            "QueueUrl": INBOUND_QUEUE_URL,
            "MessageBody": json.dumps(message),
        }
        if INBOUND_QUEUE_URL.endswith(".fifo"):
            params["MessageDeduplicationId"] = parsed["action_id"]
            params["MessageGroupId"] = tenant_id
        get_sqs().send_message(**params)
    except Exception as exc:
        logger.error("Failed to queue action %s: %s", parsed["action_id"], exc)
        return cors_response(503, {"error": "queue_unavailable"})
    return cors_response(202, {"action_id": parsed["action_id"], "status": "queued"})


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

    auth = extract_auth_context(event)
    normalized = path.split("?")[0].rstrip("/") or "/"

    if normalized.endswith("/incidents/trigger") and http_method == "POST":
        tenant_id = _trigger_tenant(auth, body, query_params)
        return handle_trigger_incident(tenant_id, body)

    if not auth["is_authenticated"]:
        return cors_response(401, {"error": "unauthorized"})
    if not auth["tenant_id"]:
        return cors_response(403, {"error": "tenant_required"})
    tenant_id = str(auth["tenant_id"])

    action_match = re.fullmatch(r"(?:/[^/]+)?/incidents/([^/]+)/actions?", normalized)
    if http_method == "POST" and action_match:
        if auth["role"] not in _ACTION_ROLES:
            return cors_response(403, {"error": "forbidden"})
        return handle_dashboard_action(
            tenant_id, action_match.group(1), body, auth.get("sub")
        )

    if http_method == "GET" and (
        normalized.endswith("/incidents") or re.fullmatch(r"(?:/[^/]+)?/incidents", normalized)
    ):
        return _list_incidents(tenant_id)

    if http_method == "GET" and path_params.get("id"):
        return _get_incident(tenant_id, str(path_params["id"]))

    return cors_response(404, {"error": "Route not found", "path": path})


def _trigger_tenant(auth: dict[str, Any], body: dict[str, Any], query: dict[str, Any]) -> str:
    if auth["is_authenticated"] and auth["tenant_id"] and not auth["is_platform_admin"]:
        return str(auth["tenant_id"])
    requested = body.get("tenant_id") or query.get("tenant_id")
    if auth["is_platform_admin"] and requested:
        return str(requested)
    if auth["tenant_id"]:
        return str(auth["tenant_id"])
    return str(requested or "T1")


def _list_incidents(tenant_id: str) -> dict[str, Any]:
    if not INCIDENT_TABLE_NAME:
        return cors_response(503, {"error": "gsi_unavailable"})
    try:
        from oerf.platform.api.queries import DynamoIncidentQuery

        page = DynamoIncidentQuery(boto3.client("dynamodb"), INCIDENT_TABLE_NAME).list_incidents(
            tenant_id
        )
    except Exception as exc:
        logger.error("Incident list failed: %s", exc)
        return cors_response(503, {"error": "gsi_unavailable"})
    return cors_response(200, page)


def _get_incident(tenant_id: str, incident_id: str) -> dict[str, Any]:
    if not INCIDENT_TABLE_NAME:
        return cors_response(404, {"error": "not_found"})
    try:
        from oerf.platform.api.queries import DynamoIncidentQuery

        item = DynamoIncidentQuery(boto3.client("dynamodb"), INCIDENT_TABLE_NAME).get_incident(
            tenant_id, incident_id
        )
    except Exception as exc:
        logger.error("Incident read failed: %s", exc)
        return cors_response(404, {"error": "not_found"})
    if item is None:
        return cors_response(404, {"error": "not_found"})
    return cors_response(200, item)


def _action_body(body: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(body, dict) or set(body) - _ACTION_FIELDS:
        return None
    action = body.get("action")
    action_id = body.get("action_id")
    if action not in _ACTIONS or not isinstance(action_id, str) or not action_id.strip():
        return None
    engagement = body.get("engagement_id")
    if action in _NEEDS_ENGAGEMENT and not _text(engagement):
        return None
    option = body.get("option_id")
    if action == "pick_option" and not _text(option):
        return None
    if option is not None and not isinstance(option, str):
        return None
    if "reason" in body and not isinstance(body.get("reason"), str):
        return None
    reason = body.get("reason")
    return {
        "action": action,
        "action_id": action_id.strip(),
        "engagement_id": engagement.strip() if _text(engagement) else None,
        "option_id": option.strip() if _text(option) else None,
        "reason": reason.strip()[:4000] if isinstance(reason, str) and reason.strip() else None,
    }


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
