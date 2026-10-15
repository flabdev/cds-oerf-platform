"""Cadence ladders, quiet hours, multiplier, and callback pause."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oerf.platform.cadence.calendar import SendingCalendar
from oerf.platform.cadence.engine import SentRung, compute_next_rung
from oerf.platform.cadence.policy import (
    CadencePolicy,
    CadenceRung,
    DurationError,
    load_policies,
    parse_duration,
    select_policy,
)


def test_example_returns_sms_with_scaled_wait() -> None:
    start = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    decision = compute_next_rung(
        _example_policy(),
        [
            SentRung(rung=1, sent_at=start),
            SentRung(rung=2, sent_at=start + timedelta(minutes=5)),
        ],
        start + timedelta(minutes=8),
        SendingCalendar(),
        multiplier=20,
        engagement_started_at=start,
    )

    assert decision.next_rung == 3
    assert decision.channel == "sms"
    assert decision.wait == "PT2M"
    assert decision.real_seconds == 6
    assert decision.exhausted is False
    json.dumps(decision.model_dump(mode="json"))


def test_configured_ladders_are_reachable() -> None:
    policies = load_policies()
    mapping = {
        "driver.diagnose": "critical_field",
        "facility.accept": "partner_request",
        "ops_manager.approve": "approval_fast",
    }
    expected = {
        "driver": ("diagnose", ["whatsapp", "whatsapp", "sms", "voice", "whatsapp"]),
        "facility": ("accept", ["whatsapp", "whatsapp", "sms", "whatsapp"]),
        "ops_manager": ("approve", ["whatsapp", "whatsapp", "email", "whatsapp"]),
    }
    start = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    for role, (ask, channels) in expected.items():
        policy = select_policy(role, ask, mapping, policies)
        history: list[SentRung] = []
        for index, channel in enumerate(channels, start=1):
            decision = compute_next_rung(
                policy,
                history,
                start,
                SendingCalendar(),
                engagement_started_at=start,
            )
            assert decision.next_rung == index
            assert decision.channel == channel
            assert decision.exhausted is False
            history.append(SentRung(rung=index, sent_at=start))
        finished = compute_next_rung(
            policy,
            history,
            start,
            SendingCalendar(),
            engagement_started_at=start,
        )
        assert finished.exhausted is True
        assert finished.exhaust_action is not None


def test_voice_does_not_wait_for_a_spoken_reply() -> None:
    policies = load_policies()
    start = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    history = [SentRung(rung=index, sent_at=start) for index in range(1, 4)]

    decision = compute_next_rung(
        policies["critical_field"],
        history,
        start,
        SendingCalendar(),
        engagement_started_at=start,
    )

    assert decision.channel == "voice"
    assert decision.awaits_reply is False
    assert decision.reply_channel == "whatsapp"


def test_quiet_hours_defer_without_dropping_the_rung() -> None:
    start = datetime(2026, 10, 20, 21, 50, tzinfo=UTC)
    calendar = SendingCalendar(
        quiet_start=datetime(2026, 1, 1, 22, 0, tzinfo=UTC).timetz().replace(tzinfo=None),
        quiet_end=datetime(2026, 1, 1, 7, 0, tzinfo=UTC).timetz().replace(tzinfo=None),
    )
    policy = _one_rung("PT30M", urgency="normal", respect_quiet_hours=True)

    decision = compute_next_rung(policy, [], start, calendar, engagement_started_at=start)
    again = compute_next_rung(policy, [], start, calendar, engagement_started_at=start)

    assert decision.next_rung == 1
    assert again.next_rung == 1
    assert decision.next_allowed_at == datetime(2026, 10, 21, 7, 0, tzinfo=UTC)


def test_critical_policy_does_not_defer_for_quiet_hours() -> None:
    start = datetime(2026, 10, 20, 21, 50, tzinfo=UTC)
    calendar = SendingCalendar(
        quiet_start=datetime(2026, 1, 1, 22, 0).time(),
        quiet_end=datetime(2026, 1, 1, 7, 0).time(),
    )
    policy = _one_rung("PT30M", urgency="critical", respect_quiet_hours=False)

    decision = compute_next_rung(policy, [], start, calendar, engagement_started_at=start)

    assert decision.next_allowed_at == start + timedelta(minutes=30)


def test_callback_pause_does_not_move_the_deadline() -> None:
    start = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    policy = _one_rung("PT0S", exhaust_after="PT2M")
    paused = compute_next_rung(
        policy,
        [],
        start + timedelta(seconds=30),
        SendingCalendar(),
        engagement_started_at=start,
        paused_until=start + timedelta(minutes=5),
    )
    running = compute_next_rung(
        policy,
        [],
        start + timedelta(seconds=30),
        SendingCalendar(),
        engagement_started_at=start,
    )

    assert paused.deadline == running.deadline
    assert paused.deadline == start + timedelta(minutes=2)
    assert paused.paused_until == start + timedelta(minutes=5)
    assert paused.next_rung == 1


@pytest.mark.parametrize("multiplier, expected", [(1, 120), (20, 6)])
def test_multiplier_scales_real_wait(multiplier: int, expected: float) -> None:
    start = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    decision = compute_next_rung(
        _one_rung("PT2M"),
        [],
        start,
        SendingCalendar(),
        multiplier=multiplier,
        engagement_started_at=start,
    )

    assert decision.real_seconds == expected


@pytest.mark.parametrize("value", ["", "PT", "-PT5M", "PT-5M", "later"])
def test_malformed_durations_fail(value: str) -> None:
    with pytest.raises(DurationError):
        parse_duration(value)


def test_engine_has_no_tenant_constants() -> None:
    source = Path("oerf/platform/cadence/engine.py").read_text(encoding="utf-8")

    assert "t1" not in source
    assert "t2" not in source


def _example_policy() -> CadencePolicy:
    return _policy(
        [
            _rung("PT0S", "whatsapp"),
            _rung("PT5M", "whatsapp"),
            _rung("PT2M", "sms"),
        ],
        exhaust_after="PT30M",
    )


def _one_rung(
    after: str,
    *,
    urgency: str = "normal",
    respect_quiet_hours: bool = False,
    exhaust_after: str = "PT30M",
) -> CadencePolicy:
    return _policy(
        [_rung(after, "whatsapp")],
        urgency=urgency,
        respect_quiet_hours=respect_quiet_hours,
        exhaust_after=exhaust_after,
    )


def _policy(
    rungs: list[CadenceRung],
    *,
    urgency: str = "critical",
    respect_quiet_hours: bool = False,
    exhaust_after: str = "PT30M",
) -> CadencePolicy:
    return CadencePolicy(
        policy_id="example",
        urgency=urgency,  # type: ignore[arg-type]
        respect_quiet_hours=respect_quiet_hours,
        exhaust_after=exhaust_after,
        exhaust_action={"type": "continue_without"},
        rungs=rungs,
    )


def _rung(after: str, channel: str) -> CadenceRung:
    return CadenceRung(
        after=after,
        anchor="engagement_start",
        action="send",
        channel=channel,  # type: ignore[arg-type]
        message="checkin",
    )
