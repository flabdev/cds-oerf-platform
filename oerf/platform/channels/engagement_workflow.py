"""Application boundary for one cadence rung in ``engage_party_v1``.

The orchestration state machine supplies the task token and selected rung. This
service persists/replaces the current wait before calling the channel router;
it never calls a provider directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from oerf.platform.channels.engagement import Engagement, EngagementStore
from oerf.platform.channels.providers.base import MessageProvider
from oerf.platform.channels.registry import PhoneRegistry
from oerf.platform.channels.router import (
    DeliveryResult,
    OutboundRequest,
    route_outbound,
)


@dataclass(frozen=True)
class AlreadyAnswered:
    """The engagement already has a reply, so this rung must not be sent."""

    reply: dict[str, Any]
    status: str = "already_answered"


@dataclass(frozen=True)
class PendingSend:
    """No stored answer. The caller still has to open the task-token wait."""

    status: str = "pending"


def send_rung(
    request: OutboundRequest,
    *,
    ask: str,
    policy_id: str,
    task_token: str,
    rung: int,
    deadline: datetime,
    engagements: EngagementStore,
    registry: PhoneRegistry,
    providers: dict[str, MessageProvider],
    messages: dict[str, Any],
    response_intent: str | None = None,
) -> DeliveryResult | AlreadyAnswered | PendingSend:
    """Persist the current token/rung, then deliver through the sole router.

    A retry of an existing rung uses ``prepare_wait`` to replace a stale token;
    a retry of the first opening reuses the stored engagement and does not
    replace its token. Correlation is created by ``route_outbound`` only after
    the provider successfully delivers.

    An ``ANSWERED`` row that already holds a reply, and whose intent is not
    ``clarify``, is not sent. The caller routes that stored reply to Interpret.
    Any other non-``WAITING`` status still raises. An empty token does not send:
    it only reports whether a stored answer is waiting.
    """
    token = task_token.strip()
    current = engagements.get(
        request.tenant_id, request.incident_id, request.engagement_id
    )
    if current is not None and current.status == "ANSWERED" and response_intent != "clarify":
        recorded = _recorded_reply(current)
        if recorded is not None:
            return AlreadyAnswered(reply=recorded)
        raise ValueError("only a WAITING engagement can send a cadence rung")
    if not token:
        if current is not None and current.status not in {"WAITING", "ANSWERED"}:
            raise ValueError("only a WAITING engagement can send a cadence rung")
        return PendingSend()
    if current is None:
        engagements.open_engagement(
            request.tenant_id,
            request.incident_id,
            request.engagement_id,
            request.role,
            ask,
            policy_id,
            token,
            request.facts,
            contact_id=request.contact_id,
            rung=rung,
            deadline=deadline,
        )
    else:
        if current.status == "ANSWERED":
            current = engagements.reopen_for_clarify(
                request.tenant_id, request.incident_id, request.engagement_id
            )
        if current is None or current.status != "WAITING":
            raise ValueError("only a WAITING engagement can send a cadence rung")
        if current.rung != rung or current.task_token != token:
            engagements.prepare_wait(
                request.tenant_id,
                request.incident_id,
                request.engagement_id,
                token,
                rung=rung,
                deadline=deadline,
            )

    return route_outbound(
        request,
        registry=registry,
        engagements=engagements,
        providers=providers,
        messages=messages,
    )


def _recorded_reply(engagement: Engagement) -> dict[str, Any] | None:
    """The sanitized reply object, or None when the row has no reply text."""

    reply = engagement.reply
    if not isinstance(reply, dict):
        return None
    text = reply.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return reply
