"""Runtime seed writes the rows ingest and assess read, and a second run is a no-op."""

import json
import re
from decimal import Decimal

import pytest

from oerf.platform.agent.context import DOMAIN
from oerf.platform.channels.address import resolve_party_address
from oerf.platform.channels.engagement_send import _request
from oerf.platform.channels.registry import DynamoPhoneRegistry
from oerf.platform.ingest.handler import DynamoShipmentReader
from oerf.platform.storage.dynamo import key
from oerf.platform.tenancy.config_resolver import get_config
from oerf.platform.tenancy.dynamo_config import DynamoConfigRepository
from oerf.platform.tenancy.facilities import DynamoFacilityCatalog
from scripts.demo_reply import _address
from scripts.seed_runtime import SYNTHETIC_E164, main, seed_live_contacts, seed_runtime, tenants_for
from tests.unit.support.fake_dynamo import FakeDynamoClient

TABLE = "tenant"


def test_all_tenants_are_refused() -> None:
    with pytest.raises(SystemExit, match="refused"):
        tenants_for("all")


def test_seed_round_trip_matches_the_workflow_readers() -> None:
    client = FakeDynamoClient()

    counts = seed_runtime(client, TABLE, "t1", apply=True)

    assert counts == {
        "meta": 1,
        "shipments": 1,
        "facilities": 2,
        "contacts": 5,
        "phones": 0,
        "config": 1,
    }
    repository = DynamoConfigRepository(client, TABLE)
    assert repository.subscribed_domains("t1") == {"cold_chain_excursion"}
    config = get_config("t1", DOMAIN, repository=repository)
    profile = config.product_profile("PP-2-8-45")
    assert profile.excursion_allowance_min == 45
    assert profile.range_c == [2, 8]
    assert config.assessment.unload_min == 4
    facilities = {
        row["facility_id"]: row
        for row in DynamoFacilityCatalog(client, TABLE).list_facilities("t1")
    }
    assert facilities["FAC-A"]["drive_min"] == 9.0
    assert facilities["FAC-B"]["drive_min"] == 21.0
    assert facilities["FAC-A"]["approved"] is True
    assert facilities["FAC-A"]["capacity_available"] is True
    assert facilities["FAC-B"]["approved"] is True
    assert facilities["FAC-B"]["capacity_available"] is True
    shipment = DynamoShipmentReader(client, TABLE).get("t1", "PH-2841")
    assert shipment is not None
    assert shipment["product_profile"] == "PP-2-8-45"
    assert shipment["eta_min_at_excursion"] == 46
    assert isinstance(shipment["eta_min_at_excursion"], int)
    assert not isinstance(shipment["eta_min_at_excursion"], bool)
    assert shipment["value_usd"] == 180000
    assert shipment["storage_phase"] is False
    facility = client.get_item(
        TableName=TABLE, Key=key("TENANT#t1", "FACILITY#FAC-A")
    )["Item"]
    assert facility["range_c"]["L"] == [{"N": "2"}, {"N": "8"}]
    assert "S" not in facility["range_c"]
    meta = client.get_item(TableName=TABLE, Key=key("TENANT#t1", "META"))["Item"]
    assert json.loads(meta["domains"]["S"]) == ["cold_chain_excursion"]


def test_second_seed_writes_nothing() -> None:
    client = FakeDynamoClient()
    seed_runtime(client, TABLE, "t1", apply=True)
    writes = client.writes

    again = seed_runtime(client, TABLE, "t1", apply=True)

    assert again == {
        "meta": 0,
        "shipments": 0,
        "facilities": 0,
        "contacts": 0,
        "phones": 0,
        "config": 0,
    }
    assert client.writes == writes


def test_equivalent_decimals_count_as_unchanged() -> None:
    client = FakeDynamoClient()
    seed_runtime(client, TABLE, "t1", apply=True)
    facility = client.get_item(TableName=TABLE, Key=key("TENANT#t1", "FACILITY#FAC-A"))["Item"]
    facility["drive_min"] = {"N": "9.0"}
    facility["distance_km"] = {"N": "6.80"}
    facility["range_c"] = {"L": [{"N": "2.0"}, {"N": "8.0"}]}
    client.put_item(TableName=TABLE, Item=facility)
    version = client.get_item(
        TableName=TABLE, Key=key("TENANT#t1", "CONFIG#cold_chain_excursion#v1")
    )["Item"]
    document = json.loads(version["document"]["S"])
    document["product_profiles"][0]["excursion_allowance_min"] = 45.0
    version["document"] = {"S": json.dumps(document)}
    client.put_item(TableName=TABLE, Item=version)
    writes = client.writes

    again = seed_runtime(client, TABLE, "t1", apply=True)

    assert again == {
        "meta": 0,
        "shipments": 0,
        "facilities": 0,
        "contacts": 0,
        "phones": 0,
        "config": 0,
    }
    assert client.writes == writes
    assert Decimal("9.0") == Decimal(facility["drive_min"]["N"])


