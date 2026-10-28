"""Step Functions adapters for ``ComputeNextRung`` and ``AdvanceRung``.

``plan_next_rung`` stays a flat decision. This module nests that decision
under ``cadence`` and keeps the engagement ids on the execution.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from oerf.platform.cadence.calendar import SendingCalendar
from oerf.platform.cadence.engine import SentRung
from oerf.platform.cadence.policy import load_policies
from oerf.platform.cadence.workflow import plan_next_rung

_KEEP = (
    "exhausted",
    "awaits_reply",
    "delay_seconds",
    "timeout_seconds",
    "next_rung",
    "channel",
    "action",
    "message",
    "to_role",
    "final_question",
    "deadline",
    "due_at",
)


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Plan the next rung, or record the rung that just finished."""

    step = event.get("step_name")
    workflow = event.get("workflow")
    if not isinstance(workflow, dict):
        raise ValueError("engagement cadence needs a workflow")
    now = datetime.now(UTC)
    if step == "EngagementComputeNextRung":
        return compute_rung(workflow, now)
    if step == "EngagementAdvanceRung":
        return advance_rung(workflow, now)
    raise ValueError(f"cadence function does not serve {step!r}")


def compute_rung(workflow: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Return the same engagement document with ``cadence`` filled in."""

    policy_id = workflow.get("policy_id")
    if not isinstance(policy_id, str) or not policy_id.strip():
        raise ValueError("engagement cadence needs policy_id")
    policies = load_policies()
    policy = policies.get(policy_id.strip())
    if policy is None:
        raise ValueError(f"no cadence policy {policy_id!r}")
    started = _started(workflow, now)
    decision = plan_next_rung(
        policy,
        _history(workflow),
        now,
        SendingCalendar(),
        engagement_started_at=started,
    )
    updated = dict(workflow)
    updated["engagement_started_at"] = started.isoformat()
    updated["cadence"] = _cadence(decision)
    action = decision.get("exhaust_action")
    if decision.get("exhausted") and action is not None:
        updated["exhaust_action"] = action
    return updated


def advance_rung(workflow: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Append the rung that was just attempted and drop the stale reply."""

    cadence = workflow.get("cadence")
    if not isinstance(cadence, dict):
        raise ValueError("advance needs the cadence just attempted")
    rung = cadence.get("next_rung")
    if not isinstance(rung, int):
        raise ValueError("advance needs cadence.next_rung")
    updated = dict(workflow)
    history = list(workflow.get("history") or [])
    history.append({"rung": rung, "sent_at": now.isoformat()})
    updated["history"] = history
    updated.pop("reply", None)
    updated.pop("response", None)
    updated.pop("rung_timeout", None)
    return updated


def _started(workflow: dict[str, Any], now: datetime) -> datetime:
    raw = workflow.get("engagement_started_at")
    if isinstance(raw, str) and raw.strip():
        return _moment(raw)
    return now


def _history(workflow: dict[str, Any]) -> list[SentRung]:
    raw = workflow.get("history") or []
    if not isinstance(raw, list):
        raise ValueError("engagement history must be a list")
    history: list[SentRung] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("engagement history entries must be objects")
        rung = item.get("rung")
        sent_at = item.get("sent_at")
        if not isinstance(rung, int) or not isinstance(sent_at, str):
            raise ValueError("engagement history needs rung and sent_at")
        history.append(SentRung(rung=rung, sent_at=_moment(sent_at)))
    return history


def _moment(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("engagement timestamps need a timezone")
    return parsed


def _cadence(decision: dict[str, Any]) -> dict[str, Any]:
    """The fields ``CheckStop`` and ``WaitUntilDue`` read, with no nulls."""

    cadence: dict[str, Any] = {}
    for name in _KEEP:
        if name not in decision or decision[name] is None:
            continue
        cadence[name] = decision[name]
    cadence["exhausted"] = bool(decision.get("exhausted"))
    cadence["awaits_reply"] = bool(decision.get("awaits_reply"))
    cadence["delay_seconds"] = int(decision.get("delay_seconds") or 0)
    cadence["timeout_seconds"] = int(decision.get("timeout_seconds") or 0)
    return cadence
