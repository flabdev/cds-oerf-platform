"""In-process PH-2841 walk from GatherContext through real Act."""

import inspect
import json
from datetime import UTC, datetime
from pathlib import Path

from domains.cold_chain_excursion.steps.workflow_stub import handler as secure_facility
from oerf.platform.agent.context import StepDependencies
from oerf.platform.agent.envelope import StepEnvelope, parse_envelope
from oerf.platform.agent.handlers import HANDLERS, diagnose
from oerf.platform.agent.idempotency import MemoryStepResultStore
from oerf.platform.cadence.engage import advance_rung, compute_rung
from oerf.platform.cadence.interpret import interpret_workflow
from oerf.platform.channels.engagement import MemoryEngagementStore
from oerf.platform.channels.engagement_send import send_engagement
from oerf.platform.channels.one_way import MemoryOutbox, send_one_way
from oerf.platform.channels.providers.base import ProviderResult
from oerf.platform.channels.registry import MemoryPhoneRegistry
from oerf.platform.inbound.dedupe import InboundDedupe
from oerf.platform.inbound.handler import InboundDependencies, process_message
from oerf.platform.incidents.repository import IncidentRepository
from oerf.platform.tenancy.config_resolver import get_config
from oerf.platform.tenancy.facilities import MemoryFacilityCatalog

ROOT = Path(__file__).resolve().parents[3]
TENANT = "t1"
SHIPMENT = "PH-2841"
NOW = datetime(2026, 10, 20, 10, 16, tzinfo=UTC)
TOKEN = "task-token-ph2841"
PLACEHOLDER = "+910000000101"
OTHER = "+910000000102"


class _Probe:
    def __init__(self, engagements: MemoryEngagementStore) -> None:
        self.engagements = engagements
        self.calls: list[tuple[str, str, str, tuple[str, ...]]] = []
        self.token_at_send: str | None = None

    def send(
        self,
        recipient: str,
        body: str,
        template_id: str,
        parameters: tuple[str, ...] = (),
    ) -> ProviderResult:
        current = self.engagements.get(TENANT, "INC-000001", self._engagement_id(template_id))
        self.token_at_send = None if current is None else current.task_token
        self.calls.append((recipient, body, template_id, parameters))
        return ProviderResult(
            delivered=False,
            provider_msg_id="whatsapp-1",
            reason="simulated",
            simulated=True,
        )

    def _engagement_id(self, template_id: str) -> str:
        del template_id
        return "ENG-INC-000001-driver-diagnose"


class _Callbacks:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def send_task_success(self, *, taskToken: str, output: str) -> None:
        self.calls.append({"taskToken": taskToken, "output": output})


