"""Offline routing tests for the 42 driver-reply corpus rows (OERF-212)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oerf.platform.cadence.interpret import interpret_workflow
from tests.unit.test_statemachine_definition import ASL_PATH, _walk_engagement

_ROOT = Path(__file__).resolve().parents[3]
_DATASET = _ROOT / "tests" / "ai-evals" / "driver_replies.jsonl"
_ROWS = [json.loads(line) for line in _DATASET.read_text(encoding="utf-8").splitlines() if line]


def _interpreter(row: dict, catalog: set[str], observed: list[dict] | None = None):
    """Build deterministic schema-valid proposals; nothing calls a model or AWS."""

    category = row["expected_intent"]
    intent = {
        "answer": "unit_running",
        "decline": "decline",
        "call-me": "call_me",
    }.get(category, "unit_running")
    proposal = {
        "category": category,
        "intent": intent,
        "fields": row["expected_fields"],
        "confidence": 0.95,
    }

    def interpret(event: dict, _context: object) -> dict:
        result = interpret_workflow(
            event["workflow"],
            enabled=True,
            classify=lambda *_args: proposal,
            confidence_floor=0.8,
            knows_fault_code=lambda _tenant, code: code.upper() in catalog,
        )
        if observed is not None:
            observed.append(result["response"])
        return result

    return interpret


def _initial_workflow() -> dict:
    return {
        "tenant_id": "t1",
        "incident_id": "INC-SYNTHETIC",
        "shipment_id": "PH-2841",
        "engagement_id": "ENG-SYNTHETIC-driver-diagnose",
        "role": "driver",
        "ask": "diagnose",
        "policy_id": "critical_field",
        "facts": {"shipment_id": "PH-2841"},
    }


@pytest.mark.parametrize("row", _ROWS, ids=lambda row: row["id"])
def test_each_corpus_row_takes_its_documented_asl_route(row: dict) -> None:
    """Route fixture-approved proposals through actual Interpret and ASL handlers."""

    catalog = {"E17", "E05", "E22", "E09"}
    definition = json.loads(ASL_PATH.with_name("engage_party.asl.json").read_text(encoding="utf-8"))
    category = row["expected_intent"]
    if category == "answer":
        expected_state = "Answered"
        expected_intent = "unit_running"
        stop_at = frozenset()
    elif category == "decline":
        expected_state = "Answered"
        expected_intent = "decline"
        stop_at = frozenset()
    elif category == "call-me":
        expected_state = "HandleCallback"
        expected_intent = "call_me"
        stop_at = frozenset({"HandleCallback"})
    else:
        expected_state = "ResetLadder"
        expected_intent = "clarify"
        stop_at = frozenset({"ResetLadder"})

    observed: list[dict] = []
    visited, final = _walk_engagement(
        definition,
        _initial_workflow(),
        row["text"],
        stop_at=stop_at,
        interpret_fn=_interpreter(row, catalog, observed),
    )

    assert expected_state in visited, row["id"]
    assert observed[0]["intent"] == expected_intent, row["id"]
    assert observed[0]["valid"] is (category in {"answer", "decline", "call-me"}), row["id"]
    if expected_state == "Answered":
        assert visited[-1] == "Answered"
        assert "AcknowledgeReply" in visited
    elif expected_state == "ResetLadder":
        assert "AcknowledgeReply" not in visited
        assert "Answered" not in visited
    else:
        assert "AcknowledgeReply" not in visited
        assert "Answered" not in visited


@pytest.mark.parametrize("failure", ["exception", "malformed", "low-confidence"])
def test_classifier_failures_clarify_and_reset_without_consuming(failure: str) -> None:
    definition = json.loads(ASL_PATH.with_name("engage_party.asl.json").read_text(encoding="utf-8"))
    workflow = _initial_workflow()
    observed: list[dict] = []

    def classify(*_args: object) -> object:
        if failure == "exception":
            raise TimeoutError("simulated classifier timeout")
        proposal = {
            "category": "answer",
            "intent": "unit_running",
            "fields": {},
            "confidence": 0.95 if failure == "malformed" else 0.2,
        }
        if failure == "malformed":
            proposal["unexpected"] = True
        return proposal

    def interpret(event: dict, _context: object) -> dict:
        result = interpret_workflow(
            event["workflow"],
            enabled=True,
            classify=classify,
            confidence_floor=0.8,
            knows_fault_code=lambda *_args: False,
        )
        observed.append(result["response"])
        return result

    visited, final = _walk_engagement(
        definition,
        workflow,
        "the unit seems to be running",
        stop_at=frozenset({"ResetLadder"}),
        interpret_fn=interpret,
    )

    assert observed[0]["intent"] == "clarify"
    assert observed[0]["valid"] is False
    assert "ResetLadder" in visited
    assert "AcknowledgeReply" not in visited
    assert "Answered" not in visited
    assert "response" not in final and "reply" not in final


@pytest.mark.parametrize(
    "row",
    [
        row
        for row in _ROWS
        if row["expected_intent"] == "answer" and "fault_code" in row["expected_fields"]
    ],
    ids=lambda row: row["id"],
)
def test_code_answers_fail_closed_without_tenant_catalog(row: dict) -> None:
    """The test fixture's positive catalog must not mask the production precondition."""

    definition = json.loads(ASL_PATH.with_name("engage_party.asl.json").read_text(encoding="utf-8"))
    observed: list[dict] = []
    visited, final = _walk_engagement(
        definition,
        _initial_workflow(),
        row["text"],
        stop_at=frozenset({"ResetLadder"}),
        interpret_fn=_interpreter(row, set(), observed),
    )

    assert "ResetLadder" in visited
    assert observed[0]["intent"] == "clarify"
    assert observed[0]["valid"] is False
    assert "response" not in final and "reply" not in final
    assert "AcknowledgeReply" not in visited
    assert "Answered" not in visited
