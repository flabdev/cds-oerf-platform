"""Structural validation of the cold_chain_excursion_v1 ASL definition.

These tests read the state machine definition directly so a broken transition
target or an accidentally un-stubbed task fails fast, without needing AWS.
"""

import json
from pathlib import Path

from domains.cold_chain_excursion.steps.workflow_stub import handler
from infra.step_functions import (
    ASL_STEP_PLACEHOLDERS,
    STEP_FUNCTION_NAMES,
    WIRED_STATES,
    function_arn,
)
from oerf.platform.incidents.repository import TRANSITIONS

ASL_PATH = (
    Path(__file__).resolve().parents[2]
    / "domains"
    / "cold_chain_excursion"
    / "statemachine.asl.json"
)
STUB_PLACEHOLDER = "__STUB_ARN__"
REAL_PLACEHOLDERS = {
    "GatherContext": "__STEP_CONTEXT_ARN__",
    "Assess": "__STEP_ASSESS_ARN__",
    "Act": "__STEP_ACT_ARN__",
    "VerifyLoop": "__STEP_VERIFY_ARN__",
    "QA": "__STEP_QA_ARN__",
    "Close": "__STEP_CLOSE_ARN__",
}
STUB_COLD_CHAIN_TASKS = frozenset(
    {
        "OPEN",
        "Diagnose",
        "Approval",
        "SecureFacility",
        "HandleReopen",
        "GenerateReport",
        "RecordFailure",
        "Escalate",
        "NoResponse",
        "Cancel",
        "RecordContainment",
        "BeginDisposition",
    }
)
STUB_ENGAGEMENT_TASKS = frozenset(
    {
        "ComputeNextRung",
        "SendOneWay",
        "SendViaRouter",
        "Interpret",
        "HandleCallback",
        "AdvanceRung",
        "ResetLadder",
        "SendFinalQuestion",
        "ApplyExhaustAction",
    }
)


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


def test_real_and_stub_tasks_are_listed() -> None:
    """The six wired tasks are explicit. Everything else stays on the stub."""

    cold = task_states()
    engagement = {
        name: state
        for name, state in json.loads(
            (ASL_PATH.parent / "engage_party.asl.json").read_text(encoding="utf-8")
        )["States"].items()
        if state["Type"] == "Task"
    }
    real = {name for name, state in cold.items() if name in REAL_PLACEHOLDERS}
    stub = {
        name
        for name, state in cold.items()
        if state.get("Resource", "").startswith("arn:aws:states:::lambda:")
        and state["Parameters"]["FunctionName"] == STUB_PLACEHOLDER
    }
    engagement_stub = {
        name
        for name, state in engagement.items()
        if state["Parameters"]["FunctionName"] == "__ENGAGEMENT_STUB_ARN__"
    }

    assert real == frozenset(REAL_PLACEHOLDERS)
    assert stub == STUB_COLD_CHAIN_TASKS
    assert engagement_stub == STUB_ENGAGEMENT_TASKS
    assert "EngageDriverDiagnosis" not in real
    assert "EngageDriverDiagnosis" not in stub
    assert cold["EngageDriverDiagnosis"]["Resource"].endswith("startExecution.sync:2")


def test_wired_tasks_resolve_to_their_function_arns() -> None:
    text = ASL_PATH.read_text(encoding="utf-8")
    for placeholder, phase in ASL_STEP_PLACEHOLDERS.items():
        text = text.replace(
            placeholder,
            function_arn("us-east-1", "000000000000", STEP_FUNCTION_NAMES[phase]),
        )
    text = text.replace("__STUB_ARN__", "arn:aws:lambda:us-east-1:000000000000:function:stub")
    states = json.loads(text)["States"]

    for state, phase in WIRED_STATES.items():
        expected = function_arn("us-east-1", "000000000000", STEP_FUNCTION_NAMES[phase])
        assert states[state]["Parameters"]["FunctionName"] == expected, state
    for name in STUB_COLD_CHAIN_TASKS:
        assert states[name]["Parameters"]["FunctionName"].endswith(":function:stub"), name


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
        if name == "EngageDriverDiagnosis":
            assert state["Resource"] == "arn:aws:states:::states:startExecution.sync:2"
            assert state["Parameters"]["StateMachineArn"] == "__ENGAGE_PARTY_ARN__"
            continue
        expected_resource = (
            "arn:aws:states:::lambda:invoke.waitForTaskToken"
            if name == "Approval"
            else "arn:aws:states:::lambda:invoke"
        )
        assert state["Resource"] == expected_resource, name
        assert state["Parameters"]["FunctionName"] == REAL_PLACEHOLDERS.get(
            name, STUB_PLACEHOLDER
        ), name
        assert state["OutputPath"] == "$.Payload", name
        assert state["Retry"][0]["MaxAttempts"] == 3, name
        assert state["TimeoutSeconds"] == (1800 if name == "Approval" else 60), name
        if name in {"Escalate", "Cancel", "Close"}:
            # Terminal outcomes must not loop after persistent failure.
            if name == "Close":
                assert state["Catch"][0]["Next"] == "TerminalFailure"
            else:
                assert "Catch" not in state, name
        elif name == "Approval":
            assert state["Catch"][0]["Next"] == "NoResponse"
            assert state["Catch"][-1]["Next"] == "RecordFailure"
        else:
            expected = "FailureRoute" if name == "RecordFailure" else "RecordFailure"
            assert state["Catch"][-1]["Next"] == expected, name


