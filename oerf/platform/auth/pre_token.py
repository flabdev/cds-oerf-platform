"""Copy ``custom:tenant_id`` onto Cognito access and ID tokens.

Cognito access tokens omit custom attributes unless a pre-token-generation
trigger (V2) adds them. The claim name matches ``oerf.platform.api.claims``.
"""

from __future__ import annotations

from typing import Any

TENANT_CLAIM = "custom:tenant_id"
_TOKEN_SECTIONS = ("idTokenGeneration", "accessTokenGeneration")


def apply_tenant_claim(event: dict[str, Any]) -> dict[str, Any]:
    """Add the user's tenant claim to both tokens when the attribute is set."""
    attributes = event.get("request", {}).get("userAttributes") or {}
    raw = attributes.get(TENANT_CLAIM)
    if not isinstance(raw, str) or not raw.strip():
        return event
    tenant_id = raw.strip()
    response = event.setdefault("response", {})
    details = dict(response.get("claimsAndScopeOverrideDetails") or {})
    for section in _TOKEN_SECTIONS:
        generation = dict(details.get(section) or {})
        claims = dict(generation.get("claimsToAddOrOverride") or {})
        claims[TENANT_CLAIM] = tenant_id
        generation["claimsToAddOrOverride"] = claims
        details[section] = generation
    response["claimsAndScopeOverrideDetails"] = details
    return event


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Cognito pre-token-generation entry point."""
    return apply_tenant_claim(event)
