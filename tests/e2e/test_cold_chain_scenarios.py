"""Credential-free connected-step walks for the documented cold-chain scenarios.

The harness starts at the real EventBridge ingest handler, walks the deployed
parent ASL with production step handlers, and simulates nested engagements with
the production cadence and interpretation functions. AWS, provider channels,
and wall-clock waits are intentionally replaced by deterministic in-memory ports.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from oerf.platform.agent.context import StepDependencies
from oerf.platform.agent.envelope import parse_envelope
from oerf.platform.agent.handlers import HANDLERS
from oerf.platform.agent.idempotency import MemoryStepResultStore
from oerf.platform.agent.qa_review import MemoryQAReviewStore
from oerf.platform.cadence.engagement_lifecycle import apply_exhaust_action
from oerf.platform.cadence.interpret import interpret_workflow
from oerf.platform.incidents.debounce import MemoryDebounce
from oerf.platform.incidents.repository import IncidentRepository
from oerf.platform.ingest.handler import IngestDependencies, handler as ingest
from oerf.platform.tenancy.config_resolver import get_config
from oerf.platform.tenancy.facilities import MemoryFacilityCatalog
from sim.scenarios import scenario_events

ROOT = Path(__file__).resolve().parents[2]
ASL = json.loads(
    (ROOT / "domains" / "cold_chain_excursion" / "statemachine.asl.json").read_text(
        encoding="utf-8"
    )
)
ENGAGEMENT_ASL = json.loads(
    (ROOT / "domains" / "cold_chain_excursion" / "engage_party.asl.json").read_text(
        encoding="utf-8"
    )
)
SEED = ROOT / "sim" / "seed" / "t1"
START = datetime(2026, 10, 20, 10, 16, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.current = START

    def __call__(self) -> datetime:
        return self.current


class _Devices:
    def resolve(self, device_id: str) -> tuple[str, str] | None:
        return {
            "RF-7781": ("t1", "PH-2841"),
            "RF-7782": ("t2", "VX-3101"),
        }.get(device_id)


class _Shipments:
    def get(self, tenant_id: str, shipment_id: str) -> dict[str, Any] | None:
        if tenant_id not in {"t1", "t2"}:
            return None
        rows = json.loads(
            (ROOT / "sim" / "seed" / tenant_id / "shipments.json").read_text(
                encoding="utf-8"
            )
        )
        return next(
            (dict(row) for row in rows if row["shipment_id"] == shipment_id), None
        )


class _Configs:
    def get(self, tenant_id: str, domain: str):
        return get_config(tenant_id, domain)


class _Dedupe:
    def __init__(self) -> None:
        self.ids: set[str] = set()

    def register(self, event_id: str, *, now: datetime) -> bool:
        del now
        if event_id in self.ids:
            return False
        self.ids.add(event_id)
        return True

    def forget(self, event_id: str) -> None:
        self.ids.discard(event_id)


class _Executions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def start(self, *, name: str, input: dict[str, Any]) -> str:
        self.calls.append({"name": name, "input": input})
        return f"arn:aws:states:us-east-1:123456789012:execution:offline:{name}"


class _Reports:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    def publish(self, *, tenant_id: str, incident_id: str) -> dict[str, Any]:
        self.published.append((tenant_id, incident_id))
        return {
            "report_key": f"reports/{tenant_id}/{incident_id}/v1.html",
            "s3_uri": f"s3://offline-reports/reports/{tenant_id}/{incident_id}/v1.html",
            "report_version": 1,
            "sha256": "a" * 64,
            "locked": False,
            "generated_at": START.isoformat(),
            "sections": ["incident", "temperature_timeline", "state_timeline"],
        }


class _Scenario:
    """Run the cold-chain workflow with tenant-specific checked-in demo data."""

    def __init__(self, tenant_id: str = "t1") -> None:
        self.tenant_id = tenant_id
        self.seed = ROOT / "sim" / "seed" / tenant_id
        self.shipment_id, self.device_id = {
            "t1": ("PH-2841", "RF-7781"),
            "t2": ("VX-3101", "RF-7782"),
        }[tenant_id]
        self.clock = _Clock()
        self.incidents = IncidentRepository(clock=self.clock)
        self.executions = _Executions()
        self.ingest_dependencies = IngestDependencies(
            devices=_Devices(),
            shipments=_Shipments(),
            configs=_Configs(),
            debounce=MemoryDebounce(clock=self.clock),
            incidents=self.incidents,
            dedupe=_Dedupe(),
            executions=self.executions,
            state_machine_arn="arn:aws:states:us-east-1:123456789012:stateMachine:offline",
            clock=self.clock,
        )
        self.reporter = _Reports()
        config = get_config(tenant_id, "cold_chain_excursion")
        self.qa_reviews = MemoryQAReviewStore()
        self.rise_during_qa = False
        self.qa_rise_recorded = False
        self.qa_recovery_recorded = False
        self.recovery_recorded = False
        self.reading_sequence = 3
        self.steps = StepDependencies(
            incidents=self.incidents,
            results=MemoryStepResultStore(clock=self.clock),
            facilities=MemoryFacilityCatalog(
                json.loads((self.seed / "facilities.json").read_text(encoding="utf-8"))
            ),
            reports=self.reporter,
            qa_reviews=self.qa_reviews,
            clock=self.clock,
        )
        self.config = config
        self.events: list[str] = []
        self.child_outputs: list[dict[str, Any]] = []
        self.one_way: list[tuple[str, str | None]] = []

    def publish(
        self, event_id: str, seq: int, moment: datetime, temperature: float
    ) -> dict[str, Any]:
        event = {
            "id": event_id,
            "source": "sim.reefer",
            "detail-type": "TemperatureReading",
            "detail": {
                "tenant_id": self.tenant_id,
                "shipment_id": self.shipment_id,
                "device_id": self.device_id,
                "ts": moment.isoformat().replace("+00:00", "Z"),
                "temperature_c": temperature,
                "lat": 17.44,
                "lon": 78.38,
                "seq": seq,
            },
        }
        return ingest(event, None, dependencies=self.ingest_dependencies)

    """Process the same EventBridge reading burst through real handlers."""

    def begin(self) -> dict[str, Any]:
        first = self.publish(
            f"{self.tenant_id}-hot-first", 1, START - timedelta(minutes=1), 8.1
        )
        assert first["status"] == "accepted"
        second = self.publish(f"{self.tenant_id}-hot-second", 2, START, 8.6)
        assert second["status"] == "triggered"
        assert len(self.executions.calls) == 1
        return self.executions.calls[0]["input"]

    def stabilize_temperature(self) -> None:
        """Backdate the in-range readings so Verify observes a stable window."""
        self.recovery_recorded = True
        for offset in range(6):
            self.record_temperature(
                START + timedelta(minutes=offset, seconds=1),
                5.2,
                self.reading_sequence,
            )
        self.clock.current = START + timedelta(minutes=5, seconds=6)

    def record_keeps_rising(self) -> None:
        """Publish the remaining documented keeps_rising curve during QA."""
        self.qa_rise_recorded = True
        rows = scenario_events(
            tenant_id=self.tenant_id,
            shipment_id=self.shipment_id,
            scenario="keeps_rising",
            device_id=self.device_id,
            start=START,
            event_bus="offline",
        )
        for row in rows:
            detail = row["Detail"]
            moment = datetime.fromisoformat(detail["ts"].replace("Z", "+00:00"))
            if moment <= START + timedelta(minutes=5, seconds=6):
                continue
            result = self.publish(
                f"{self.tenant_id}-rising-{self.reading_sequence}",
                self.reading_sequence,
                moment,
                float(detail["temperature_c"]),
            )
            self.reading_sequence += 1
            assert result["status"] in {"accepted", "already_open"}
        self.clock.current = START + timedelta(minutes=42, seconds=1)

    def record_keeps_rising_recovery(self) -> None:
        """Record the stable follow-up window after the scripted rising curve."""
        self.qa_recovery_recorded = True
        for offset in range(43, 49):
            self.record_temperature(
                START + timedelta(minutes=offset, seconds=1),
                5.2,
                self.reading_sequence,
            )
        self.clock.current = START + timedelta(minutes=48, seconds=2)

    def record_temperature(self, moment: datetime, temperature: float, seq: int) -> None:
        self.clock.current = moment
        result = self.publish(
            f"{self.tenant_id}-reading-{seq}", seq, moment, temperature
        )
        self.reading_sequence = max(self.reading_sequence, seq + 1)
        assert result["status"] == "accepted"


def _path_present(data: dict[str, Any], path: str) -> bool:
    node: Any = data
    parts = __import__("re").sub(r"\[(\d+)\]", r".\1", path.removeprefix("$." )).split(".")
    for part in parts:
        if isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
            continue
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _path_value(data: dict[str, Any], path: str) -> Any:
    node: Any = data
    parts = __import__("re").sub(r"\[(\d+)\]", r".\1", path.removeprefix("$." )).split(".")
    for part in parts:
        node = node[int(part)] if isinstance(node, list) and part.isdigit() else node[part]
    return node


def _matches(rule: dict[str, Any], data: dict[str, Any]) -> bool:
    if "And" in rule:
        return all(_matches(part, data) for part in rule["And"])
    if "Or" in rule:
        return any(_matches(part, data) for part in rule["Or"])
    if "Not" in rule:
        return not _matches(rule["Not"], data)
    path = rule["Variable"]
    present = _path_present(data, path)
    if "IsPresent" in rule:
        return present is rule["IsPresent"]
    if not present:
        return False
    value = _path_value(data, path)
    if "BooleanEquals" in rule:
        return value is rule["BooleanEquals"]
    if "StringEquals" in rule:
        return value == rule["StringEquals"]
    if "IsNull" in rule:
        return (value is None) is rule["IsNull"]
    raise AssertionError(f"unsupported ASL comparison: {rule}")


def _choice(state: dict[str, Any], data: dict[str, Any]) -> str:
    for choice in state["Choices"]:
        if _matches(choice, data):
            return choice["Next"]
    return state["Default"]


def _assign(data: dict[str, Any], path: str, value: Any) -> None:
    parts = [part for part in path.removeprefix("$").split(".") if part]
    target = data
    for part in parts[:-1]:
        child = target.get(part)
        if not isinstance(child, dict):
            child = {}
            target[part] = child
        target = child
    target[parts[-1]] = value


def _terminal_output(state: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in state.get("Parameters", {}).items():
        if key.endswith(".$"):
            output[key[:-2]] = _path_value(data, value)
        else:
            output[key] = value
    return output


def _engage(
    child_input: dict[str, Any], response_text: str | None, scenario: _Scenario
) -> dict[str, Any]:
    """Walk the real nested ASL with production cadence/reply handlers."""
    from oerf.platform.cadence.engage import handler as cadence_handler

    states = ENGAGEMENT_ASL["States"]
    current = ENGAGEMENT_ASL["StartAt"]
    data = dict(child_input)
    cursor = START
    reply_available = response_text is not None

    for _ in range(64):
        state = states[current]
        if state["Type"] == "Choice":
            current = _choice(state, data)
            if current in {"UseApprovedQAResult", "UseQAResult"}:
                data = dict(data["qa_parallel"][0])
            continue
        if state["Type"] == "Wait":
            if current == "WaitUntilDue":
                cursor += timedelta(seconds=data["cadence"]["delay_seconds"])
            else:
                cursor += timedelta(seconds=data["callback"]["timeout_seconds"])
            current = state["Next"]
            continue
        if state["Type"] in {"Pass", "Succeed"}:
            output = _terminal_output(state, data)
            scenario.child_outputs.append(output)
            return output
        if state["Type"] == "Fail":
            raise AssertionError(f"nested engagement failed at {current}")
        if state["Type"] != "Task":
            raise AssertionError(f"unsupported engagement ASL state {current}: {state['Type']}")

        if state["Resource"].endswith(".waitForTaskToken"):
            cadence = data["cadence"]
            scenario.events.append(f"{data['role']}:{cadence['message']}")
            if reply_available and cadence["awaits_reply"]:
                data["reply"] = {"text": response_text}
                reply_available = False
                current = state["Next"]
            else:
                cursor += timedelta(seconds=cadence["timeout_seconds"])
                timeout = next(
                    catcher
                    for catcher in state.get("Catch", [])
                    if "States.Timeout" in catcher["ErrorEquals"]
                )
                current = timeout["Next"]
            continue

        step_name = state["Parameters"]["Payload"]["step_name"]
        if step_name == "EngagementComputeNextRung":
            result = cadence_handler(
                {"step_name": step_name, "workflow": data}, None
            )
        elif step_name == "EngagementAdvanceRung":
            result = cadence_handler(
                {"step_name": step_name, "workflow": data}, None
            )
        elif step_name == "EngagementInterpret":
            result = interpret_workflow(data, enabled=False)
        elif step_name == "EngagementApplyExhaustAction":
            result = apply_exhaust_action(data)
        elif step_name == "EngagementSendOneWay":
            cadence = data["cadence"]
            scenario.events.append(f"{data['role']}:{cadence['message']}")
            result = {"status": "simulated"}
        else:
            raise AssertionError(f"unexpected engagement step {step_name}")

        result_path = state.get("ResultPath", "$")
        if result_path is None:
            pass
        elif result_path == "$":
            data = result
        else:
            _assign(data, result_path, result)
        current = state["Next"]

    raise AssertionError("nested engagement state machine did not terminate")


def _parent_walk(
    start: dict[str, Any],
    scenario: _Scenario,
    driver_reply: str | None,
    facility_replies: list[str | None],
) -> tuple[str, dict[str, Any]]:
    states = ASL["States"]
    current = ASL["StartAt"]
    data = start
    for _ in range(96):
        scenario.events.append(current)
        state = states[current]
        if state["Type"] == "Succeed":
            return current, data
        if state["Type"] == "Fail":
            return current, data
        if current == "RecordContainment" and scenario.qa_rise_recorded:
            current = state["Next"]
            data["incident_state"] = "CONTAINED"
            data["current_stage"] = "CONTAINED"
            continue
        if state["Type"] == "Choice":
            current = _choice(state, data)
            if current in {"UseApprovedQAResult", "UseQAResult"}:
                pass_state = ASL["States"][current]
                branch = data["qa_parallel"][0]
                data = {key: value for key, value in branch.items() if key != "qa_parallel"}
                current = pass_state["Next"]
                continue
            continue
        if state["Type"] == "Parallel":
            # The QA callback wait runs concurrently with the monitor branch.
            # Record it on every Parallel entry so reopen/retry tests can assert
            # that a fresh QA review started after reverification.
            scenario.events.append("QAReviewWait")
            qa_data = dict(data)
            qa_data["qa_review"] = {
                "approved": True,
                "disposition": "release",
                "reviewer_role": "tenant_admin",
            }
            qa_data["qa"] = {
                "approved": True,
                "disposition": "approved",
                "closure_disposition": "release",
                "reviewer_role": "tenant_admin",
            }
            monitor_data = dict(data)
            if scenario.rise_during_qa and not scenario.qa_rise_recorded:
                scenario.qa_reviews.put(
                    tenant_id=data["tenant_id"],
                    incident_id=data["incident_id"],
                    task_token=f"qa-token-{data['incident_id']}",
                )
                scenario.record_keeps_rising()
                monitor_data["workflow"] = dict(monitor_data.get("workflow", {}))
                monitor_data["workflow"]["qa_monitor_mode"] = True
                monitored = HANDLERS["Verify"](
                    parse_envelope({"step_name": "Verify", "workflow": monitor_data}),
                    scenario.steps,
                )
                monitor_data = monitored.to_event()
                if monitored.containment is None or monitored.containment["status"] != "reopened":
                    raise AssertionError("QA monitor must reopen on the rising curve")
                scenario.qa_rise_recorded = True
                current = state["Catch"][0]["Next"]
                data = monitor_data
                continue

            monitor_data = dict(data)
            monitor_data["incident_state"] = "AWAITING_DISPOSITION"
            monitor_data["current_stage"] = "AWAITING_DISPOSITION"
            monitor_data["workflow"] = dict(monitor_data.get("workflow", {}))
            monitor_data["workflow"]["qa_monitor_mode"] = True
            monitored = HANDLERS["Verify"](
                parse_envelope({"step_name": "Verify", "workflow": monitor_data}),
                scenario.steps,
            )
            monitor_data = monitored.to_event()
            monitor_data["incident_state"] = "AWAITING_DISPOSITION"
            monitor_data["current_stage"] = "AWAITING_DISPOSITION"
            monitor_data.setdefault("workflow", {})["qa_monitor_done"] = True
            data = {**qa_data, "qa_parallel": [qa_data, monitor_data]}
            current = state["Next"]
            continue
        if state["Type"] != "Task":
            raise AssertionError(f"unsupported parent ASL state {current}: {state['Type']}")

        resource = state["Resource"]
        if current == "VerifyLoop" and scenario.rise_during_qa and scenario.qa_rise_recorded:
            if data.get("incident_state") == "REOPENED":
                data["incident_state"] = "VERIFYING"
                data["current_stage"] = "VERIFYING"
            if not scenario.qa_recovery_recorded:
                scenario.record_keeps_rising_recovery()
                incident = scenario.incidents.get(data["tenant_id"], data["incident_id"])
                if incident.state == "REOPENED":
                    scenario.incidents.transition(
                        data["tenant_id"],
                        data["incident_id"],
                        from_state="REOPENED",
                        to_state="VERIFYING",
                    )
                incident = scenario.incidents.get(data["tenant_id"], data["incident_id"])
                if incident.state == "VERIFYING":
                    scenario.incidents.transition(
                        data["tenant_id"],
                        data["incident_id"],
                        from_state="VERIFYING",
                        to_state="CONTAINED",
                    )
        elif current == "VerifyLoop" and not scenario.recovery_recorded:
            # Publish the in-range recovery readings only after the recovery
            # choice is made; earlier readings can make FAC-B infeasible.
            scenario.stabilize_temperature()
        if resource == "arn:aws:states:::states:startExecution.sync:2":
            name = current
            if name == "EngageDriverDiagnosis":
                diagnosis = data["workflow"]["diagnosis"]
                child = {
                    "tenant_id": data["tenant_id"],
                    "incident_id": data["incident_id"],
                    "engagement_id": diagnosis["engagement_id"],
                    "role": "driver",
                    "ask": "diagnose",
                    "policy_id": diagnosis["policy_id"],
                    "facts": data["context"],
                }
                result = _engage(child, driver_reply, scenario)
                _assign(data, state["ResultPath"], result)
            elif name == "EngageFacilityAccept":
                request = data["workflow"]["facility_request"]
                facility_reply = facility_replies.pop(0) if facility_replies else None
                child = {
                    "tenant_id": data["tenant_id"],
                    "incident_id": data["incident_id"],
                    "engagement_id": request["engagement_id"],
                    "role": request["role"],
                    "ask": request["ask"],
                    "policy_id": request["policy_id"],
                    "facts": data["context"],
                }
                result = _engage(child, facility_reply, scenario)
                _assign(data, state["ResultPath"], result)
            else:
                raise AssertionError(f"unexpected nested state {name}")
            current = state["Next"]
            continue

        if current == "Approval":
            raise AssertionError(
                "the configured T2 within-limits path must not wait for approver input"
            )
        if current == "QAReviewWait":
            if scenario.rise_during_qa and not scenario.qa_rise_recorded:
                # Model the ADR-031 monitor while the callback wait is pending.
                # It consumes the documented rising curve and routes the verdict
                # into the same production Verify and HandleReopen handlers.
                qa_token = f"qa-token-{data['incident_id']}"
                scenario.qa_reviews.put(
                    tenant_id=data["tenant_id"],
                    incident_id=data["incident_id"],
                    task_token=qa_token,
                )
                scenario.record_keeps_rising()
                monitored = HANDLERS["Verify"](
                    parse_envelope({"step_name": "Verify", "workflow": data}),
                    scenario.steps,
                )
                assert monitored.containment is not None
                assert monitored.containment["status"] == "reopened"
                assert monitored.incident_state == "AWAITING_DISPOSITION"
                data = monitored.to_event()
                scenario.qa_rise_recorded = True
                current = "HandleReopen"
                continue
            _assign(
                data,
                state["ResultPath"],
                {"approved": True, "disposition": "release", "reviewer_role": "tenant_admin"},
            )
            current = state["Next"]
            continue

        step_name = state["Parameters"]["Payload"]["step_name"]
        if step_name == "EngagementSendOneWay":
            scenario.one_way.append(
                (
                    data.get("workflow", {}).get("action", {}).get("kind", ""),
                    data.get("workflow", {}).get("action", {}).get("facility_id"),
                )
            )
            result = {"status": "simulated"}
        else:
            workflow = data.get("qa_branch", data)
            event = {"step_name": step_name, "workflow": workflow}
            result = HANDLERS[step_name](parse_envelope(event), scenario.steps).to_event()

        result_path = state.get("ResultPath", "$")
        if result_path is None:
            pass
        elif result_path == "$":
            data = result
        else:
            _assign(data, result_path, result)
        if state.get("End"):
            return current, data
        current = state["Next"]
    raise AssertionError(f"cold-chain parent state machine did not terminate: {scenario.events}")


def _walk(
    scenario: _Scenario,
    *,
    driver_reply: str | None,
    facility_replies: list[str | None] | None = None,
) -> tuple[str, dict[str, Any]]:
    start = scenario.begin()
    return _parent_walk(start, scenario, driver_reply, list(facility_replies or []))


@pytest.mark.e2e
def test_happy_scenario_connects_ingest_through_verify_report_qa_and_close() -> None:
    scenario = _Scenario()
    outcome, final = _walk(scenario, driver_reply="Running")

    assert outcome == "WorkflowComplete"
    assert scenario.events[:8] == [
        "OPEN", "CancelAfterOPEN?", "CancelRequested?", "GatherContext",
        "CancelAfterGatherContext?", "Diagnose", "CancelAfterDiagnose?",
        "EngageDriverDiagnosis",
    ]
    assert final["workflow"]["action"]["kind"] == "continue"
    assert final["workflow"]["containment"]["status"] == "contained"
    assert final["workflow"]["report"]["report_key"].startswith("reports/t1/")
    assert final["qa"]["disposition"] == "approved"
    assert scenario.incidents.get("t1", final["incident_id"]).state == "CLOSED"
    assert scenario.reporter.published == [("t1", final["incident_id"])]


@pytest.mark.e2e
def test_keeps_rising_reopens_during_qa_then_reverifies_and_restarts_qa() -> None:
    """C7 / ADR-031: a rise invalidates pending QA and verification restarts."""
    scenario = _Scenario()
    scenario.rise_during_qa = True
    outcome, final = _walk(scenario, driver_reply="Running")

    assert outcome == "WorkflowComplete", (scenario.events, final)
    assert scenario.events.count("QAReviewWait") == 2
    assert scenario.events.count("HandleReopen") == 1
    assert scenario.events.count("VerifyLoop") >= 2
    assert final["workflow"]["reopen_cycle"] == 1
    assert final["workflow"]["containment"]["status"] == "contained"
    assert final["workflow"]["containment"]["previous_status"] == "reopened"
    assert final["qa"]["disposition"] == "approved"
    assert scenario.reporter.published == [("t1", final["incident_id"])] * 2
    assert scenario.qa_reviews.get(
        tenant_id="t1", incident_id=final["incident_id"]
    ) is None
    assert scenario.incidents.get("t1", final["incident_id"]).state == "CLOSED"


@pytest.mark.e2e
def test_stopped_scenario_declines_facility_a_then_uses_facility_b_fallback() -> None:
    scenario = _Scenario()
    outcome, final = _walk(
        scenario,
        driver_reply="Stopped",
        facility_replies=["Decline"],
    )

    assert outcome == "WorkflowComplete", (scenario.events, final)
    assert "Assess" in scenario.events
    assert "SecureFacility" in scenario.events
    assert "EngageFacilityAccept" in scenario.events
    assert "FacilityDecision" in scenario.events
    assert "SecureFacilityFallback" in scenario.events
    assert scenario.child_outputs[0]["intent"] == "unit_stopped"
    assert scenario.child_outputs[1]["intent"] == "decline"
    assert scenario.events.count("EngageFacilityAccept") == 1
    assert final["workflow"]["facility"]["facility_id"] == "FAC-B"
    assert final["workflow"]["containment"]["status"] == "contained"
    assert scenario.incidents.get("t1", final["incident_id"]).state == "CLOSED"


@pytest.mark.e2e
def test_silent_driver_exhausts_real_cadence_then_continue_path_closes() -> None:
    scenario = _Scenario()
    outcome, final = _walk(scenario, driver_reply=None)

    assert outcome == "WorkflowComplete"
    driver_messages = [event for event in scenario.events if event.startswith("driver:")]
    assert driver_messages == [
        "driver:driver_checkin",
        "driver:driver_reminder",
        "driver:driver_reminder",
        "driver:driver_call",
        "driver:supervisor_escalation",
    ]
    assert scenario.child_outputs[0]["status"] == "exhausted"
    assert scenario.child_outputs[0]["exhaust_action"] == {
        "type": "continue_without", "record": "cause_unknown"
    }
    assert "intent" not in final["workflow"]["diagnosis"]["engagement_result"]
    assert final["workflow"]["action"]["kind"] == "continue"
    assert final["workflow"]["containment"]["status"] == "contained"
    assert scenario.incidents.get("t1", final["incident_id"]).state == "CLOSED"


@pytest.mark.e2e
def test_t2_s3_facility_a_decline_escalates_when_no_fallback_is_feasible() -> None:
    scenario = _Scenario(tenant_id="t2")
    outcome, final = _walk(
        scenario,
        driver_reply="Stopped",
        facility_replies=["Decline"],
    )

    assert outcome == "Escalate", (scenario.events, final)
    assert final["workflow"]["assessment"]["recommended_option"] == "FAC-A"
    assert not next(
        option
        for option in final["workflow"]["assessment"]["options"]
        if option["id"] == "FAC-B"
    )["feasible"]
    assert final["workflow"]["facility"]["facility_id"] == "FAC-A"
    assert final["workflow"]["facility"]["next_option_id"] is None
    assert [child.get("intent") for child in scenario.child_outputs] == [
        "unit_stopped",
        "decline",
    ]
    assert scenario.events.count("EngageFacilityAccept") == 1
    assert scenario.events[-1] == "Escalate"
    assert scenario.incidents.get("t2", final["incident_id"]).state == "ESCALATED"


@pytest.mark.e2e
def test_t2_s5_within_limits_route_skips_approver_gate() -> None:
    """T2's limits skip the approval gate; no approver response is required."""
    scenario = _Scenario(tenant_id="t2")
    outcome, final = _walk(
        scenario,
        driver_reply="Stopped",
        facility_replies=["Accept"],
    )

    assert outcome == "WorkflowComplete", (scenario.events, final)
    assert final["workflow"]["assessment"]["route"] == "auto_proceed"
    assert final["workflow"]["assessment"]["recommended_option"] == "FAC-A"
    assert "Approval" not in scenario.events
    assert "approval" not in final["workflow"]
    assert final["workflow"]["facility"]["facility_id"] == "FAC-A"
    assert scenario.incidents.get("t2", final["incident_id"]).state == "CLOSED"