def test_nested_engagement_is_a_token_waiting_state_machine_and_is_wired_after_assess() -> None:
    definition = json.loads(
        (ASL_PATH.parent / "engage_party.asl.json").read_text(encoding="utf-8")
    )
    states = definition["States"]
    parent_states = load_definition()["States"]
    parent = parent_states["EngageDriverDiagnosis"]
    assert parent_states["Assess"]["Next"] == "EngageDriverDiagnosis"
    assert parent["Next"] == "CancelAfterEngageDriverDiagnosis?"
    assert parent_states["CancelAfterEngageDriverDiagnosis?"]["Default"] == "NeedApproval?"

    assert definition["StartAt"] == "ComputeNextRung"
    assert parent["Resource"] == "arn:aws:states:::states:startExecution.sync:2"
    assert parent["Parameters"]["Input"]["role"] == "driver"
    assert parent["Parameters"]["Input"]["ask"] == "diagnose"
    assert states["SendViaRouter"]["Resource"].endswith(".waitForTaskToken")
    assert states["SendViaRouter"]["Parameters"]["Payload"]["task_token.$"] == "$$.Task.Token"
    assert states["SendViaRouter"]["TimeoutSecondsPath"] == "$.cadence.timeout_seconds"
    assert states["SendViaRouter"]["ResultPath"] == "$.reply"
    assert states["Interpret"]["Next"] == "InterpretResult"
    assert states["SendFinalQuestion"]["TimeoutSeconds"] == 86400
    assert states["SendViaRouter"]["Resource"].endswith(".waitForTaskToken")
    assert states["InterpretResult"]["Choices"][0]["Next"] == "HandleCallback"
    assert states["SendViaRouter"]["Catch"][0]["Next"] == "AdvanceRung"
    assert states["CallbackPause"]["Next"] == "ComputeNextRung"
    assert states["Answered"]["Parameters"]["status"] == "answered"
    assert states["Exhausted"]["Parameters"]["status"] == "exhausted"
    assert states["SendOneWay"]["Resource"] == "arn:aws:states:::lambda:invoke"
    assert states["HandleCallback"]["Next"] == "CallbackPause"


def test_every_nested_engagement_transition_points_to_a_declared_state() -> None:
    definition = json.loads(
        (ASL_PATH.parent / "engage_party.asl.json").read_text(encoding="utf-8")
    )
    states = definition["States"]

    for name, state in states.items():
        for target in transition_targets(state):
            assert target in states, f"{name} -> {target}"


def test_human_approval_uses_bounded_task_token_and_timeout_route() -> None:
    states = load_definition()["States"]
    approval = states["Approval"]

    assert approval["Resource"] == "arn:aws:states:::lambda:invoke.waitForTaskToken"
    assert approval["Parameters"]["Payload"]["task_token.$"] == "$$.Task.Token"
    assert approval["TimeoutSeconds"] == 1800
    assert any(catch["Next"] == "NoResponse" for catch in approval["Catch"])
    assert states["NoResponse"]["Next"] == "RecordFailure"


