"""Copy ``custom:tenant_id`` and the user's groups onto Cognito tokens.

Cognito access tokens omit custom attributes unless a pre-token-generation
trigger (V2) adds them. The claim name matches ``oerf.platform.api.claims``.
``cognito:groups`` is not a ``claimsToAddOrOverride`` entry. V2 writes that
claim on the ID token and the access token from ``groupOverrideDetails``.
"""

from __future__ import annotations

from typing import Any

TENANT_CLAIM = "custom:tenant_id"
_TOKEN_SECTIONS = ("idTokenGeneration", "accessTokenGeneration")


def apply_tenant_claim(event: dict[str, Any]) -> dict[str, Any]:
    """Add the tenant claim to both tokens and the groups to the ID token.

    ``groupOverrideDetails`` replaces ``cognito:groups`` on the ID token and
    the access token. An empty override would suppress the groups, so a user
    with no groups leaves that object unset.
    """

    attributes = event.get("request", {}).get("userAttributes") or {}
    raw = attributes.get(TENANT_CLAIM)
    tenant_id = raw.strip() if isinstance(raw, str) and raw.strip() else None
    groups = _group_override(event)
    if tenant_id is None and groups is None:
        return event
    response = event.setdefault("response", {})
    details = dict(response.get("claimsAndScopeOverrideDetails") or {})
    if tenant_id is not None:
        for section in _TOKEN_SECTIONS:
            generation = dict(details.get(section) or {})
            claims = dict(generation.get("claimsToAddOrOverride") or {})
            claims[TENANT_CLAIM] = tenant_id
            generation["claimsToAddOrOverride"] = claims
            details[section] = generation
    if groups is not None:
        details["groupOverrideDetails"] = groups
    response["claimsAndScopeOverrideDetails"] = details
    return event


def _group_override(event: dict[str, Any]) -> dict[str, Any] | None:
    """The request's groups, in the shape Cognito writes onto the ID token."""

    request = event.get("request")
    config = request.get("groupConfiguration") if isinstance(request, dict) else None
    if not isinstance(config, dict):
        return None
    raw = config.get("groupsToOverride")
    if not isinstance(raw, list):
        return None
    groups = [name.strip() for name in raw if isinstance(name, str) and name.strip()]
    if not groups:
        return None
    override: dict[str, Any] = {"groupsToOverride": groups}
    roles = config.get("iamRolesToOverride")
    if isinstance(roles, list):
        override["iamRolesToOverride"] = [
            role.strip() for role in roles if isinstance(role, str) and role.strip()
        ]
    preferred = config.get("preferredRole")
    if isinstance(preferred, str) and preferred.strip():
        override["preferredRole"] = preferred.strip()
    return override


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Cognito pre-token-generation entry point."""
    return apply_tenant_claim(event)
