"""Pure cadence step. The caller supplies the clock, the history, and the policy."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from oerf.platform.cadence.calendar import SendingCalendar
from oerf.platform.cadence.policy import CadencePolicy, DurationError, parse_duration


class SentRung(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rung: int = Field(ge=1)
    sent_at: datetime


class RungDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    next_rung: int | None
    channel: str | None
    action: str | None
    message: str | None
    wait: str | None
    real_seconds: float | None
    next_allowed_at: datetime | None
    final_question: str | None
    exhausted: bool
    exhaust_action: dict[str, str] | None
    deadline: datetime
    paused_until: datetime | None
    awaits_reply: bool
    reply_channel: str | None
    to_role: str | None = None


def compute_next_rung(
    policy: CadencePolicy,
    history: list[SentRung],
    now: datetime,
    calendar: SendingCalendar,
    *,
    multiplier: int = 1,
    engagement_started_at: datetime | None = None,
    paused_until: datetime | None = None,
) -> RungDecision:
    """Return the next rung, or exhaustion. Quiet hours and callback pause do not consume a rung."""

    if multiplier < 1:
        raise DurationError("clock multiplier must be at least 1")
    started = engagement_started_at or (history[0].sent_at if history else now)
    deadline = started + parse_duration(policy.exhaust_after)
    index = len(history)
    if index >= len(policy.rungs) or now >= deadline:
        return _exhausted(policy, deadline, paused_until)
    rung = policy.rungs[index]
    due = _due(rung.after, rung.anchor, started, history)
    if paused_until is not None and now < paused_until:
        due = max(due, paused_until)
    if policy.respect_quiet_hours and policy.urgency != "critical":
        allowed = calendar.next_allowed(due)
    else:
        allowed = due
    voice = rung.channel == "voice" or rung.action == "call"
    return RungDecision(
        next_rung=index + 1,
        channel=rung.channel,
        action=rung.action,
        message=rung.message,
        wait=rung.after,
        real_seconds=parse_duration(rung.after).total_seconds() / multiplier,
        next_allowed_at=allowed,
        final_question=policy.final_question,
        exhausted=False,
        exhaust_action=None,
        deadline=deadline,
        paused_until=paused_until if paused_until is not None and now < paused_until else None,
        awaits_reply=not voice,
        reply_channel="whatsapp" if voice else rung.channel,
        to_role=rung.to_role,
    )


def _due(
    after: str,
    anchor: str,
    started: datetime,
    history: list[SentRung],
) -> datetime:
    base = history[-1].sent_at if anchor == "previous_rung" and history else started
    return base + parse_duration(after)


def _exhausted(
    policy: CadencePolicy,
    deadline: datetime,
    paused_until: datetime | None,
) -> RungDecision:
    return RungDecision(
        next_rung=None,
        channel=None,
        action=None,
        message=None,
        wait=None,
        real_seconds=None,
        next_allowed_at=None,
        final_question=policy.final_question,
        exhausted=True,
        exhaust_action=policy.exhaust_action,
        deadline=deadline,
        paused_until=paused_until,
        awaits_reply=False,
        reply_channel=None,
    )
