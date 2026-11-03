"""Lifecycle Lambda adapters stay deterministic and never perform side effects."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from oerf.platform.cadence import engagement_lifecycle as lifecycle

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def test_handle_callback_uses_tenant_mapping_and_keeps_original_deadline(monkeypatch) -> None:
    deadline = NOW + timedelta(minutes=2)
    config = SimpleNamespace(
        callback_roles={"driver": "fleet_supervisor"},
        callback=SimpleNamespace(wait="PT5M"),
    )
    monkeypatch.setattr(lifecycle, "_config", lambda _workflow: config)
    workflow = {
        "tenant_id": "t1",
        "role": "driver",
        "deadline": deadline.isoformat(),
        "cadence": {"deadline": deadline.isoformat()},
    }

    result = lifecycle.handle_callback(workflow, now=NOW)

    assert result["callback"]["callback_role"] == "fleet_supervisor"
    assert result["callback_requested_by"] == "driver"
    assert result["role"] == "fleet_supervisor"
    assert result["callback"]["timeout_seconds"] == 120
    assert result["deadline"] == deadline.isoformat()
    assert workflow["deadline"] == deadline.isoformat()


def test_ladder_reset_drops_stale_response_but_preserves_deadline() -> None:
    workflow = {
        "deadline": NOW.isoformat(),
        "history": [{"rung": 2, "sent_at": NOW.isoformat()}],
        "reply": {"text": "stale"},
        "response": {"intent": "clarify"},
        "rung_timeout": {"error": "timeout"},
    }

    result = lifecycle.reset_ladder(workflow)

    assert result["history"] == []
    assert not {"reply", "response", "rung_timeout"} & result.keys()
    assert result["deadline"] == workflow["deadline"]
    assert workflow["reply"]["text"] == "stale"


def test_final_question_prepares_router_payload_with_deadline_bounded_wait() -> None:
    workflow = {
        "deadline": NOW.isoformat(),
        "history": [{"rung": 2, "sent_at": NOW.isoformat()}],
        "cadence": {
            "final_question": "Final check?",
            "final_question_timeout_seconds": 17,
            "channel": "whatsapp",
        },
    }

    result = lifecycle.final_question({}, workflow)

    assert result["cadence"]["message"] == "Final check?"
    assert result["cadence"]["inline_text"] is True
    assert result["cadence"]["next_rung"] == 3
    assert result["cadence"]["final_question_timeout_seconds"] == 17
    assert result["cadence"]["deadline"] == NOW.isoformat()
    assert "task_token" not in result


def test_final_question_rejects_missing_question() -> None:
    with pytest.raises(ValueError, match="final question"):
        lifecycle.final_question({}, {"cadence": {"final_question": " "}})


def test_exhaustion_is_returned_as_data_without_transitioning_state() -> None:
    workflow = {"incident_state": "OPEN", "exhaust_action": {"type": "escalate"}}

    result = lifecycle.apply_exhaust_action(workflow)

    assert result["exhaustion"] == {"status": "exhausted", "action": {"type": "escalate"}}
    assert result["incident_state"] == "OPEN"
    assert "exhaustion" not in workflow
