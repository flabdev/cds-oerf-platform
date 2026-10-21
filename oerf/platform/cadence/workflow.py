"""Step Functions payload helpers for the nested engagement workflow.

The cadence engine remains pure. These adapters turn its decision into the
bounded task timeout required by ``engage_party_v1`` and calculate a callback
pause without moving the original exhaustion deadline.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from oerf.platform.cadence.calendar import SendingCalendar
from oerf.platform.cadence.engine import RungDecision, SentRung, compute_next_rung
from oerf.platform.cadence.policy import CadencePolicy, parse_duration


def plan_next_rung(
    policy: CadencePolicy,
    history: list[SentRung],
    now: datetime,
    calendar: SendingCalendar,
    *,
    multiplier: int = 1,
    engagement_started_at: datetime | None = None,
    paused_until: datetime | None = None,
) -> dict[str, Any]:
    """Return a JSON-ready cadence decision with a timeout bounded by deadline.

    Quiet hours and callback pauses may move a rung's eligible time past the
    policy deadline. In that case the engagement is exhausted rather than
    creating a task-token wait which can outlive its original deadline.
    """
    decision = compute_next_rung(
        policy,
        history,
        now,
        calendar,
        multiplier=multiplier,
        engagement_started_at=engagement_started_at,
        paused_until=paused_until,
    )
    if decision.exhausted:
        payload = _json_decision(decision)
        if decision.final_question:
            remaining = max(0, math.ceil((decision.deadline - now).total_seconds() / multiplier))
            payload["final_question_timeout_seconds"] = max(1, min(remaining, 86400))
        return payload

    assert decision.next_allowed_at is not None
    due = decision.next_allowed_at
    delay = max(0, math.ceil((due - now).total_seconds() / multiplier))
    if due >= decision.deadline:
        return _json_decision(_exhausted_decision(policy, decision))

    next_history = [*history, SentRung(rung=decision.next_rung or 1, sent_at=due)]
    following = compute_next_rung(
        policy,
        next_history,
        due,
        calendar,
        multiplier=multiplier,
        engagement_started_at=engagement_started_at or (history[0].sent_at if history else now),
        paused_until=paused_until,
    )
    wait_until = decision.deadline
    if not following.exhausted and following.next_allowed_at is not None:
        wait_until = min(wait_until, following.next_allowed_at)
    timeout = max(1, math.ceil((wait_until - due).total_seconds() / multiplier))

    payload = _json_decision(decision)
    payload["delay_seconds"] = delay
    payload["timeout_seconds"] = timeout
    payload["due_at"] = due.isoformat()
    return payload


def _exhausted_decision(policy: CadencePolicy, decision: RungDecision) -> RungDecision:
    return decision.model_copy(
        update={
            "next_rung": None,
            "channel": None,
            "action": None,
            "message": None,
            "wait": None,
            "real_seconds": None,
            "next_allowed_at": None,
            "exhausted": True,
            "exhaust_action": policy.exhaust_action,
            "paused_until": None,
            "awaits_reply": False,
            "reply_channel": None,
            "to_role": None,
        }
    )


def _json_decision(decision: RungDecision) -> dict[str, Any]:
    """Produce Step Functions-compatible JSON without serializing unset values."""
    result = decision.model_dump(mode="json", exclude_none=True)
    result.setdefault("timeout_seconds", 0)
    result.setdefault("delay_seconds", 0)
    return result


def callback_pause(
    *,
    role: str,
    callback_roles: dict[str, str],
    callback_wait: str,
    now: datetime,
    deadline: datetime,
) -> dict[str, Any]:
    """Map CALL ME to a callback role and a pause capped at exhaustion.

    The result is a pause instruction only; callers must preserve ``deadline``
    and pass ``paused_until`` to the next cadence calculation.
    """
    callback_role = callback_roles.get(role)
    if not callback_role:
        raise ValueError(f"no callback role configured for {role!r}")
    wait = parse_duration(callback_wait)
    pause_until = min(now + wait, deadline)
    pause = max(0, math.ceil((pause_until - now).total_seconds()))
    return {
        "callback_role": callback_role,
        "paused_until": pause_until.isoformat(),
        "deadline": deadline.isoformat(),
        "timeout_seconds": pause,
    }
