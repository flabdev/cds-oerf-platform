"""Application boundary for one cadence rung in ``engage_party_v1``.

The orchestration state machine supplies the task token and selected rung. This
service persists/replaces the current wait before calling the channel router;
it never calls a provider directly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from oerf.platform.channels.engagement import EngagementStore
from oerf.platform.channels.providers.base import MessageProvider
from oerf.platform.channels.registry import PhoneRegistry
from oerf.platform.channels.router import (
    DeliveryResult,
    OutboundRequest,
    route_outbound,
)


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
) -> DeliveryResult:
    """Persist the current token/rung, then deliver through the sole router.

    A retry of an existing rung uses ``prepare_wait`` to replace a stale token;
    a retry of the first opening reuses the stored engagement and does not
    replace its token. Correlation is created by ``route_outbound`` only after
    the provider successfully delivers.
    """
    current = engagements.get(
        request.tenant_id, request.incident_id, request.engagement_id
    )
    if current is None:
        engagements.open_engagement(
            request.tenant_id,
            request.incident_id,
            request.engagement_id,
            request.role,
            ask,
            policy_id,
            task_token,
            request.facts,
            contact_id=request.contact_id,
            rung=rung,
            deadline=deadline,
        )
    elif current.status != "WAITING":
        raise ValueError("only a WAITING engagement can send a cadence rung")
    elif current.rung != rung or current.task_token != task_token:
        engagements.prepare_wait(
            request.tenant_id,
            request.incident_id,
            request.engagement_id,
            task_token,
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