def test_cancel_and_failure_routes_cover_canonical_terminal_transitions() -> None:
    states = load_definition()["States"]

    assert states["CancelRequested?"]["Default"] == "GatherContext"
    assert states["CancelAfterOPEN?"]["Default"] == "CancelRequested?"
    assert states["CancelAfterOPEN?"]["Choices"][0]["Next"] == "Cancel"
    assert states["Cancel"]["End"] is True
    assert states["Cancel"]["Parameters"]["Payload"]["step_name"] == "Cancel"
    failure_route = states["FailureRoute"]
    escalation_sources = {
        choice["StringEquals"]
        for choice in failure_route["Choices"]
        if choice["Next"] == "Escalate"
    }
    assert escalation_sources == {
        state for state, targets in TRANSITIONS.items() if "ESCALATED" in targets
    }
    assert all(
        "CANCELLED" in targets
        for state, targets in TRANSITIONS.items()
        if state not in {"CLOSED", "CANCELLED"}
    )
    assert failure_route["Default"] == "Cancel"


def test_cancellation_is_checked_after_work_steps_and_infeasible_assessment_escalates() -> None:
    states = load_definition()["States"]
    work_steps = (
        "OPEN",
        "GatherContext",
        "Diagnose",
        "Assess",
        "EngageDriverDiagnosis",
        "Approval",
        "SecureFacility",
        "Act",
        "VerifyLoop",
        "HandleReopen",
        "RecordContainment",
        "BeginDisposition",
        "GenerateReport",
        "QA",
    )

    for step in work_steps:
        gate = states[step]["Next"]
        if step == "Diagnose":
            assert gate == "Assess"
            continue
        if step == "Assess":
            assert gate == "EngageDriverDiagnosis"
            gate = states[gate]["Next"]
        assert gate.startswith("CancelAfter"), step
        assert states[gate]["Choices"] == [
            {
                "Variable": "$.cancel_requested",
                "BooleanEquals": True,
                "Next": "Cancel",
            }
        ]
    assert states["NeedApproval?"]["Choices"][0] == {
        "Variable": "$.decision.has_feasible_options",
        "BooleanEquals": False,
        "Next": "Escalate",
    }


def test_human_approval_and_cancellation_decisions_are_explicit() -> None:
    states = load_definition()["States"]

    assert any(
        choice["StringEquals"] == "cancelled" and choice["Next"] == "Cancel"
        for choice in states["ApprovalDecision"]["Choices"]
    )
    assert states["QADecision"]["Default"] == "FailureRoute"


def test_primary_flow_prefix_is_intact() -> None:
    definition = load_definition()
    states = definition["States"]

    visited = []
    current = definition["StartAt"]
    while True:
        visited.append(current)
        state = states[current]
        if state["Type"] == "Choice":
            current = state["Default"]
        elif "Next" in state:
            current = state["Next"]
        else:
            break

    assert visited[:6] == [
        "OPEN",
        "CancelAfterOPEN?",
        "CancelRequested?",
        "GatherContext",
        "CancelAfterGatherContext?",
        "Diagnose",
    ]
    assert states["NeedApproval?"]["Type"] == "Choice"


def test_contained_branch_reaches_success() -> None:
    states = load_definition()["States"]

    chain = []
    current = "ContainmentDecision"
    while True:
        chain.append(current)
        state = states[current]
        if current.startswith("CancelAfter"):
            current = state["Default"]
        elif state["Type"] == "Choice":
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
        "Next": "RecordContainment",
    }
    assert states["RecordContainment"]["Next"] == "CancelAfterRecordContainment?"
    assert states["CancelAfterRecordContainment?"]["Default"] == "BeginDisposition"
    assert states["BeginDisposition"]["Next"] == "CancelAfterBeginDisposition?"
    assert states["CancelAfterBeginDisposition?"]["Default"] == "GenerateReport"
    assert chain == [
        "ContainmentDecision",
        "RecordContainment",
        "CancelAfterRecordContainment?",
        "BeginDisposition",
        "CancelAfterBeginDisposition?",
        "GenerateReport",
        "CancelAfterGenerateReport?",
        "QA",
        "CancelAfterQA?",
        "QADecision",
        "Close",
        "WorkflowComplete",
    ]
    assert states["WorkflowComplete"]["Type"] == "Succeed"


def test_failure_path_records_then_escalates_and_terminates() -> None:
    states = load_definition()["States"]

    assert states["RecordFailure"]["Next"] == "FailureRoute"
    assert states["Escalate"]["End"] is True
    assert states["FailureRoute"]["Default"] == "Cancel"
    assert states["HandleReopen"]["Next"] == "CancelAfterHandleReopen?"
    assert states["CancelAfterHandleReopen?"]["Default"] == "VerifyLoop"


def test_terminal_states_have_no_next_transition() -> None:
    for name, state in load_definition()["States"].items():
        if state["Type"] in {"Succeed", "Fail"} or state.get("End"):
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
    "cancel_requested": False,
}


