"""One-way sends: the right party and template, dedupe after success, failure can retry."""

from datetime import UTC, datetime

import pytest

from oerf.platform.channels.engagement import MemoryEngagementStore
from oerf.platform.channels.one_way import (
    RETENTION_SECONDS,
    DynamoFacilityReader,
    DynamoOutbox,
    MemoryOutbox,
    send_one_way,
)
from oerf.platform.channels.providers.base import ProviderResult
from oerf.platform.channels.registry import MemoryPhoneRegistry
from oerf.platform.channels.router import handler
from oerf.platform.storage.dynamo import epoch_seconds, key, n, s
from tests.unit.support.fake_dynamo import FakeDynamoClient

NOW = datetime(2026, 10, 20, 10, 16, tzinfo=UTC)
DRIVER = "+910000000101"
OTHER = "+910000000102"
MESSAGES = {
    "messages": {
        "driver_ack": {
            "whatsapp": "Thanks, noted: {answer}. We're checking the shipment now."
        },
        "status_update": {
            "whatsapp": (
                "Update on shipment {shipment_id}: {status}. "
                "Next step: {next_step}. No action is needed from you."
            )
        },
        "driver_reroute": {
            "whatsapp": (
                "Please take shipment {shipment_id} to {facility_name}, about {minutes} "
                "minutes away. They are expecting you. Directions: {directions} "
                "Tap below when you are on your way."
            )
        },
    }
}


class _Probe:
    def __init__(self, *, simulated: bool = True, succeed: bool = True) -> None:
        self.simulated = simulated
        self.succeed = succeed
        self.calls: list[tuple[str, str, str, tuple[str, ...]]] = []

    def send(
        self,
        recipient: str,
        body: str,
        template_id: str,
        parameters: tuple[str, ...] = (),
    ) -> ProviderResult:
        self.calls.append((recipient, body, template_id, parameters))
        if self.simulated:
            return ProviderResult(
                delivered=False,
                provider_msg_id="wa-1",
                reason="simulated",
                simulated=True,
            )
        if not self.succeed:
            return ProviderResult(delivered=False, reason="delivery_failed")
        return ProviderResult(delivered=True, provider_msg_id="wa-live")


class _Boom:
    def send(self, recipient: str, body: str, template_id: str, parameters: tuple = ()) -> None:
        del recipient, body, template_id, parameters
        raise RuntimeError("provider down +910000000101")


def _ports(probe: object, outbox: object, *, facilities: object | None = None) -> object:
    registry = MemoryPhoneRegistry()
    registry.register_contact("t1", "C-T1-DRV", "driver", DRIVER)
    registry.register_contact("t2", "C-T1-DRV", "driver", OTHER)
    registry.register_contact("t2", "C-T2-DRV", "driver", OTHER)

    class _Ports:
        pass

    ports = _Ports()
    ports.registry = registry
    ports.engagements = MemoryEngagementStore(clock=lambda: NOW)
    ports.providers = {"whatsapp": probe}
    ports.messages = MESSAGES
    ports.parameters = None
    ports.mock_whatsapp = True
    ports.outbox = outbox
    ports.facilities = facilities
    return ports


def _event(message: str, **facts: object) -> dict:
    return {
        "step_name": "EngagementSendOneWay",
        "tenant_id": "t1",
        "incident_id": "INC-000001",
        "role": "driver",
        "message": message,
        "channel": "whatsapp",
        "facts": {
            "shipment": {"driver": "C-T1-DRV", "shipment_id": "PH-2841"},
            **facts,
        },
        "dedupe_key": f"INC-000001:{message}",
    }


def test_ack_uses_the_driver_and_session_text() -> None:
    probe = _Probe()
    outbox = MemoryOutbox(clock=lambda: NOW)
    result = send_one_way(
        _event("driver_ack", answer="Running"),
        ports=_ports(probe, outbox),
    )

    assert result == {"status": "simulated", "provider_msg_id": "wa-1"}
    recipient, body, template_id, parameters = probe.calls[0]
    assert recipient == DRIVER
    assert template_id == ""
    assert parameters == ()
    assert body == "Thanks, noted: Running. We're checking the shipment now."


def test_status_update_parameters_follow_the_template() -> None:
    probe = _Probe()
    result = send_one_way(
        _event("status_update", shipment_id="PH-2841"),
        ports=_ports(probe, MemoryOutbox(clock=lambda: NOW)),
    )

    assert result["status"] == "simulated"
    assert probe.calls[0][3] == (
        "PH-2841",
        "continue to your destination",
        "we're monitoring the temperature",
    )