def test_ph2841_reaches_act_on_the_recommended_option() -> None:
    """A running unit keeps today's ranking, then the continue notice is sent."""

    source = inspect.getsource(diagnose)
    step_source = (ROOT / "domains" / "cold_chain_excursion" / "steps" / "diagnose.py").read_text()
    assert "critical_field" not in source
    assert "critical_field" not in step_source
    assert "FAC-" not in source
    assert "FAC-" not in step_source

    facilities = json.loads((ROOT / "sim" / "seed" / "t1" / "facilities.json").read_text())
    shipment = json.loads((ROOT / "sim" / "seed" / "t1" / "shipments.json").read_text())[0]
    shipment = dict(shipment)
    shipment["product_profile_id"] = shipment["product_profile"]
    messages = json.loads(
        (ROOT / "domains" / "cold_chain_excursion" / "messages.json").read_text()
    )
    config = get_config(TENANT, "cold_chain_excursion")
    dependencies = _dependencies(facilities, config)
    gathered = HANDLERS["GatherContext"](
        _envelope(
            "GatherContext",
            workflow={
                "trigger": {
                    "temperature_c": 8.1,
                    "timestamp": NOW.isoformat(),
                },
                "shipment": shipment,
            },
        ),
        dependencies,
    )
    diagnosed = HANDLERS["Diagnose"](
        gathered.model_copy(update={"step": "Diagnose"}),
        dependencies,
    )
    diagnosis = diagnosed.workflow["diagnosis"]
    assert diagnosis["policy_id"] == config.cadence["driver.diagnose"]
    assert diagnosis["engagement_id"] == "ENG-INC-000001-driver-diagnose"
    assert diagnosed.incident_state == "DIAGNOSING"
    loaded = diagnosed.workflow["context"]["facilities"]
    assert [row["facility_id"] for row in loaded] == [row["facility_id"] for row in facilities]
    assert all("name" not in row for row in loaded)
    assert diagnosed.workflow["ship_time"] == NOW.isoformat()

    child = {
        "tenant_id": TENANT,
        "incident_id": "INC-000001",
        "engagement_id": diagnosis["engagement_id"],
        "role": "driver",
        "ask": "diagnose",
        "policy_id": diagnosis["policy_id"],
        "facts": diagnosed.context,
    }
    planned = compute_rung(child, NOW)
    cadence = planned["cadence"]
    assert cadence["channel"] == "whatsapp"
    assert cadence["message"] == "driver_checkin"
    assert cadence["next_rung"] == 1
    assert cadence["awaits_reply"] is True
    assert cadence["delay_seconds"] == 0
    assert cadence["exhausted"] is False
    assert "final_question" not in cadence
    assert "next_rung" in cadence

    registry = MemoryPhoneRegistry()
    registry.register_contact(TENANT, shipment["driver"], "driver", PLACEHOLDER)
    registry.register_contact("t2", "C-T2-DRV", "driver", OTHER)
    engagements = MemoryEngagementStore(clock=lambda: NOW)
    probe = _Probe(engagements)
    sent = send_engagement(
        {
            "step_name": "EngagementSendViaRouter",
            "task_token": TOKEN,
            "workflow": planned,
        },
        ports=_ports(engagements, registry, probe, messages),
    )
    assert probe.token_at_send == TOKEN
    assert sent["simulated"] is True
    assert sent["delivered"] is False
    assert "task_token" not in sent
    recipient, body, template_id, parameters = probe.calls[0]
    assert recipient == PLACEHOLDER
    assert template_id == "driver_checkin"
    assert parameters == ("PH-2841", "8.1")
    assert "PH-2841" in body and "8.1" in body
    stored = engagements.get(TENANT, "INC-000001", diagnosis["engagement_id"])
    assert stored is not None and stored.status == "WAITING"
    assert stored.task_token == TOKEN
    assert engagements.correlate("whatsapp-1") is not None
    assert engagements.for_address("whatsapp", PLACEHOLDER)

    callbacks = _Callbacks()
    inbound = InboundDependencies(
        dedupe=InboundDedupe(clock=lambda: NOW),
        engagements=engagements,
        callbacks=callbacks,
        registry=registry,
    )
    wrong = _reply(OTHER, "incoming-wrong")
    assert process_message(wrong, inbound) == "ignored"
    assert callbacks.calls == []
    assert stored.status == "WAITING"
    assert process_message(_reply(PLACEHOLDER, "incoming-1"), inbound) == "resumed"
    assert callbacks.calls[0]["taskToken"] == TOKEN
    assert process_message(_reply(PLACEHOLDER, "incoming-1"), inbound) == "duplicate"
    assert len(callbacks.calls) == 1

    interpreted = interpret_workflow(
        {**planned, "reply": {"text": "Running"}},
        enabled=False,
    )
    outbox = MemoryOutbox(clock=lambda: NOW)
    one_way = _ports(engagements, registry, probe, messages)
    one_way.outbox = outbox
    ack = send_one_way(
        {
            "step_name": "EngagementSendOneWay",
            "tenant_id": TENANT,
            "incident_id": "INC-000001",
            "role": "driver",
            "message": "driver_ack",
            "channel": "whatsapp",
            "facts": {"answer": "Running", "shipment": shipment},
            "dedupe_key": "INC-000001:driver_ack",
        },
        ports=one_way,
    )
    assert ack == {"status": "simulated", "provider_msg_id": "whatsapp-1"}
    assert probe.calls[1][1] == "Thanks, noted: Running. We're checking the shipment now."
    assert probe.calls[1][2] == ""
    assert probe.calls[1][3] == ()
    answered_row = engagements.get(TENANT, "INC-000001", diagnosis["engagement_id"])
    assert answered_row is not None and answered_row.task_token == TOKEN
    assert len(probe.calls) == 2
    answered = {
        "status": "answered",
        "intent": interpreted["response"]["intent"],
        "fields": interpreted["response"]["fields"],
        "evidence_refs": interpreted["response"]["evidence_refs"],
        "engagement_id": planned["engagement_id"],
    }
    assert answered["intent"] == "unit_running"
    assert answered["fields"] == {"unit": "running"}
    assessed = _assess(diagnosed, dependencies, answered)
    assessment = assessed.workflow["assessment"]
    assert assessment["elapsed_minutes"] == 0
    assert assessment["needs_approval"] is False
    assert assessment["recommended_option"] == "continue"
    feasible = [item["id"] for item in assessment["options"] if item["feasible"]]
    assert feasible[0] == "continue"
    assert set(feasible) == {"continue", "FAC-A", "FAC-B"}
    parent = assessed
    secured = secure_facility({"step_name": "SecureFacility", "workflow": parent.to_event()}, None)
    assert secured["facility"]["accepted"] is True
    assert secured["incident_state"] == "ASSESSING"
    acted = HANDLERS["Act"](parse_envelope({"step_name": "Act", "workflow": secured}), dependencies)

    action = acted.workflow["action"]
    assert acted.incident_state == "IN_TRANSIT"
    assert action["option_id"] == assessment["recommended_option"]
    assert action["kind"] == "continue"
    assert action["facility_id"] is None
    assert acted.workflow["diagnosis"]["engagement_result"]["status"] == "answered"
    notice = send_one_way(
        {
            "step_name": "EngagementSendOneWay",
            "tenant_id": acted.tenant_id,
            "incident_id": acted.incident_id,
            "role": "driver",
            "message": "status_update",
            "channel": "whatsapp",
            "facts": {
                "shipment_id": acted.shipment_id,
                "shipment": shipment,
                "action": action,
            },
            "dedupe_key": f"{acted.incident_id}:decision",
        },
        ports=one_way,
    )
    assert notice["status"] == "simulated"
    recipient, body, template_id, parameters = probe.calls[2]
    assert recipient == PLACEHOLDER
    assert template_id == "status_update"
    assert parameters == (
        "PH-2841",
        "continue to your destination",
        "we're monitoring the temperature",
    )
    assert "PH-2841" in body and "continue to your destination" in body
    assert answered_row.task_token == TOKEN
    machine = json.loads(
        (ROOT / "domains" / "cold_chain_excursion" / "statemachine.asl.json").read_text()
    )["States"]
    after_act = machine["Act"]["Next"]
    assert after_act == "CancelAfterAct?"
    assert machine[after_act]["Default"] == "DecisionKind?"
    assert machine["DecisionKind?"]["Choices"][0]["Next"] == "NotifyContinue"
    assert machine["NotifyContinue"]["Next"] == "CancelAfterNotifyContinue?"
    assert machine["CancelAfterNotifyContinue?"]["Default"] == "VerifyLoop"


