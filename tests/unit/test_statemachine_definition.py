"""Structural validation of the cold_chain_excursion_v1 ASL definition.

These tests read the state machine definition directly so a broken transition
target or an accidentally un-stubbed task fails fast, without needing AWS.
"""

import json
from pathlib import Path

from domains.cold_chain_excursion.steps.workflow_stub import handler

ASL_PATH = (
    Path(__file__).resolve().parents[2]
    / "domains"
    / "cold_chain_excursion"
    / "statemachine.asl.json"
)
STUB_PLACEHOLDER = "__STUB_ARN__"


def load_definition() -> dict:
    return json.loads(ASL_PATH.read_text(encoding="utf-8"))


def transition_targets(state: dict) -> list[str]:
    targets = []
    if "Next" in state:
        targets.append(state["Next"])
    if "Default" in state:
        targets.append(state["Default"])
    targets.extend(choice["Next"] for choice in state.get("Choices", []))
    targets.extend(catcher["Next"] for catcher in state.get("Catch", []))
    return targets


def task_states() -> dict:
    return {
        name: state
        for name, state in load_definition()["States"].items()
        if state["Type"] == "Task"
    }


def test_every_transition_points_to_a_declared_state() -> None:
    definition = load_definition()
    states = definition["States"]

    assert definition["StartAt"] in states
    for name, state in states.items():
        for target in transition_targets(state):
            assert target in states, f"{name} -> {target}"


def test_task_states_invoke_the_stub_lambda_with_retries_and_catches() -> None:
    tasks = task_states()

    assert tasks
    for name, state in tasks.items():
        assert state["Resource"] == "arn:aws:states:::lambda:invoke", name
        assert state["Parameters"]["FunctionName"] == STUB_PLACEHOLDER, name
        assert state["OutputPath"] == "$.Payload", name
        assert state["Retry"][0]["MaxAttempts"] == 3, name
        assert state["TimeoutSeconds"] == 60, name
        if name == "Escalate":
            # Terminal escalation must not catch, otherwise a persistent
            # failure would loop between escalation and failure recording.
            assert "Catch" not in state, name
        else:
            expected = "Escalate" if name == "RecordFailure" else "RecordFailure"
            assert state["Catch"][0]["Next"] == expected, name


def test_primary_flow_prefix_is_intact() -> None:
    definition = load_definition()
    states = definition["States"]

    visited = []
    current = definition["StartAt"]
    while "Next" in states[current]:
        visited.append(current)
        current = states[current]["Next"]
    visited.append(current)

    assert visited == [
        "OPEN",
        "GatherContext",
        "Diagnose",
        "Assess",
        "NeedApproval?",
    ]
    assert states["NeedApproval?"]["Type"] == "Choice"


def test_contained_branch_reaches_success() -> None:
    states = load_definition()["States"]

    chain = []
    current = "ContainmentDecision"
    while True:
        chain.append(current)
        state = states[current]
        if state["Type"] == "Choice":
            # The first choice in each decision is the happy-path branch:
            # containment.status == contained, then qa.disposition == approved.
            current = state["Choices"][0]["Next"]
        elif "Next" in state:
            current = state["Next"]
        else:
            break

    assert states["ContainmentDecision"]["Choices"][0] == {
        "Variable": "$.containment.status",
        "StringEquals": "contained",
        "Next": "GenerateReport",
    }
    assert chain == [
        "ContainmentDecision",
        "GenerateReport",
        "QA",
        "QADecision",
        "Close",
        "WorkflowComplete",
    ]
    assert states["WorkflowComplete"]["Type"] == "Succeed"


def test_failure_path_records_then_escalates_and_terminates() -> None:
    states = load_definition()["States"]

    assert states["RecordFailure"]["Next"] == "Escalate"
    assert states["Escalate"]["End"] is True
    assert states["HandleReopen"]["Next"] == "VerifyLoop"


def test_terminal_states_have_no_next_transition() -> None:
    for name, state in load_definition()["States"].items():
        if state["Type"] == "Succeed" or state.get("End"):
            assert "Next" not in state, name


def test_definition_guards_against_orphaned_executions() -> None:
    # The CDK L2 `timeout` prop is not rendered for a from_string definition,
    # so the execution timeout lives in the ASL top level instead.
    assert load_definition()["TimeoutSeconds"] == 86400