def test_reroute_uses_facility_coordinates_and_n_a_without_them() -> None:
    client = FakeDynamoClient()
    client.put_item(
        TableName="tenant",
        Item={
            **key("TENANT#t1", "FACILITY#FAC-A"),
            "name": s("Facility A (sample cold store)"),
            "lat": n(0),
            "lon": n(0),
            "drive_min": {"N": "9"},
        },
    )
    stored = client.get_item(TableName="tenant", Key=key("TENANT#t1", "FACILITY#FAC-A"))["Item"]
    stored["lat"] = {"N": "17.47"}
    stored["lon"] = {"N": "78.42"}
    client.put_item(TableName="tenant", Item=stored)
    probe = _Probe()
    reader = DynamoFacilityReader(client, "tenant")
    ports = _ports(probe, MemoryOutbox(clock=lambda: NOW), facilities=reader)
    result = send_one_way(
        _event(
            "driver_reroute",
            shipment_id="PH-2841",
            action={"facility_id": "FAC-A", "drive_min": 9, "kind": "facility"},
        ),
        ports=ports,
    )

    assert result["status"] == "simulated"
    assert probe.calls[0][2] == "driver_reroute"
    assert probe.calls[0][3] == (
        "PH-2841",
        "Facility A (sample cold store)",
        "9",
        "https://maps.google.com/?q=17.47,78.42",
    )

    bare = _Probe()
    missing = send_one_way(
        _event(
            "driver_reroute",
            shipment_id="PH-2841",
            action={"facility_id": "FAC-B", "drive_min": 21, "kind": "facility"},
            facility_name="Facility B (sample cold store)",
        ),
        ports=_ports(bare, MemoryOutbox(clock=lambda: NOW)),
    )
    assert missing["status"] == "simulated"
    assert bare.calls[0][3][3] == "n/a"


@pytest.mark.security
def test_one_way_resolves_only_the_caller_tenant() -> None:
    probe = _Probe()
    ports = _ports(probe, MemoryOutbox(clock=lambda: NOW))
    send_one_way(_event("driver_ack", answer="Running"), ports=ports)

    assert probe.calls[0][0] == DRIVER
    assert OTHER not in probe.calls[0][0]


def test_a_repeat_is_skipped_and_a_failure_is_not_recorded() -> None:
    client = FakeDynamoClient()
    outbox = DynamoOutbox(client, "routing", clock=lambda: NOW)
    probe = _Probe(simulated=False, succeed=False)
    failed = send_one_way(_event("driver_ack", answer="Running"), ports=_ports(probe, outbox))

    assert failed == {"status": "failed", "provider_msg_id": None}
    assert "Item" not in client.get_item(
        TableName="routing",
        Key=key("TENANT#t1#OUTBOX#INC-000001:driver_ack", "META"),
    )

    probe.succeed = True
    sent = send_one_way(_event("driver_ack", answer="Running"), ports=_ports(probe, outbox))
    assert sent == {"status": "sent", "provider_msg_id": "wa-live"}
    item = client.get_item(
        TableName="routing",
        Key=key("TENANT#t1#OUTBOX#INC-000001:driver_ack", "META"),
    )["Item"]
    assert item["expires_at"]["N"] == str(epoch_seconds(NOW) + RETENTION_SECONDS)
    assert item["status"]["S"] == "sent"
    assert "answer" not in item

    again = send_one_way(_event("driver_ack", answer="Running"), ports=_ports(probe, outbox))
    assert again == {"status": "skipped", "provider_msg_id": "wa-live"}
    assert len(probe.calls) == 2


def test_voice_is_skipped_and_a_provider_error_stays_failed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    probe = _Probe()
    outbox = MemoryOutbox(clock=lambda: NOW)
    voice = send_one_way(
        {**_event("driver_call"), "channel": "voice"},
        ports=_ports(probe, outbox),
    )
    assert voice == {"status": "skipped", "provider_msg_id": None}
    assert probe.calls == []
    assert outbox.rows == {}

    boom = send_one_way(
        _event("driver_ack", answer="Running"),
        ports=_ports(_Boom(), MemoryOutbox(clock=lambda: NOW)),
    )
    assert boom == {"status": "failed", "provider_msg_id": None}
    logged = capsys.readouterr().out
    assert "RuntimeError" in logged
    assert "+910000000101" not in logged
    assert "Running" not in logged


def test_router_handler_dispatches_one_way(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "oerf.platform.channels.one_way.send_one_way",
        lambda event, ports=None: {"status": "simulated", "provider_msg_id": "wa-1"},
    )

    assert handler({"step_name": "EngagementSendOneWay"}, None) == {
        "status": "simulated",
        "provider_msg_id": "wa-1",
    }