def test_stopped_unit_recommends_facility_a_and_sends_the_reroute() -> None:
    diagnosed, shipment, messages, dependencies, facilities = _open()
    answered = {
        "status": "answered",
        "intent": "unit_stopped",
        "fields": {"unit": "stopped"},
        "evidence_refs": [],
        "engagement_id": diagnosed.workflow["diagnosis"]["engagement_id"],
    }
    assessed = _assess(diagnosed, dependencies, answered)
    assessment = assessed.workflow["assessment"]
    feasible = [item["id"] for item in assessment["options"] if item["feasible"]]
    assert assessment["recommended_option"] == "FAC-A"
    assert assessed.decision["recommended_option"] == "FAC-A"
    assert feasible[0] == "FAC-A"
    assert "continue" not in feasible
    secured = secure_facility(
        {"step_name": "SecureFacility", "workflow": assessed.to_event()}, None
    )
    acted = HANDLERS["Act"](parse_envelope({"step_name": "Act", "workflow": secured}), dependencies)
    action = acted.workflow["action"]
    assert action["kind"] == "facility"
    assert action["option_id"] == "FAC-A"
    assert action["facility_id"] == "FAC-A"
    probe, ports = _sender(shipment, messages, facilities)
    notice = send_one_way(
        {
            "step_name": "EngagementSendOneWay",
            "tenant_id": TENANT,
            "incident_id": "INC-000001",
            "role": "driver",
            "message": "driver_reroute",
            "channel": "whatsapp",
            "facts": {
                "shipment_id": SHIPMENT,
                "shipment": shipment,
                "action": action,
            },
            "dedupe_key": "INC-000001:decision",
        },
        ports=ports,
    )
    assert notice["status"] == "simulated"
    _recipient, body, template_id, parameters = probe.calls[0]
    assert template_id == "driver_reroute"
    assert parameters == (
        "PH-2841",
        "Facility A (sample cold store)",
        "9",
        "https://maps.google.com/?q=17.47,78.42",
    )
    assert "Facility A (sample cold store)" in body