def resolve(data: dict, path: str) -> object:
    node: object = data
    for part in path.lstrip("$.").split("."):
        if not isinstance(node, dict) or part not in node:
            return None
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
            if state["Resource"] == "arn:aws:states:::states:startExecution.sync:2":
                data["diagnosis"] = {"engagement_id": "ENG-1", "policy_id": "critical_field"}
                data["engagement_result"] = {"status": "answered", "intent": "unit_status"}
                current = state["Next"]
                continue
            if state["Resource"].endswith(".waitForTaskToken"):
                # Offline contract: the real inbound callback resumes the task;
                # the deterministic stub supplies the same valid reply.
                data["task_token"] = "offline-task-token"
            payload = {
                "step_name": state["Parameters"]["Payload"]["step_name"],
                "workflow": data,
            }
            previous_incident_state = data.get("incident_state")
            try:
                data = handler_fn(payload, None)
            except Exception as error:
                error_name = "States.Timeout" if isinstance(error, TimeoutError) else "States.ALL"
                catcher = next(
                    (
                        item
                        for item in state.get("Catch", [])
                        if "States.ALL" in item["ErrorEquals"] or error_name in item["ErrorEquals"]
                    ),
                    None,
                )
                if catcher is None:
                    raise
                data = dict(data)
                data["simulated_error"] = error_name
                current = catcher["Next"]
                continue
            next_incident_state = data.get("incident_state")
            if (
                previous_incident_state is not None
                and next_incident_state != previous_incident_state
            ):
                assert next_incident_state in TRANSITIONS[previous_incident_state], (
                    f"illegal simulated incident transition: "
                    f"{previous_incident_state} -> {next_incident_state}"
                )
            if state.get("End"):
                return current, data
            current = state["Next"]
        elif state["Type"] == "Choice":
            current = evaluate_choice(state, data)
        elif state["Type"] == "Wait":
            current = state["Next"]
        elif state["Type"] == "Fail":
            return current, data
        else:  # pragma: no cover - guards against new unsupported state types
            raise AssertionError(f"unsupported state type {state['Type']}")

    raise AssertionError("simulation did not terminate")


def test_cancelled_input_terminates_without_running_business_steps() -> None:
    event = dict(INITIAL_EVENT, cancel_requested=True)

    outcome, final = simulate(event)

    assert outcome == "Cancel"
    assert final["incident_state"] == "CANCELLED"
    assert final["cancelled"] is True
    assert final["last_completed_step"] == "Cancel"


def test_failed_close_terminates_at_terminal_failure_instead_of_cancelling_closed_incident() -> (
    None
):
    def failed_close_handler(event: dict, _context: object) -> dict:
        result = handler(event, None)
        if event["step_name"] == "Close":
            raise RuntimeError("close persistence failed")
        return result

    outcome, final = simulate(dict(INITIAL_EVENT), failed_close_handler)

    assert outcome == "TerminalFailure"
    assert final["incident_state"] == "AWAITING_DISPOSITION"


def test_approval_timeout_is_recorded_then_escalated_from_legal_state() -> None:
    visited: list[str] = []

    def timeout_handler(event: dict, _context: object) -> dict:
        step_name = event["step_name"]
        visited.append(step_name)
        result = handler(event, None)
        if step_name == "Assess":
            result["decision"]["needs_approval"] = True
            result["incident_state"] = "AWAITING_APPROVAL"
        elif step_name == "Approval":
            raise TimeoutError("simulated task-token timeout")
        return result

    # The offline graph simulator models Timeout through the declared catch.
    outcome, final = simulate(dict(INITIAL_EVENT), timeout_handler)

    assert "NoResponse" in visited
    assert "RecordFailure" in visited
    assert outcome == "Escalate"
    assert final["incident_state"] == "ESCALATED"


def test_failed_or_stale_approval_callback_is_caught_and_escalated() -> None:
    visited: list[str] = []

    def failed_callback_handler(event: dict, _context: object) -> dict:
        step_name = event["step_name"]
        visited.append(step_name)
        result = handler(event, None)
        if step_name == "Assess":
            result["decision"]["needs_approval"] = True
            result["incident_state"] = "AWAITING_APPROVAL"
        elif step_name == "Approval":
            raise RuntimeError("callback token is no longer valid")
        return result

    outcome, final = simulate(dict(INITIAL_EVENT), failed_callback_handler)

    assert "RecordFailure" in visited
    assert outcome == "Escalate"
    assert final["incident_state"] == "ESCALATED"


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
