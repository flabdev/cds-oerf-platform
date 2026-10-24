"""HTTP API incident reads.

GET /incidents and GET /incidents/{id} read TENANT#{tid}#INC#{iid}.
Dashboard actions are not accepted on this API. This handler does not call
states:SendTaskSuccess, states:StartExecution, or SQS. Ingest starts
executions. The REST API queues dashboard actions.
"""

from __future__ import annotations

import json
import logging
import os
import re
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


def cors_response(status_code: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "OPTIONS,GET",
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


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """HTTP API Gateway router with Cognito authorization and tenant isolation."""
    http_context = event.get("requestContext", {}).get("http", {})
    http_method = event.get("httpMethod") or http_context.get("method", "GET")
    path = event.get("path") or event.get("rawPath", "")
    log(logger, logging.INFO, f"{http_method} {path}", outcome="received")

    if http_method == "OPTIONS":
        return cors_response(200, {})

    path_params = event.get("pathParameters") or {}

    auth = extract_auth_context(event)
    normalized = path.split("?")[0].rstrip("/") or "/"

    if not auth["is_authenticated"]:
        return cors_response(401, {"error": "unauthorized"})
    if not auth["tenant_id"]:
        return cors_response(403, {"error": "tenant_required"})
    tenant_id = str(auth["tenant_id"])

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