# ---------------------------------------------------------------------------
# Offline simulation: walk the ASL graph using the real stub handler so a
# mismatch between the definition's Choice variables and the stub's output
# shape fails here instead of at runtime.
# ---------------------------------------------------------------------------

INITIAL_EVENT = {
    "tenant_id": "T1",
    "incident_id": "INC-001",
    "shipment_id": "PH-2841",
    "domain": "cold_chain_excursion",
    "trigger": {"device_id": "REEFER-001", "temperature_c": 8.1},
}


def resolve(data: dict, path: str) -> object:
    node: object = data
    for part in path.lstrip("$.").split("."):
        node = node[part]
    return node


def evaluate_choice(state: dict, data: dict) -> str:
    for choice in state["Choices"]:
        actual = resolve(data, choice["Variable"])
        if "BooleanEquals" in choice and actual == choice["BooleanEquals"]:
            return choice["Next"]
        if "StringEquals" in choice and actual == choice["StringEquals"]:
            return choice["Next"]
    return state["Default"]


def simulate(event: dict, handler_fn=handler, max_steps: int = 40) -> tuple:
    definition = load_definition()
    states = definition["States"]
    current = definition["StartAt"]
    data = event

    for _ in range(max_steps):
        state = states[current]
        if state["Type"] == "Succeed":
            return "WorkflowComplete", data
        if state["Type"] == "Task":
            payload = {
                "step_name": state["Parameters"]["Payload"]["step_name"],
                "workflow": data,
            }
            data = handler_fn(payload, None)
            if state.get("End"):
                return current, data
            current = state["Next"]
        elif state["Type"] == "Choice":
            current = evaluate_choice(state, data)
        elif state["Type"] == "Wait":
            current = state["Next"]
        else:  # pragma: no cover - guards against new unsupported state types
            raise AssertionError(f"unsupported state type {state['Type']}")

    raise AssertionError("simulation did not terminate")


def test_stub_contract_drives_the_happy_path_to_closed() -> None:
    outcome, final = simulate(dict(INITIAL_EVENT))

    assert outcome == "WorkflowComplete"
    assert final["incident_state"] == "CLOSED"
    assert final["current_stage"] == "CLOSED"
    # Identity fields must survive every stub round trip.
    assert final["tenant_id"] == "T1"
    assert final["incident_id"] == "INC-001"
    assert final["shipment_id"] == "PH-2841"
    assert final["last_completed_step"] == "Close"


def test_rejected_approval_escalates_and_keeps_identity() -> None:
    visited: list[str] = []

    def rejecting_handler(event: dict, _context: object) -> dict:
        visited.append(event["step_name"])
        result = handler(event, None)
        if event["step_name"] == "Assess":
            result["decision"]["needs_approval"] = True
        if event["step_name"] == "Approval":
            result["approval"]["status"] = "rejected"
        return result

    outcome, final = simulate(dict(INITIAL_EVENT), rejecting_handler)

    assert "Approval" in visited
    assert outcome == "Escalate"
    assert final["escalated"] is True
    assert final["tenant_id"] == "T1"
    assert final["incident_id"] == "INC-001"


def test_declined_facility_escalates() -> None:
    def declining_handler(event: dict, _context: object) -> dict:
        result = handler(event, None)
        if event["step_name"] == "SecureFacility":
            result["facility"]["accepted"] = False
        return result

    outcome, final = simulate(dict(INITIAL_EVENT), declining_handler)

    assert outcome == "Escalate"
    assert final["escalated"] is True


def test_reopened_containment_loops_through_reopen_then_closes() -> None:
    seen: list[str] = []

    def flapping_handler(event: dict, _context: object) -> dict:
        result = handler(event, None)
        if event["step_name"] == "Verify":
            if not seen:
                result["containment"]["status"] = "reopened"
            seen.append(result["containment"]["status"])
        return result

    outcome, final = simulate(dict(INITIAL_EVENT), flapping_handler)

    assert seen == ["reopened", "contained"]
    assert outcome == "WorkflowComplete"
    assert final["incident_state"] == "CLOSED"
