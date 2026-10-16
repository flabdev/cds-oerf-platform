"""Helpers for interpreting Cognito claims supplied by API Gateway authorizers."""

from __future__ import annotations

import json
from typing import Any

PLATFORM_ADMIN_GROUP = "platform_admin"
DEFAULT_ROLE = "ops_user"
GROUPS_CLAIM = "cognito:groups"
TENANT_CLAIM = "custom:tenant_id"


def parse_cognito_groups(raw: Any) -> list[str]:
    """Normalise the ``cognito:groups`` claim into a list of group names.

    API Gateway JWT and Lambda authorizers surface the claim as a string.
    Depending on how the token was issued, that string is a plain group name
    (``ops_user``), a bracketed list (``[ops_user, tenant_admin]``) or a
    JSON-encoded list (``["ops_user","tenant_admin"]``), so every shape is
    handled before the role is resolved.
    """
    if raw is None or isinstance(raw, bool):
        return []
    if isinstance(raw, (list, tuple, set)):
        return [text for group in raw if (text := str(group).strip())]
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            text = text[:-1] if text.endswith("]") else text
            text = text[1:]
        else:
            if isinstance(parsed, list):
                return [text for group in parsed if (text := str(group).strip())]
            text = str(parsed)
    return [group.strip() for group in text.split(",") if group.strip()]


def resolve_role(groups: list[str]) -> str:
    """Return the effective operator role implied by a set of Cognito groups."""
    if PLATFORM_ADMIN_GROUP in groups:
        return PLATFORM_ADMIN_GROUP
    return groups[0] if groups else DEFAULT_ROLE
