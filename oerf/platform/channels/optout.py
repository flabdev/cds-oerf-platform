"""Keyword opt-out. It runs before correlation and does not call a provider."""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

from oerf.platform.channels.engagement import ACTIVE_STATUSES, Engagement, EngagementStore
from oerf.platform.channels.registry import PhoneRegistry

DEFAULT_KEYWORDS = ("STOP", "UNSUBSCRIBE")


class OptOutResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stopped_ids: list[str]
    unknown: bool
    contact_ids: list[str]


class ReplyResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    engagement: Engagement | None
    stopped_ids: list[str]


def is_opt_out(text: str | None, *, extra: Iterable[str] = ()) -> bool:
    """True when the whole trimmed message is a configured opt-out keyword."""

    if text is None:
        return False
    token = " ".join(text.strip().casefold().split())
    if not token:
        return False
    keywords = {item.strip().casefold() for item in (*DEFAULT_KEYWORDS, *extra) if item.strip()}
    return token in keywords


def apply_opt_out(
    registry: PhoneRegistry,
    engagements: EngagementStore,
    e164: str,
) -> OptOutResult:
    """Set opt-in false and stop active engagements for every matching contact."""

    resolution = registry.resolve_sender(e164)
    if resolution.unknown:
        return OptOutResult(stopped_ids=[], unknown=True, contact_ids=[])
    stopped: list[str] = []
    contacts: list[str] = []
    for match in resolution.matches:
        registry.set_opt_in(match.tenant_id, match.contact_id, False)
        contacts.append(match.contact_id)
        for engagement in engagements.for_contact(match.tenant_id, match.contact_id):
            if engagement.status not in ACTIVE_STATUSES:
                continue
            engagements.stop(engagement.tenant_id, engagement.incident_id, engagement.engagement_id)
            stopped.append(engagement.engagement_id)
    return OptOutResult(stopped_ids=stopped, unknown=False, contact_ids=contacts)


def handle_reply(
    text: str | None,
    provider_msg_id: str,
    sender: str,
    *,
    engagements: EngagementStore,
    registry: PhoneRegistry,
    extra_keywords: Iterable[str] = (),
) -> ReplyResult:
    """Opt out before correlation. A keyword never resumes a waiting engagement."""

    if is_opt_out(text, extra=extra_keywords):
        result = apply_opt_out(registry, engagements, sender)
        return ReplyResult(kind="opt_out", engagement=None, stopped_ids=result.stopped_ids)
    found = engagements.correlate(provider_msg_id)
    if found is None:
        return ReplyResult(kind="ignored", engagement=None, stopped_ids=[])
    return ReplyResult(kind="correlated", engagement=found, stopped_ids=[])
