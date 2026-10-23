"""HTTP API for dashboard actions and incident reads.

POST /incidents/{id}/action queues the inbound message from the interface
contract. GET reads use TENANT#{tid}#INC#{iid}. This handler does not call
states:SendTaskSuccess or states:StartExecution. Ingest starts executions.
"""

from __future__ import annotations

import json
import logging
import os
import re
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
from oerf.platform.observability.logging import get_logger, log

logger = get_logger(__name__)
logger.setLevel(logging.INFO)

INCIDENT_TABLE_NAME = os.environ.get("INCIDENT_TABLE_NAME", "")
INBOUND_QUEUE_URL = os.environ.get("INBOUND_QUEUE_URL") or os.environ.get("ACTION_QUEUE_URL", "")

_ACTIONS = frozenset({"confirm_transfer", "pick_option", "cancel"})
_ACTION_FIELDS = frozenset({"action", "action_id", "engagement_id", "option_id", "reason"})
_ACTION_ROLES = frozenset({"platform_admin", "tenant_admin", "ops_user"})
_NEEDS_ENGAGEMENT = frozenset({"confirm_transfer", "pick_option"})

_sqs = None


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


def handle_dashboard_action(
    tenant_id: str,
    incident_id: str,
    body: dict[str, Any],
    user_id: str | None,
    role: str,
) -> dict[str, Any]:
    """Queue one dashboard action. Cancel is finished by the inbound handler."""

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
        "user_id": user_id,
        "role": role,
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
    except Exception:
        log(
            logger,
            logging.ERROR,
            f"Failed to queue action {parsed['action_id']}",
            tenant_id=tenant_id,
            incident_id=incident_id,
            outcome="error",
            count=1,
        )
        return cors_response(503, {"error": "queue_unavailable"})
    return cors_response(202, {"action_id": parsed["action_id"], "status": "queued"})


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """HTTP API Gateway router with Cognito authorization and tenant isolation."""
    http_context = event.get("requestContext", {}).get("http", {})
    http_method = event.get("httpMethod") or http_context.get("method", "GET")
    path = event.get("path") or event.get("rawPath", "")
    log(logger, logging.INFO, f"{http_method} {path}", outcome="received")

    if http_method == "OPTIONS":
        return cors_response(200, {})

    try:
        body = json.loads(event.get("body", "{}")) if event.get("body") else {}
    except Exception:
        body = {}

    path_params = event.get("pathParameters") or {}

    auth = extract_auth_context(event)
    normalized = path.split("?")[0].rstrip("/") or "/"

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
            tenant_id, action_match.group(1), body, auth.get("sub"), str(auth["role"])
        )

    if http_method == "GET" and (
        normalized.endswith("/incidents") or re.fullmatch(r"(?:/[^/]+)?/incidents", normalized)
    ):
        return _list_incidents(tenant_id)

    if http_method == "GET" and path_params.get("id"):
        return _get_incident(tenant_id, str(path_params["id"]))

    return cors_response(404, {"error": "Route not found", "path": path})


def _list_incidents(tenant_id: str) -> dict[str, Any]:
    if not INCIDENT_TABLE_NAME:
        return cors_response(503, {"error": "gsi_unavailable"})
    try:
        from oerf.platform.api.queries import DynamoIncidentQuery

        page = DynamoIncidentQuery(boto3.client("dynamodb"), INCIDENT_TABLE_NAME).list_incidents(
            tenant_id
        )
    except Exception:
        log(
            logger,
            logging.ERROR,
            "Incident list failed",
            tenant_id=tenant_id,
            outcome="error",
            count=1,
        )
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
    except Exception:
        log(
            logger,
            logging.ERROR,
            "Incident read failed",
            tenant_id=tenant_id,
            incident_id=incident_id,
            outcome="error",
            count=1,
        )
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
    return {
        "action": action,
        "action_id": action_id.strip(),
        "engagement_id": engagement.strip() if _text(engagement) else None,
        "option_id": option.strip() if _text(option) else None,
    }


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
