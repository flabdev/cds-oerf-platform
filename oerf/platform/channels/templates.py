"""Render a domain message template. Missing facts are a validation error."""

from __future__ import annotations

import re
from typing import Any

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class TemplateError(ValueError):
    """The template library cannot render this message."""


class MissingTemplate(TemplateError):
    """This channel has no body for the requested template."""


class MissingFacts(TemplateError):
    """The template names facts the caller did not supply."""


def render_message(
    messages: dict[str, Any], template_id: str, channel: str, facts: dict[str, Any]
) -> str:
    """Fill one channel body. Extra facts are ignored. Missing facts raise."""

    library = messages.get("messages", messages)
    entry = library.get(template_id) if isinstance(library, dict) else None
    body = entry.get(channel) if isinstance(entry, dict) else None
    if not isinstance(body, str) or not body.strip():
        raise MissingTemplate(f"template {template_id!r} has no {channel} body")
    names = set(_PLACEHOLDER.findall(body))
    missing = sorted(name for name in names if name not in facts)
    if missing:
        joined = ", ".join(missing)
        raise MissingFacts(f"template {template_id!r} is missing facts: {joined}")
    values = {name: facts[name] for name in names}
    return body.format(**values)


def parameter_values(
    messages: dict[str, Any], template_id: str, channel: str, facts: dict[str, Any]
) -> list[str]:
    """Fact values in the order the channel body names them.

    Live WhatsApp sends these as template parameters ``{{1}}``, ``{{2}}``, and
    so on. The mocked send renders the same body instead.
    """

    library = messages.get("messages", messages)
    entry = library.get(template_id) if isinstance(library, dict) else None
    body = entry.get(channel) if isinstance(entry, dict) else None
    if not isinstance(body, str):
        return []
    return [str(facts[name]) for name in _PLACEHOLDER.findall(body)]
