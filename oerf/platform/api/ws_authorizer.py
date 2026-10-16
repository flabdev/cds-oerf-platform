"""WebSocket API Lambda Authorizer for live dashboard connections (ADR-010, ADR-011).

API Gateway supports a Lambda authorizer only on the ``$connect`` route of a
WebSocket API, so this function is the single place where a connection is
authenticated. It verifies the Cognito access token supplied as the ``token``
query-string parameter (browsers cannot set headers on a WebSocket handshake),
resolves the tenant and role from the verified user, and records the
connection-to-tenant binding that ``ws_handler`` enforces on subscribe.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import time
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from oerf.platform.api.claims import (
    GROUPS_CLAIM,
    PLATFORM_ADMIN_GROUP,
    TENANT_CLAIM,
    parse_cognito_groups,
    resolve_role,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

ROUTING_TABLE_NAME = os.environ.get("ROUTING_TABLE_NAME", "")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
USER_POOL_CLIENT_ID = os.environ.get("USER_POOL_CLIENT_ID", "")

CONNECTION_TTL_SECONDS = 86400  # 24-hour TTL, matches the connection handler

# API Gateway WebSocket $connect authorizers are REQUEST authorizers: they must
# answer with an IAM policy document. The HTTP API "simple response" shape
# (``isAuthenticated``) is not supported here and would be rejected.
POLICY_VERSION = "2012-10-17"
INVOKE_ACTION = "execute-api:Invoke"
ANONYMOUS_PRINCIPAL = "anonymous"


def policy_resource(event: dict[str, Any]) -> str:
    """Return the route ARN the returned IAM policy applies to."""
    return str(event.get("methodArn") or "")


def build_policy(effect: str, principal_id: str, resource: str) -> dict[str, Any]:
    """Build the IAM policy response required by a WebSocket authorizer."""
    return {
        "principalId": principal_id or ANONYMOUS_PRINCIPAL,
        "policyDocument": {
            "Version": POLICY_VERSION,
            "Statement": [
                {"Action": INVOKE_ACTION, "Effect": effect, "Resource": resource}
            ],
        },
    }


def deny_connection(event: dict[str, Any], principal_id: str = "") -> dict[str, Any]:
    """Deny the connection so API Gateway refuses the handshake with HTTP 401."""
    return build_policy("Deny", principal_id, policy_resource(event))


def allow_connection(
    event: dict[str, Any],
    principal_id: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Allow the connection and hand the trusted tenant context to the handler."""
    response = build_policy("Allow", principal_id, policy_resource(event))
    response["context"] = context
    return response

_dynamodb = None
_cognito = None


def get_dynamodb():
    """Lazily create the DynamoDB resource."""
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def get_cognito():
    """Lazily create the Cognito Identity Provider client."""
    global _cognito
    if _cognito is None:
        _cognito = boto3.client("cognito-idp")
    return _cognito


def extract_token(event: dict[str, Any]) -> str:
    """Read the connection token from the request identity sources."""
    identity_source = event.get("identitySource")
    candidates: list[Any] = []
    if isinstance(identity_source, str):
        candidates.append(identity_source)
    elif isinstance(identity_source, (list, tuple)):
        candidates.extend(identity_source)

    candidates.append(event.get("token"))
    candidates.append((event.get("queryStringParameters") or {}).get("token"))

    headers = {str(key).lower(): value for key, value in (event.get("headers") or {}).items()}
    authorization = headers.get("authorization")
    if isinstance(authorization, str) and authorization.lower().startswith("bearer "):
        # Strip the "Bearer " prefix (7 characters).
        candidates.append(authorization[7:])

    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def decode_claims(token: str) -> dict[str, Any]:
    """Decode the JWT payload, which is trusted only after ``verify_token`` succeeds."""
    try:
        payload = token.split(".")[1]
    except IndexError:
        return {}
    try:
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, ValueError, TypeError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def get_user_attributes(user: dict[str, Any]) -> dict[str, str]:
    """Flatten the Cognito ``UserAttributes`` list into a name/value mapping."""
    attributes: dict[str, str] = {}
    for attribute in user.get("UserAttributes") or []:
        name = attribute.get("Name")
        if isinstance(name, str) and name:
            attributes[name] = attribute.get("Value") or ""
    return attributes


def verify_token(token: str) -> dict[str, Any]:
    """Return the Cognito user for ``token``, or an empty mapping when it is invalid."""
    if not token or not USER_POOL_ID:
        return {}
    try:
        return get_cognito().get_user(AccessToken=token)
    except (ClientError, BotoCoreError) as exc:
        logger.warning("WebSocket token rejected by Cognito: %s", exc)
        return {}


def bind_connection(connection_id: str, tenant_id: str, role: str, sub: str) -> bool:
    """Record the connection-to-tenant binding that subscriptions are scoped to."""
    if not ROUTING_TABLE_NAME:
        logger.warning("ROUTING_TABLE_NAME not configured, connection binding skipped")
        return True
    if not connection_id:
        return False

    now = int(time.time())
    try:
        get_dynamodb().Table(ROUTING_TABLE_NAME).put_item(
            Item={
                "pk": f"WS#{connection_id}",
                "sk": "METADATA",
                "connection_id": connection_id,
                "tenant_id": tenant_id,
                "role": role,
                "sub": sub,
                "connected_at": now,
                "ttl": now + CONNECTION_TTL_SECONDS,
            }
        )
    except Exception as exc:
        logger.error(
            "Failed to bind connection %s to tenant %s: %s", connection_id, tenant_id, exc
        )
        return False
    return True


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Authorize a WebSocket ``$connect`` request and bind it to its tenant."""
    request_context = event.get("requestContext") or {}
    connection_id = str(request_context.get("connectionId") or "")

    token = extract_token(event)
    user = verify_token(token)
    if not user:
        logger.warning("Denying connection %s without a verified Cognito token", connection_id)
        return deny_connection(event)

    claims = decode_claims(token)
    client_id = claims.get("client_id")
    if USER_POOL_CLIENT_ID and client_id != USER_POOL_CLIENT_ID:
        logger.warning("Denying token issued for unexpected client '%s'", client_id)
        return deny_connection(event)

    attributes = get_user_attributes(user)
    tenant_id = attributes.get(TENANT_CLAIM) or attributes.get("tenant_id") or ""
    sub = attributes.get("sub") or str(claims.get("sub") or user.get("Username") or "")
    if not tenant_id:
        logger.warning("Denying connection %s for a user without a tenant claim", connection_id)
        return deny_connection(event, sub)

    role = resolve_role(parse_cognito_groups(claims.get(GROUPS_CLAIM)))

    if connection_id and not bind_connection(connection_id, tenant_id, role, sub):
        # When the Gateway supplies an ID, persist the binding before allowing.
        # Some $connect authorizer payloads omit it; the subsequent $connect
        # integration persists the same trusted authorizer context instead.
        logger.error("Denying connection %s: tenant binding could not be stored", connection_id)
        return deny_connection(event, sub)

    logger.info("Authorized connection %s for tenant %s as %s", connection_id, tenant_id, role)
    return allow_connection(
        event,
        sub or str(connection_id),
        {
            "tenant_id": tenant_id,
            "role": role,
            "is_platform_admin": "true" if role == PLATFORM_ADMIN_GROUP else "false",
            "sub": sub,
        },
    )