def test_first_run_publishes_when_no_config_is_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeDynamoClient()

    counts = seed_runtime(client, TABLE, "t1", apply=True)

    assert counts["config"] == 1
    assert client.transactions

    def _missing(self: object, tenant_id: str, domain: str) -> None:
        return None

    monkeypatch.setattr(DynamoConfigRepository, "read_tenant_config", _missing)
    empty = FakeDynamoClient()
    again = seed_runtime(empty, TABLE, "t1", apply=True)
    assert again["config"] == 1
    assert empty.transactions


def test_dry_run_reads_and_does_not_write() -> None:
    client = _RejectWrites()

    counts = seed_runtime(client, TABLE, "t1", apply=False, routing_table="routing")

    assert counts == {
        "meta": 1,
        "shipments": 1,
        "facilities": 2,
        "contacts": 5,
        "phones": 5,
        "config": 1,
    }
    assert client.reads > 0


def test_main_dry_run_prints_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = _RejectWrites()
    monkeypatch.setattr("boto3.Session", _session(client))

    assert main(["--profile", "Fission-OERF", "--tenant", "t1"]) == 0

    out = capsys.readouterr().out
    assert "would change t1 meta 1" in out
    assert "would change t1 shipments 1" in out
    assert "would change t1 facilities 2" in out
    assert "would change t1 contacts 5" in out
    assert "would change t1 phones 5" in out
    assert "would change t1 config 1" in out
    assert client.reads > 0


def test_mocked_driver_address_matches_the_router() -> None:
    client = FakeDynamoClient()
    seed_runtime(client, TABLE, "t1", apply=True, routing_table="routing")
    registry = DynamoPhoneRegistry(client, "routing", TABLE)

    contact = registry.get_contact("t1", "C-T1-DRV")
    assert contact is not None
    assert contact.role == "driver"
    assert contact.opt_in is True
    assert contact.e164 == SYNTHETIC_E164["driver"]
    mocked = resolve_party_address(
        "t1",
        "driver",
        "C-T1-DRV",
        registry=registry,
        parameters=None,
        live=False,
    )
    reply = _address(
        tenant_id="t1",
        role="driver",
        contact_id="C-T1-DRV",
        live=False,
        registry=registry,
        parameters=None,
    )
    routed, _rung, _deadline = _request(
        {
            "tenant_id": "t1",
            "incident_id": "INC-000001",
            "engagement_id": "ENG-1",
            "role": "driver",
            "facts": {"shipment": {"driver": "C-T1-DRV", "shipment_id": "PH-2841"}},
            "cadence": {
                "message": "driver_checkin",
                "channel": "whatsapp",
                "next_rung": 1,
                "deadline": "2026-10-27T12:00:00+00:00",
            },
        },
        _Ports(registry),
    )
    assert mocked == reply == routed.recipient == "+15550100001"
    owned = registry.resolve_sender("15550100001")
    assert owned.unknown is False
    assert owned.matches[0].contact_id == "C-T1-DRV"
    assert owned.matches[0].e164 == "+15550100001"


def test_written_numbers_stay_in_the_fictional_range() -> None:
    client = FakeDynamoClient()
    for tenant_id in ("t1", "t2", "sbx"):
        seed_runtime(client, TABLE, tenant_id, apply=True, routing_table="routing")

    items = client.scan(TableName=TABLE)["Items"] + client.scan(TableName="routing")["Items"]
    blob = json.dumps(items)
    assert "PHONE_" not in blob
    assert "+91" not in blob
    assert "qa-t2@example.com" not in blob
    assert "qa-sbx@example.com" not in blob
    numbers = re.findall(r"\+\d{8,}", blob)
    assert numbers
    assert all(re.fullmatch(r"\+15550100\d{3}", number) for number in numbers)
    qa = client.get_item(TableName=TABLE, Key=key("TENANT#t1", "CONTACT#C-T1-QA"))["Item"]
    assert qa["email"]["S"] == "qa@example.com"
    assert qa["opt_in"]["S"] == "true"


class _Ports:
    def __init__(self, registry: DynamoPhoneRegistry) -> None:
        self.registry = registry
        self.parameters = None
        self.mock_whatsapp = True


class _RejectWrites:
    """Reads succeed on an empty table. Any write fails the dry-run test."""

    def __init__(self) -> None:
        self.reads = 0

    def get_item(self, **_: object) -> dict[str, object]:
        self.reads += 1
        return {}

    def put_item(self, **_: object) -> None:
        raise AssertionError("put_item")

    def transact_write_items(self, **_: object) -> None:
        raise AssertionError("transact_write_items")


