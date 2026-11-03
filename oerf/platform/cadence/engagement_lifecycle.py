"""Deterministic lifecycle handlers for the reusable engagement state machine."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from oerf.platform.cadence.workflow import callback_pause
from oerf.platform.tenancy.config_resolver import get_config

DOMAIN = "cold_chain_excursion"


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Handle callback pauses, ladder resets, final questions, and exhaustion."""

    step = event.get("step_name")
    workflow = event.get("workflow")
    if not isinstance(workflow, dict):
        raise ValueError("engagement lifecycle needs a workflow")
    if step == "EngagementHandleCallback":
        return handle_callback(workflow)
    if step == "EngagementResetLadder":
        return reset_ladder(workflow)
    if step == "EngagementFinalQuestion":
        return final_question(event, workflow)
    if step == "EngagementApplyExhaustAction":
        return apply_exhaust_action(workflow)
    raise ValueError(f"engagement lifecycle does not serve {step!r}")


def handle_callback(workflow: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Pause for the configured callback interval without extending exhaustion."""

    config = _config(workflow)
    role = _required_text(workflow.get("role"), "role")
    deadline = _deadline(workflow)
    pause = callback_pause(
        role=role,
        callback_roles=config.callback_roles,
        callback_wait=config.callback.wait,
        now=now or datetime.now(UTC),
        deadline=deadline,
    )
    updated = dict(workflow)
    updated["callback"] = pause
    updated["callback_requested_by"] = role
    updated["role"] = pause["callback_role"]
    updated["paused_until"] = pause["paused_until"]
    updated["deadline"] = pause["deadline"]
    return updated


def reset_ladder(workflow: dict[str, Any]) -> dict[str, Any]:
    """Clear a stale reply and start another deterministic clarification ladder."""

    updated = dict(workflow)
    updated["history"] = []
    for name in ("reply", "response", "rung_timeout", "final_question_timeout"):
        updated.pop(name, None)
    return updated


def final_question(event: dict[str, Any], workflow: dict[str, Any]) -> dict[str, Any]:
    """Prepare the final question for the channel router's task-token send path."""

    cadence = workflow.get("cadence")
    if not isinstance(cadence, dict):
        raise ValueError("final question needs cadence")
    question = cadence.get("final_question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("final question must be non-empty")
    deadline = _deadline(workflow)
    updated = dict(workflow)
    updated["cadence"] = {
        **cadence,
        "message": question.strip(),
        "inline_text": True,
        "channel": cadence.get("channel") or "whatsapp",
        "next_rung": _next_rung(workflow),
        "deadline": deadline.isoformat(),
    }
    timeout = cadence.get("final_question_timeout_seconds")
    updated["cadence"]["final_question_timeout_seconds"] = (
        min(max(timeout, 1), 86400)
        if isinstance(timeout, int) and not isinstance(timeout, bool)
        else 86400
    )
    return updated


def apply_exhaust_action(workflow: dict[str, Any]) -> dict[str, Any]:
    """Return the configured exhaustion action as data; never mutate incident state."""

    action = workflow.get("exhaust_action")
    if not isinstance(action, dict) or not action:
        raise ValueError("exhaustion needs a configured exhaust_action")
    updated = dict(workflow)
    updated["exhaustion"] = {"status": "exhausted", "action": dict(action)}
    return updated


def _config(workflow: dict[str, Any]) -> Any:
    tenant_id = _required_text(workflow.get("tenant_id"), "tenant_id")
    return get_config(tenant_id, DOMAIN)


def _deadline(workflow: dict[str, Any]) -> datetime:
    cadence = workflow.get("cadence")
    value = workflow.get("deadline")
    if not isinstance(value, str) and isinstance(cadence, dict):
        value = cadence.get("deadline")
    value = _required_text(value, "deadline")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("engagement deadline needs a timezone")
    return parsed.astimezone(UTC)


def _next_rung(workflow: dict[str, Any]) -> int:
    history = workflow.get("history") or []
    if not isinstance(history, list):
        raise ValueError("engagement history must be a list")
    rungs = [item.get("rung") for item in history if isinstance(item, dict)]
    valid = [rung for rung in rungs if isinstance(rung, int) and not isinstance(rung, bool)]
    return max(valid, default=0) + 1


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"engagement lifecycle needs {name}")
    return value.strip()
