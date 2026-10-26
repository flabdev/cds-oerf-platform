"""Structural validation of the cold_chain_excursion_v1 ASL definition.

These tests read the state machine definition directly so a broken transition
target or an accidentally un-stubbed task fails fast, without needing AWS.
The Step Functions CLI check is marked ``aws`` and is skipped when the CLI
is not configured.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from domains.cold_chain_excursion.steps.workflow_stub import handler
from infra.step_functions import (
    ASL_ENGAGE_PLACEHOLDERS,
    ASL_STEP_PLACEHOLDERS,
    CHANNEL_ROUTER_NAME,
    ENGAGE_FUNCTION_NAMES,
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
    "Diagnose": "__STEP_DIAGNOSE_ARN__",
    "Assess": "__STEP_ASSESS_ARN__",
    "Act": "__STEP_ACT_ARN__",
    "VerifyLoop": "__STEP_VERIFY_ARN__",
    "QA": "__STEP_QA_ARN__",
    "Close": "__STEP_CLOSE_ARN__",
    "RecordFailure": "__STEP_RECORD_FAILURE_ARN__",
    "Escalate": "__STEP_ESCALATE_ARN__",
    "Cancel": "__STEP_CANCEL_ARN__",
}
STUB_COLD_CHAIN_TASKS = frozenset(
    {
        "OPEN",
        "Approval",
        "SecureFacility",
        "HandleReopen",
        "GenerateReport",
        "NoResponse",
        "RecordContainment",
        "BeginDisposition",
    }
)
STUB_ENGAGEMENT_TASKS = frozenset(
    {
        "SendOneWay",
        "HandleCallback",
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
    """Wired tasks name their functions. The remaining tasks stay on the stub."""

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
    assert parent_states["Assess"]["Next"] == "CancelAfterAssess?"
    assert parent_states["CancelAfterAssess?"]["Default"] == "EngageDriverDiagnosis"
    assert parent["Next"] == "CancelAfterEngageDriverDiagnosis?"
    assert parent_states["CancelAfterEngageDriverDiagnosis?"]["Default"] == "NeedApproval?"

    assert definition["StartAt"] == "ComputeNextRung"
    assert parent["Resource"] == "arn:aws:states:::states:startExecution.sync:2"
    assert parent["Parameters"]["Input"]["role"] == "driver"
    assert parent["Parameters"]["Input"]["ask"] == "diagnose"
    assert parent["Parameters"]["Input"]["engagement_id.$"] == "$.workflow.diagnosis.engagement_id"
    assert parent["Parameters"]["Input"]["policy_id.$"] == "$.workflow.diagnosis.policy_id"
    assert parent["ResultPath"] == "$.workflow.diagnosis.engagement_result"
    assert states["ComputeNextRung"]["Parameters"]["FunctionName"] == "__ENGAGE_CADENCE_ARN__"
    assert states["AdvanceRung"]["Parameters"]["FunctionName"] == "__ENGAGE_CADENCE_ARN__"
    assert states["Interpret"]["Parameters"]["FunctionName"] == "__ENGAGE_INTERPRET_ARN__"
    assert states["SendViaRouter"]["Parameters"]["FunctionName"] == "__CHANNEL_ROUTER_ARN__"
    assert "HeartbeatSeconds" not in states["SendViaRouter"]
    assert "HeartbeatTimeout" not in json.dumps(states["SendViaRouter"])
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


def _nested_state_machines(state_name: str, state: dict) -> list[tuple[str, dict]]:
    """Parallel branches and Map processors are their own state machines."""

    nested: list[tuple[str, dict]] = []
    branches = state.get("Branches")
    if isinstance(branches, list):
        for index, branch in enumerate(branches):
            if isinstance(branch, dict):
                nested.append((f"{state_name}/Branches[{index}]", branch))
    for field in ("ItemProcessor", "Iterator"):
        processor = state.get(field)
        if isinstance(processor, dict):
            nested.append((f"{state_name}/{field}", processor))
    return nested


def reachability_problems(machine: dict, label: str) -> list[str]:
    """Missing targets and states Step Functions cannot reach from StartAt.

    Each Parallel branch and each Map ``ItemProcessor`` (or legacy ``Iterator``)
    is a separate graph. A state is reachable only by ``Next``, ``Default``,
    ``Choices[].Next``, or ``Catch[].Next`` from ``StartAt`` of that graph.
    """

    problems: list[str] = []
    states = machine.get("States")
    if not isinstance(states, dict):
        return [f"{label}: missing States object"]

    for name, state in states.items():
        if not isinstance(state, dict):
            problems.append(f"{label}: {name} is not a state object")
            continue
        for target in transition_targets(state):
            if target not in states:
                problems.append(f"{label}: {name} -> {target} (state does not exist)")
        for nested_label, nested in _nested_state_machines(name, state):
            problems.extend(reachability_problems(nested, f"{label} > {nested_label}"))

    start = machine.get("StartAt")
    reachable: set[str] = set()
    if not isinstance(start, str) or start not in states:
        problems.append(f"{label}: StartAt {start!r} is not a state")
    else:
        pending = [start]
        while pending:
            name = pending.pop()
            if name in reachable:
                continue
            reachable.add(name)
            state = states[name]
            if not isinstance(state, dict):
                continue
            for target in transition_targets(state):
                if target in states and target not in reachable:
                    pending.append(target)

    for name in states:
        if name not in reachable:
            problems.append(f"{label}: {name} is not reachable")
    return problems


def test_both_asl_files_match_step_functions_reachability() -> None:
    """Fail with every unreachable state and every transition to a missing state."""

    problems: list[str] = []
    for path in (ASL_PATH, ASL_PATH.parent / "engage_party.asl.json"):
        definition = json.loads(path.read_text(encoding="utf-8"))
        problems.extend(reachability_problems(definition, path.name))
    assert not problems, "ASL reachability:\n" + "\n".join(problems)


_PLACEHOLDER_REGION = "us-east-1"
_PLACEHOLDER_ACCOUNT = "000000000000"
_PLACEHOLDER_TOKEN = re.compile(r"__[A-Z0-9_]+__")


def _placeholder_lambda_arn(name: str) -> str:
    return function_arn(_PLACEHOLDER_REGION, _PLACEHOLDER_ACCOUNT, name)


def _definition_with_placeholder_arns(text: str, *, engagement: bool) -> str:
    if engagement:
        for placeholder, phase in ASL_ENGAGE_PLACEHOLDERS.items():
            text = text.replace(
                placeholder, _placeholder_lambda_arn(ENGAGE_FUNCTION_NAMES[phase])
            )
        text = text.replace(
            "__ENGAGEMENT_STUB_ARN__",
            _placeholder_lambda_arn("oerf-workflow-stub"),
        )
        text = text.replace(
            "__CHANNEL_ROUTER_ARN__", _placeholder_lambda_arn(CHANNEL_ROUTER_NAME)
        )
        return text
    for placeholder, phase in ASL_STEP_PLACEHOLDERS.items():
        text = text.replace(placeholder, _placeholder_lambda_arn(STEP_FUNCTION_NAMES[phase]))
    text = text.replace("__STUB_ARN__", _placeholder_lambda_arn("oerf-workflow-stub"))
    text = text.replace(
        "__ENGAGE_PARTY_ARN__",
        (
            f"arn:aws:states:{_PLACEHOLDER_REGION}:{_PLACEHOLDER_ACCOUNT}:"
            "stateMachine:oerf-engage-party"
        ),
    )
    return text


def _validate_with_step_functions(definition: str, label: str) -> dict:
    if shutil.which("aws") is None:
        pytest.skip("AWS CLI is not on PATH; install aws to validate the state machines")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(definition)
        definition_path = handle.name
    env = os.environ.copy()
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    env.setdefault("AWS_REGION", _PLACEHOLDER_REGION)
    env.setdefault("AWS_DEFAULT_REGION", _PLACEHOLDER_REGION)
    completed = subprocess.run(
        [
            "aws",
            "stepfunctions",
            "validate-state-machine-definition",
            "--definition",
            f"file://{definition_path}",
            "--type",
            "STANDARD",
            "--region",
            _PLACEHOLDER_REGION,
            "--output",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    Path(definition_path).unlink(missing_ok=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        lowered = detail.lower()
        if any(
            token in lowered
            for token in (
                "unable to locate credentials",
                "could not find credentials",
                "the config profile",
                "expiredtoken",
                "token has expired",
                "error when retrieving token",
            )
        ):
            pytest.skip(f"AWS CLI is not configured: {detail}")
        raise AssertionError(f"{label}: AWS CLI failed:\n{detail}")
    body = json.loads(completed.stdout)
    # CLI 2.36 reports ``result`` OK/FAIL. The API field is ``validationStatus``.
    status = body.get("validationStatus", body.get("result"))
    diagnostics = body.get("diagnostics") or []
    errors = [item for item in diagnostics if item.get("severity") == "ERROR"]
    assert status in {"VALID", "OK"} and not errors, (
        f"{label} was {status}:\n" + json.dumps(body, indent=2)
    )
    return body


@pytest.mark.aws
def test_step_functions_accepts_both_definitions() -> None:
    """Live syntax check. CI excludes this marker; it needs a configured AWS CLI."""

    files = (
        (ASL_PATH, False),
        (ASL_PATH.parent / "engage_party.asl.json", True),
    )
    for path, engagement in files:
        text = _definition_with_placeholder_arns(
            path.read_text(encoding="utf-8"), engagement=engagement
        )
        leftover = _PLACEHOLDER_TOKEN.findall(text)
        assert not leftover, f"{path.name} still has placeholders: {leftover}"
        _validate_with_step_functions(text, path.name)


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
        assert gate.startswith("CancelAfter"), step
        assert states[gate]["Choices"] == [
            {
                "Variable": "$.cancel_requested",
                "BooleanEquals": True,
                "Next": "Cancel",
            }
        ]
    assert states["Diagnose"]["Next"] == "CancelAfterDiagnose?"
    assert states["CancelAfterDiagnose?"]["Default"] == "Assess"
    assert states["Assess"]["Next"] == "CancelAfterAssess?"
    assert states["CancelAfterAssess?"]["Default"] == "EngageDriverDiagnosis"
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
    definition = load_definition()
    assert definition["TimeoutSeconds"] == 86400

    # Every declared state must be reachable from StartAt; AWS rejects a deploy
    # with MISSING_TRANSITION_TARGET for orphaned states, and a `Next`-only walk
    # of a hand-edited graph is exactly how the cancel gates were bypassed.
    states = definition["States"]
    reachable = set()
    pending = [definition["StartAt"]]
    while pending:
        name = pending.pop()
        if name in reachable or name not in states:
            continue
        reachable.add(name)
        pending.extend(transition_targets(states[name]))

    orphans = sorted(set(states) - reachable)
    assert not orphans, f"unreachable states: {orphans}"


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
