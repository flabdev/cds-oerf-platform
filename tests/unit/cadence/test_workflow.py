"""Step Functions cadence adapter contract."""

from datetime import UTC, datetime, timedelta

import pytest

from oerf.platform.cadence.calendar import SendingCalendar
from oerf.platform.cadence.engine import SentRung
from oerf.platform.cadence.policy import CadencePolicy, CadenceRung
from oerf.platform.cadence.workflow import callback_pause, plan_next_rung

START = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def _policy() -> CadencePolicy:
    return CadencePolicy(
        policy_id="test",
        urgency="normal",
        respect_quiet_hours=False,
        exhaust_after="PT10M",
        exhaust_action={"type": "escalate"},
        rungs=[
            CadenceRung(
                after="PT0S",
                anchor="engagement_start",
                action="send",
                channel="whatsapp",
                message="first",
            ),
            CadenceRung(
                after="PT5M",
                anchor="engagement_start",
                action="send",
                channel="sms",
                message="reminder",
            ),
        ],
    )


def test_plan_emits_due_wait_and_wait_bounded_by_next_rung() -> None:
    plan = plan_next_rung(_policy(), [], START, SendingCalendar(), engagement_started_at=START)

    assert plan["next_rung"] == 1
    assert plan["delay_seconds"] == 0
    assert plan["timeout_seconds"] == 300
    assert plan["deadline"] == "2026-10-20T09:10:00Z"


def test_callback_pause_uses_configured_role_and_cannot_extend_deadline() -> None:
    deadline = START + timedelta(minutes=2)
    result = callback_pause(
        role="ops_manager",
        callback_roles={"ops_manager": "ops_director"},
        callback_wait="PT5M",
        now=START,
        deadline=deadline,
    )

    assert result["callback_role"] == "ops_director"
    assert result["paused_until"] == deadline.isoformat()
    assert result["timeout_seconds"] == 120
    assert result["deadline"] == deadline.isoformat()


def test_callback_requires_a_configured_role() -> None:
    with pytest.raises(ValueError, match="no callback role"):
        callback_pause(
            role="unknown",
            callback_roles={},
            callback_wait="PT5M",
            now=START,
            deadline=START + timedelta(minutes=10),
        )


def test_next_rung_plan_uses_prior_sent_history() -> None:
    policy = _policy()
    history = [SentRung(rung=1, sent_at=START)]
    plan = plan_next_rung(
        policy,
        history,
        START + timedelta(minutes=1),
        SendingCalendar(),
        engagement_started_at=START,
    )

    assert plan["next_rung"] == 2
    assert plan["delay_seconds"] == 240
    assert plan["channel"] == "sms"
