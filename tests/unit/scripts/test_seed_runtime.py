"""Runtime seed writes the rows ingest and assess read, and a second run is a no-op."""

import json
from decimal import Decimal

import pytest

from oerf.platform.agent.context import DOMAIN
from oerf.platform.ingest.handler import DynamoShipmentReader
from oerf.platform.storage.dynamo import key
from oerf.platform.tenancy.config_resolver import get_config
from oerf.platform.tenancy.dynamo_config import DynamoConfigRepository
from oerf.platform.tenancy.facilities import DynamoFacilityCatalog
from scripts.seed_runtime import main, seed_runtime, tenants_for
from sim.seed.load import load_seed
from tests.unit.support.fake_dynamo import FakeDynamoClient

TABLE = "tenant"


def test_all_tenants_are_refused() -> None:
    with pytest.raises(SystemExit, match="refused"):
        tenants_for("all")


def test_seed_round_trip_matches_the_workflow_readers() -> None:
    client = FakeDynamoClient()

    counts = seed_runtime(client, TABLE, "t1", apply=True)

    assert counts == {"meta": 1, "shipments": 1, "facilities": 2, "config": 1}
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

    assert again == {"meta": 0, "shipments": 0, "facilities": 0, "config": 0}
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

    assert again == {"meta": 0, "shipments": 0, "facilities": 0, "config": 0}
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

    counts = seed_runtime(client, TABLE, "t1", apply=False)

    assert counts == {"meta": 1, "shipments": 1, "facilities": 2, "config": 1}
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
    assert "would change t1 config 1" in out
    assert client.reads > 0


def test_written_rows_omit_contacts_phones_and_emails() -> None:
    client = FakeDynamoClient()
    seed_runtime(client, TABLE, "t1", apply=True)

    items = client.scan(TableName=TABLE)["Items"]
    assert items
    blob = json.dumps(items)
    assert "CONTACT#" not in blob
    assert "@" not in blob
    assert "PHONE_" not in blob
    for contact in load_seed("t1")["contacts"]:
        assert contact["label"] not in blob
        email = contact.get("email")
        if isinstance(email, str):
            assert email not in blob
    for item in items:
        assert set(item) & {"email", "whatsapp", "sms", "phone", "e164"} == set()


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


def _session(client: _RejectWrites) -> type:
    class _Session:
        def __init__(self, **_: object) -> None:
            pass

        def client(self, service: str, **_: object) -> object:
            if service == "cloudformation":
                return _Stacks()
            return client

    return _Session


class _Stacks:
    def describe_stacks(self, **_: object) -> dict[str, object]:
        return {"Stacks": [{"Outputs": [{"OutputKey": "TenantTableName", "OutputValue": TABLE}]}]}