def _session(client: _RejectWrites, *, ssm_raises: bool = False) -> type:
    class _Session:
        def __init__(self, **_: object) -> None:
            pass

        def client(self, service: str, **_: object) -> object:
            if service == "ssm" and ssm_raises:
                raise AssertionError("ssm")
            if service == "cloudformation":
                return _Stacks()
            return client

    return _Session


_LIVE = {
    "/oerf/demo-numbers/t1/driver": "+15550100901",
    "/oerf/demo-numbers/t1/ops_manager": "15550100902",
    "/oerf/demo-numbers/t1/qa": "+15550100904",
    "/oerf/demo-numbers/t1/facility": "+15550100903",
}


class _Ssm:
    """Returns decrypted SecureString values. Records the call, not the value."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.calls: list[tuple[str, bool]] = []

    def get_parameter(self, *, Name: str, WithDecryption: bool) -> dict[str, object]:
        self.calls.append((Name, WithDecryption))
        if Name not in self.values:
            raise _MissingParameter(Name)
        return {"Parameter": {"Value": self.values[Name], "Type": "SecureString"}}


def test_live_contacts_point_the_ssm_number_at_the_seeded_contact() -> None:
    client = FakeDynamoClient()
    ssm = _Ssm(_LIVE)
    seed_runtime(client, TABLE, "t1", apply=True, routing_table="routing")
    written = client.writes

    counts = seed_live_contacts(client, "routing", "t1", ssm, apply=True)

    assert counts == {"driver": 1, "ops_manager": 1, "qa": 1, "facility": 2}
    assert client.writes == written + 5
    assert ssm.calls == [
        ("/oerf/demo-numbers/t1/driver", True),
        ("/oerf/demo-numbers/t1/ops_manager", True),
        ("/oerf/demo-numbers/t1/qa", True),
        ("/oerf/demo-numbers/t1/facility", True),
    ]
    registry = DynamoPhoneRegistry(client, "routing", TABLE)
    driver = registry.resolve_sender("15550100901")
    assert [(match.tenant_id, match.contact_id, match.role) for match in driver.matches] == [
        ("t1", "C-T1-DRV", "driver")
    ]
    assert driver.matches[0].opt_in is True
    assert driver.matches[0].e164 == "+15550100901"
    assert registry.get_contact("t1", "C-T1-DRV").e164 == SYNTHETIC_E164["driver"]
    assert registry.resolve_sender("+15550100902").matches[0].contact_id == "C-T1-OPS"
    facilities = {match.contact_id for match in registry.resolve_sender("+15550100903").matches}
    assert facilities == {"C-T1-FACA", "C-T1-FACB"}
    again = seed_live_contacts(client, "routing", "t1", ssm, apply=True)
    assert again == {"driver": 0, "ops_manager": 0, "qa": 0, "facility": 0}
    assert client.writes == written + 5


def test_live_contacts_dry_run_reads_ssm_and_does_not_write() -> None:
    ssm = _Ssm(_LIVE)

    counts = seed_live_contacts(_RejectWrites(), "routing", "t1", ssm, apply=False)

    assert counts["driver"] == 1
    assert counts["facility"] == 2
    assert ssm.calls[0] == ("/oerf/demo-numbers/t1/driver", True)


def test_live_contacts_flag_prints_role_and_count_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeDynamoClient()
    ssm = _Ssm(_LIVE)
    monkeypatch.setattr("boto3.Session", _live_session(client, ssm))

    assert main(["--profile", "Fission-OERF", "--tenant", "t1", "--live-contacts"]) == 0

    out = capsys.readouterr().out
    assert "would change t1 phones 5" in out
    assert "would change t1 live-contacts driver 1" in out
    assert "would change t1 live-contacts facility 2" in out
    assert client.writes == 0
    for value in _LIVE.values():
        assert value not in out
        assert value.lstrip("+") not in out


def test_default_run_does_not_read_ssm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("boto3.Session", _session(_RejectWrites(), ssm_raises=True))

    assert main(["--profile", "Fission-OERF", "--tenant", "t1"]) == 0


def test_unreadable_demo_number_does_not_echo_the_value() -> None:
    secret = "+15550100901"

    class _Leak:
        def get_parameter(self, *, Name: str, WithDecryption: bool) -> dict[str, object]:
            raise RuntimeError(f"Value={secret}")

    with pytest.raises(SystemExit, match="could not read /oerf/demo-numbers/t1/driver") as raised:
        seed_live_contacts(FakeDynamoClient(), "routing", "t1", _Leak(), apply=True)

    assert secret not in str(raised.value)
    assert "15550100901" not in str(raised.value)


def test_empty_demo_number_does_not_echo_the_value() -> None:
    raw = "not-a-phone"
    ssm = _Ssm({"/oerf/demo-numbers/t1/driver": raw})

    with pytest.raises(SystemExit, match="could not read /oerf/demo-numbers/t1/driver") as raised:
        seed_live_contacts(FakeDynamoClient(), "routing", "t1", ssm, apply=False)

    assert raw not in str(raised.value)


def test_blank_demo_number_exits() -> None:
    ssm = _Ssm({"/oerf/demo-numbers/t1/driver": "   "})

    with pytest.raises(SystemExit, match="could not read /oerf/demo-numbers/t1/driver"):
        seed_live_contacts(FakeDynamoClient(), "routing", "t1", ssm, apply=False)


def test_missing_parameter_is_skipped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeDynamoClient()
    present = {
        "/oerf/demo-numbers/t1/driver": "+15550100901",
        "/oerf/demo-numbers/t1/ops_manager": "+15550100902",
        "/oerf/demo-numbers/t1/facility": "+15550100903",
    }
    ssm = _Ssm(present)

    counts = seed_live_contacts(client, "routing", "t1", ssm, apply=True)

    out = capsys.readouterr().out
    assert counts == {"driver": 1, "ops_manager": 1, "facility": 2}
    assert "qa" not in counts
    assert client.writes == 4
    assert out == "skipped t1 live-contacts qa (no parameter)\n"
    for value in present.values():
        assert value not in out


def test_other_ssm_errors_still_exit() -> None:
    class _Denied(Exception):
        response = {"Error": {"Code": "AccessDeniedException", "Message": "nope"}}

        def get_parameter(self, *, Name: str, WithDecryption: bool) -> dict[str, object]:
            raise _Denied()

    denied = _Denied()
    with pytest.raises(SystemExit, match="could not read /oerf/demo-numbers/t1/driver"):
        seed_live_contacts(FakeDynamoClient(), "routing", "t1", denied, apply=False)


def test_roles_filter_reads_only_the_named_roles(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeDynamoClient()
    ssm = _Ssm(_LIVE)
    monkeypatch.setattr("boto3.Session", _live_session(client, ssm))

    assert (
        main(
            [
                "--profile",
                "Fission-OERF",
                "--tenant",
                "t1",
                "--yes",
                "--live-contacts",
                "--roles",
                "driver, ops_manager",
            ]
        )
        == 0
    )

    assert [name for name, decrypted in ssm.calls] == [
        "/oerf/demo-numbers/t1/driver",
        "/oerf/demo-numbers/t1/ops_manager",
    ]
    assert all(decrypted for _, decrypted in ssm.calls)
    out = capsys.readouterr().out
    assert "applied t1 live-contacts driver 1" in out
    assert "applied t1 live-contacts ops_manager 1" in out
    assert "live-contacts qa" not in out
    assert "live-contacts facility" not in out
    for value in _LIVE.values():
        assert value not in out
        assert value.lstrip("+") not in out
    registry = DynamoPhoneRegistry(client, "routing", TABLE)
    assert registry.resolve_sender("+15550100901").matches[0].contact_id == "C-T1-DRV"
    assert registry.resolve_sender("+15550100903").unknown is True


def test_unknown_role_is_refused_before_ssm() -> None:
    ssm = _Ssm(_LIVE)

    with pytest.raises(SystemExit, match="unknown role ops"):
        seed_live_contacts(
            FakeDynamoClient(), "routing", "t1", ssm, apply=False, roles=("driver", "ops")
        )

    assert ssm.calls == []


def test_roles_without_live_contacts_is_refused() -> None:
    with pytest.raises(SystemExit, match="--roles needs --live-contacts"):
        main(["--profile", "Fission-OERF", "--tenant", "t1", "--roles", "driver"])


class _MissingParameter(Exception):
    def __init__(self, name: str) -> None:
        self.response = {"Error": {"Code": "ParameterNotFound", "Message": name}}


def _live_session(client: FakeDynamoClient, ssm: _Ssm) -> type:
    class _Session:
        def __init__(self, **_: object) -> None:
            pass

        def client(self, service: str, **_: object) -> object:
            if service == "cloudformation":
                return _Stacks()
            if service == "ssm":
                return ssm
            return client

    return _Session


class _Stacks:
    def describe_stacks(self, **_: object) -> dict[str, object]:
        return {
            "Stacks": [
                {
                    "Outputs": [
                        {"OutputKey": "TenantTableName", "OutputValue": TABLE},
                        {"OutputKey": "RoutingTableName", "OutputValue": "routing"},
                    ]
                }
            ]
        }