def test_silent_driver_keeps_todays_ranking_and_sends_status_update() -> None:
    diagnosed, shipment, messages, dependencies, _facilities = _open()
    diagnosis = diagnosed.workflow["diagnosis"]
    planned = {
        "tenant_id": TENANT,
        "incident_id": "INC-000001",
        "engagement_id": diagnosis["engagement_id"],
        "role": "driver",
        "ask": "diagnose",
        "policy_id": diagnosis["policy_id"],
        "facts": diagnosed.context,
    }
    for _ in range(8):
        planned = compute_rung(planned, NOW)
        if planned["cadence"]["exhausted"]:
            break
        planned = advance_rung(planned, NOW)
    else:
        raise AssertionError("silence did not exhaust the engagement")
    assert planned["cadence"]["exhausted"] is True
    assert "intent" not in planned
    exhausted = {
        "status": "exhausted",
        "exhaust_action": planned["exhaust_action"],
        "engagement_id": planned["engagement_id"],
    }
    assert "intent" not in exhausted
    assessed = _assess(diagnosed, dependencies, exhausted)
    assessment = assessed.workflow["assessment"]
    feasible = [item["id"] for item in assessment["options"] if item["feasible"]]
    assert assessment["recommended_option"] == "continue"
    assert feasible[0] == "continue"
    secured = secure_facility(
        {"step_name": "SecureFacility", "workflow": assessed.to_event()}, None
    )
    acted = HANDLERS["Act"](parse_envelope({"step_name": "Act", "workflow": secured}), dependencies)
    action = acted.workflow["action"]
    assert action["kind"] == "continue"
    probe, ports = _sender(shipment, messages, _facilities)
    notice = send_one_way(
        {
            "step_name": "EngagementSendOneWay",
            "tenant_id": TENANT,
            "incident_id": "INC-000001",
            "role": "driver",
            "message": "status_update",
            "channel": "whatsapp",
            "facts": {"shipment_id": SHIPMENT, "shipment": shipment, "action": action},
            "dedupe_key": "INC-000001:decision",
        },
        ports=ports,
    )
    assert notice["status"] == "simulated"
    _recipient, _body, template_id, parameters = probe.calls[0]
    assert template_id == "status_update"
    assert parameters == (
        "PH-2841",
        "continue to your destination",
        "we're monitoring the temperature",
    )


def _open():
    facilities = json.loads((ROOT / "sim" / "seed" / "t1" / "facilities.json").read_text())
    shipment = dict(json.loads((ROOT / "sim" / "seed" / "t1" / "shipments.json").read_text())[0])
    shipment["product_profile_id"] = shipment["product_profile"]
    messages = json.loads((ROOT / "domains" / "cold_chain_excursion" / "messages.json").read_text())
    dependencies = _dependencies(facilities, get_config(TENANT, "cold_chain_excursion"))
    gathered = HANDLERS["GatherContext"](
        _envelope(
            "GatherContext",
            workflow={
                "trigger": {"temperature_c": 8.1, "timestamp": NOW.isoformat()},
                "shipment": shipment,
            },
        ),
        dependencies,
    )
    diagnosed = HANDLERS["Diagnose"](
        gathered.model_copy(update={"step": "Diagnose"}),
        dependencies,
    )
    return diagnosed, shipment, messages, dependencies, facilities


def _assess(diagnosed, dependencies, result):
    workflow = dict(diagnosed.workflow)
    workflow["diagnosis"] = {**workflow["diagnosis"], "engagement_result": result}
    return HANDLERS["Assess"](
        diagnosed.model_copy(update={"step": "Assess", "workflow": workflow}),
        dependencies,
    )


class _FacilityRows:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = {row["facility_id"]: row for row in rows}

    def get(self, tenant_id: str, facility_id: str) -> dict:
        assert tenant_id == TENANT
        return self._rows[facility_id]


def _sender(shipment, messages, facilities):
    registry = MemoryPhoneRegistry()
    registry.register_contact(TENANT, shipment["driver"], "driver", PLACEHOLDER)
    engagements = MemoryEngagementStore(clock=lambda: NOW)
    probe = _Probe(engagements)
    ports = _ports(engagements, registry, probe, messages)
    ports.facilities = _FacilityRows(facilities)
    ports.outbox = MemoryOutbox(clock=lambda: NOW)
    return probe, ports


def _dependencies(facilities: list[dict], config) -> StepDependencies:
    incidents = IncidentRepository(clock=lambda: NOW)
    incidents.create(
        tenant_id=TENANT,
        shipment_id=SHIPMENT,
        reason="temperature excursion",
        config_version=config.version,
        config_snapshot=config.model_dump(),
    )
    return StepDependencies(
        incidents=incidents,
        results=MemoryStepResultStore(clock=lambda: NOW),
        facilities=MemoryFacilityCatalog(facilities),
        clock=lambda: NOW,
    )


def _envelope(step: str, **values) -> StepEnvelope:
    return StepEnvelope(
        step=step,
        tenant_id=TENANT,
        incident_id="INC-000001",
        shipment_id=SHIPMENT,
        **values,
    )


def _ports(engagements, registry, probe, messages):
    class _Ports:
        pass

    ports = _Ports()
    ports.engagements = engagements
    ports.registry = registry
    ports.providers = {"whatsapp": probe}
    ports.messages = messages
    ports.parameters = None
    ports.mock_whatsapp = True
    return ports


def _reply(sender: str, message_id: str) -> dict:
    return {
        "channel": "whatsapp",
        "provider_msg_id": message_id,
        "sender": sender,
        "received_at": "2026-10-20T10:16:00Z",
        "text": "Running",
        "type": "text",
        "media": [],
        "context_msg_id": "whatsapp-1",
    }
