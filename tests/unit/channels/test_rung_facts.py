"""Cadence rungs render with the facts the send path actually attaches.

A template that gains a placeholder the rung does not fill fails here, instead
of failing the live child with MissingFacts.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from oerf.platform.cadence.engage import advance_rung, compute_rung
from oerf.platform.channels.engagement_send import _request
from oerf.platform.channels.registry import MemoryPhoneRegistry
from oerf.platform.channels.templates import MissingFacts, MissingTemplate, render_message

MESSAGES = json.loads(
    Path("domains/cold_chain_excursion/messages.json").read_text(encoding="utf-8")
)
CADENCE = json.loads(
    Path("domains/cold_chain_excursion/cadence.json").read_text(encoding="utf-8")
)
TEMPLATES = json.loads(Path("scripts/whatsapp/templates.json").read_text(encoding="utf-8"))
START = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
DRIVER = "+15550100001"
_APPROVED = (
    "The driver of shipment PH-2841 has not replied for 3 minutes during a "
    "temperature alert. Please contact the driver and check they are safe."
)

# Facts the parent puts on the engagement. The send path does not invent the rest.
_POLICY_FACTS = {
    "critical_field": {
        "shipment": {"shipment_id": "PH-2841", "driver": "C-T1-DRV"},
        "trigger": {"temperature_c": 8.6},
    },
    "partner_request": {
        "shipment_id": "PH-2841",
        "range": "2–8°C",
        "facility_name": "Facility A",
        "minutes": 9,
    },
    "approval_fast": {},
}
_CONTACTS = {
    "critical_field": ("driver", "C-T1-DRV", DRIVER),
    "partner_request": ("facility", "C-T1-FACA", "+15550100003"),
    "approval_fast": ("ops_manager", "C-T1-OPS", "+15550100002"),
}


class _Ports:
    def __init__(self) -> None:
        self.registry = MemoryPhoneRegistry()
        for role, contact_id, number in _CONTACTS.values():
            self.registry.register_contact("t1", contact_id, role, number)
        self.parameters = None
        self.mock_whatsapp = True


PORTS = _Ports()


def test_critical_field_ladder_renders_supervisor_escalation() -> None:
    """The silent-driver rung reaches supervisor_escalation with real context facts."""

    role, contact_id, _number = _CONTACTS["critical_field"]
    sent = _walk("critical_field", role, contact_id, _POLICY_FACTS["critical_field"])
    supervisor = [item for item in sent if item[0] == "supervisor_escalation"]

    assert [item[0] for item in sent] == [
        "driver_checkin",
        "driver_reminder",
        "driver_reminder",
        "driver_call",
        "supervisor_escalation",
    ]
    assert len(supervisor) == 1
    message, channel, facts, _due, awaits_reply, send_role = supervisor[0]
    assert channel == "whatsapp"
    assert awaits_reply is False
    assert send_role == "ops_manager"
    assert facts["shipment_id"] == "PH-2841"
    assert facts["minutes"] == 3
    assert facts["temperature_c"] == 8.6
    body = render_message(MESSAGES, message, channel, facts)
    template = next(
        item for item in TEMPLATES["templates"] if item["name"] == "oerf_supervisor_escalation"
    )
    assert template["body"] == (
        "The driver of shipment {{1}} has not replied for {{2}} minutes during a "
        "temperature alert. Please contact the driver and check they are safe."
    )
    assert body == _APPROVED
    assert body == template["body"].replace("{{1}}", "PH-2841").replace("{{2}}", "3")


def test_silence_minutes_are_rounded_down_and_replace_a_copied_value() -> None:
    started = START
    workflow = _cadence_workflow(
        "critical_field",
        "supervisor_escalation",
        "whatsapp",
        {"minutes": 99, "shipment": {"shipment_id": "PH-2841", "driver": "C-T1-DRV"}},
        engagement_started_at=started.isoformat(),
    )

    cases = ((59, 0), (90, 1), (179, 2), (180, 3), (239, 3))
    for elapsed, expected in cases:
        request, _rung, _deadline = _request(
            workflow, PORTS, now=started + timedelta(seconds=elapsed)
        )
        assert request.facts["minutes"] == expected

    early, _rung, _deadline = _request(workflow, PORTS, now=started - timedelta(seconds=30))
    assert early.facts["minutes"] == 0

    untimed = _cadence_workflow(
        "critical_field",
        "supervisor_escalation",
        "whatsapp",
        {"minutes": 99, "shipment": {"shipment_id": "PH-2841", "driver": "C-T1-DRV"}},
    )
    dropped, _rung, _deadline = _request(untimed, PORTS, now=started)
    assert "minutes" not in dropped.facts


def test_every_cadence_rung_renders_with_the_facts_it_provides() -> None:
    """A new placeholder in messages.json fails when the rung does not supply it."""

    for policy in CADENCE["policies"]:
        policy_id = policy["policy_id"]
        role, contact_id, _number = _CONTACTS[policy_id]
        facts = _POLICY_FACTS[policy_id]
        for index, rung in enumerate(policy["rungs"], start=1):
            workflow = _cadence_workflow(
                policy_id,
                rung["message"],
                rung["channel"],
                facts,
                engagement_started_at=START.isoformat(),
                rung_index=index,
            )
            request, _rung, _deadline = _request(
                workflow, PORTS, now=START + timedelta(minutes=3)
            )
            _assert_renders(policy_id, rung["message"], rung["channel"], request.facts)
        if policy_id == "partner_request":
            request, _rung, _deadline = _request(
                _cadence_workflow(
                    policy_id,
                    "facility_request",
                    "whatsapp",
                    facts,
                    engagement_started_at=START.isoformat(),
                ),
                PORTS,
                now=START + timedelta(minutes=3),
            )
            assert request.facts["minutes"] == 9


def _assert_renders(policy_id: str, message: str, channel: str, facts: dict[str, Any]) -> None:
    try:
        render_message(MESSAGES, message, channel, facts)
    except MissingTemplate:
        entry = MESSAGES["messages"].get(message, {})
        body = entry.get(channel) if isinstance(entry, dict) else None
        assert not isinstance(body, str) or not body.strip(), (policy_id, message, channel)
    except MissingFacts as exc:
        pytest.fail(f"{policy_id} {message}/{channel} {exc}")


def _walk(
    policy_id: str,
    role: str,
    contact_id: str,
    facts: dict[str, Any],
) -> list[tuple[str, str, dict[str, Any], datetime, Any, Any]]:
    workflow: dict[str, Any] = {
        "tenant_id": "t1",
        "incident_id": "INC-000131",
        "engagement_id": "ENG-INC-000131-driver-diagnose",
        "role": role,
        "ask": "diagnose",
        "policy_id": policy_id,
        "contact_id": contact_id,
        "facts": facts,
    }
    now = START
    sent: list[tuple[str, str, dict[str, Any], datetime]] = []
    for _ in range(12):
        planned = compute_rung(workflow, now)
        cadence = planned["cadence"]
        if cadence.get("exhausted") or "message" not in cadence:
            return sent
        due = datetime.fromisoformat(cadence["due_at"])
        request, _rung, _deadline = _request(planned, PORTS, now=due)
        sent.append(
            (
                cadence["message"],
                cadence["channel"],
                request.facts,
                due,
                cadence.get("awaits_reply"),
                cadence.get("send_role"),
            )
        )
        if cadence.get("awaits_reply", True):
            now = due + timedelta(seconds=int(cadence["timeout_seconds"]))
        else:
            now = due
        workflow = advance_rung(planned, now)
    raise AssertionError("cadence ladder did not exhaust")


def _cadence_workflow(
    policy_id: str,
    message: str,
    channel: str,
    facts: dict[str, Any],
    *,
    engagement_started_at: str | None = None,
    rung_index: int = 1,
) -> dict[str, Any]:
    role, contact_id, _number = _CONTACTS[policy_id]
    workflow = {
        "tenant_id": "t1",
        "incident_id": "INC-000131",
        "engagement_id": f"ENG-INC-000131-{policy_id}",
        "role": role,
        "ask": "diagnose",
        "policy_id": policy_id,
        "contact_id": contact_id,
        "facts": facts,
        "cadence": {
            "message": message,
            "channel": channel,
            "next_rung": rung_index,
            "deadline": "2026-11-22T10:04:00+00:00",
        },
    }
    if engagement_started_at is not None:
        workflow["engagement_started_at"] = engagement_started_at
    return workflow
