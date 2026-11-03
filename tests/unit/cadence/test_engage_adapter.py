"""Step Functions cadence adapter honors requested callback pauses."""

from datetime import UTC, datetime, timedelta

from oerf.platform.cadence import engage
from oerf.platform.cadence.policy import CadencePolicy, CadenceRung

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


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
            )
        ],
    )


def test_compute_rung_respects_callback_pause_without_changing_deadline(monkeypatch) -> None:
    deadline = NOW + timedelta(minutes=10)
    monkeypatch.setattr(engage, "load_policies", lambda: {"test": _policy()})
    workflow = {
        "policy_id": "test",
        "engagement_started_at": NOW.isoformat(),
        "paused_until": (NOW + timedelta(minutes=5)).isoformat(),
        "deadline": deadline.isoformat(),
        "history": [],
    }

    result = engage.compute_rung(workflow, NOW)

    assert result["cadence"]["next_rung"] == 1
    assert result["cadence"]["delay_seconds"] == 300
    expected_deadline = deadline.isoformat().replace("+00:00", "Z")
    assert result["cadence"]["deadline"] == expected_deadline
    assert result["deadline"] == deadline.isoformat()
