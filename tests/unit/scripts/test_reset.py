"""Demo reset deletes one tenant and leaves the incident counter alone."""

import json
from datetime import UTC, datetime

import pytest

from oerf.platform.agent.context import DOMAIN
from oerf.platform.incidents.dynamo import DynamoIncidentStore
from oerf.platform.storage.dynamo import key, s
from oerf.platform.tenancy.dynamo_config import DynamoConfigRepository
from scripts.reset import ResetClients, main, reset_tenant
from tests.unit.support.fake_dynamo import FakeDynamoClient

NOW = datetime(2026, 10, 20, 10, 16, tzinfo=UTC)
INCIDENT = "incident"
ROUTING = "routing"
TENANT_TABLE = "tenant"
EVIDENCE = "evidence"


class _Steps:
    def __init__(self, executions: list[dict[str, str]]) -> None:
        self.executions = executions
        self.stopped: list[str] = []

    def list_executions(self, *, stateMachineArn: str, statusFilter: str, **_: object) -> dict:
        return {
            "executions": [
                item
                for item in self.executions
                if item["machine"] == stateMachineArn and item["status"] == statusFilter
            ]
        }

    def describe_execution(self, *, executionArn: str) -> dict[str, str]:
        found = next(item for item in self.executions if item["executionArn"] == executionArn)
        return {"input": found["input"]}

    def stop_execution(self, *, executionArn: str, error: str, cause: str) -> dict[str, str]:
        self.stopped.append(executionArn)
        assert error == "Reset"
        assert cause == "demo_reset"
        for item in self.executions:
            if item["executionArn"] == executionArn:
                item["status"] = "ABORTED"
        return {}


class _Objects:
    def __init__(self, keys: set[str]) -> None:
        self.keys = set(keys)
        self.deleted: list[str] = []

    def list_objects_v2(self, *, Bucket: str, Prefix: str, **_: object) -> dict:
        assert Bucket == EVIDENCE
        found = [name for name in sorted(self.keys) if name.startswith(Prefix)]
        return {"Contents": [{"Key": name} for name in found]}

    def delete_objects(self, *, Bucket: str, Delete: dict) -> dict:
        assert Bucket == EVIDENCE
        for item in Delete["Objects"]:
            self.keys.discard(item["Key"])
            self.deleted.append(item["Key"])
        return {}


def _execution(machine: str, name: str, tenant_id: str) -> dict[str, str]:
    return {
        "executionArn": f"arn:aws:states:us-east-1:0:execution:{machine}:{name}",
        "machine": f"arn:{machine}",
        "status": "RUNNING",
        "input": json.dumps({"tenant_id": tenant_id, "incident_id": name}),
    }


def _clients(
    dynamo: FakeDynamoClient, steps: _Steps, objects: _Objects
) -> ResetClients:
    return ResetClients(
        dynamodb=dynamo,
        s3=objects,
        stepfunctions=steps,
        incident_table=INCIDENT,
        routing_table=ROUTING,
        tenant_table=TENANT_TABLE,
        evidence_bucket=EVIDENCE,
        cold_chain_arn="arn:oerf-cold-chain",
        engage_party_arn="arn:oerf-engage-party",
    )


def _put(client: FakeDynamoClient, table: str, pk: str, sk: str, **attributes: str) -> None:
    item = {**key(pk, sk)}
    for name, value in attributes.items():
        item[name] = s(value)
    client.put_item(TableName=table, Item=item)


def _world() -> tuple[FakeDynamoClient, _Steps, _Objects, ResetClients]:
    dynamo = FakeDynamoClient()
    store = DynamoIncidentStore(dynamo, INCIDENT, tenant_table=TENANT_TABLE, clock=lambda: NOW)
    for shipment in ("PH-1", "PH-2", "PH-3"):
        opened = store.create(tenant_id="t1", shipment_id=shipment, reason="TEMPERATURE_ABOVE_MAX")
        if shipment == "PH-1":
            store.transition("t1", opened.incident_id, from_state="OPEN", to_state="DIAGNOSING")
            store.transition(
                "t1", opened.incident_id, from_state="DIAGNOSING", to_state="ASSESSING"
            )
            store.transition("t1", opened.incident_id, from_state="ASSESSING", to_state="ESCALATED")
    store.create(tenant_id="t2", shipment_id="PH-9", reason="TEMPERATURE_ABOVE_MAX")
    _put(dynamo, INCIDENT, "INGEST#event-1", "META")
    _put(dynamo, INCIDENT, "DEBOUNCE#t1#PH-1#RF-7781", "META")
    _put(dynamo, INCIDENT, "DEBOUNCE#t2#PH-9#RF-7782", "META")
    _put(dynamo, INCIDENT, "DEBOUNCE#t10#PH-1#RF", "META")
    _put(
        dynamo,
        ROUTING,
        "ENGREF#t1#INC-000001#eng-1",
        "OUTMSG#provider-1",
        target_pk="OUTMSG#provider-1",
        target_sk="ENG#t1#INC-000001#eng-1",
    )
    _put(dynamo, ROUTING, "OUTMSG#provider-1", "ENG#t1#INC-000001#eng-1")
    _put(dynamo, ROUTING, "CONTACTENG#t1#contact-1", "ENG#INC-000001#eng-1")
    _put(dynamo, ROUTING, "TENANT#t1#INCIDENT#INC-000001", "MSG#m-1")
    _put(dynamo, ROUTING, "TENANT#t1#INCIDENT#INC-000001", "ACTIVE_TASK_TOKEN", task_token="secret")
    _put(dynamo, ROUTING, "INMSG#t1-msg", "META", tenant_id="t1")
    _put(dynamo, ROUTING, "INMSG#bare", "META")
    _put(dynamo, ROUTING, "INMSG#t2-msg", "META", tenant_id="t2")
    _put(dynamo, ROUTING, "WS#socket-1", "META")
    _put(
        dynamo,
        ROUTING,
        "DEVICE#RF-7781",
        "META",
        tenant_id="t2",
        shipment_id="other",
        asset_id="TRK",
    )
    _put(
        dynamo,
        TENANT_TABLE,
        "TENANT#t1",
        f"CONFIG#{DOMAIN}#v1",
        document=json.dumps(
            {"product_profiles": [{"id": "PP-2-8-45", "excursion_allowance_min": 30}]}
        ),
    )
    steps = _Steps(
        [
            _execution("oerf-engage-party", "child", "t1"),
            _execution("oerf-cold-chain", "t1-INC-000001", "t1"),
            _execution("oerf-cold-chain", "t2-INC-000001", "t2"),
            _execution("oerf-cold-chain", "named-like-t1", "t2"),
        ]
    )
    objects = _Objects(
        {"tenants/t1/photo", "tenants/t10/photo", "tenants/t2/photo", "reports/t1/audit"}
    )
    return dynamo, steps, objects, _clients(dynamo, steps, objects)


def _exists(client: FakeDynamoClient, table: str, pk: str, sk: str) -> bool:
    return "Item" in client.get_item(TableName=table, Key=key(pk, sk))


@pytest.mark.security
def test_all_without_yes_all_refuses_before_any_client() -> None:
    called = False

    def factory(_profile: str) -> object:
        nonlocal called
        called = True
        raise AssertionError("session was built")

    with pytest.raises(SystemExit, match="yes-all"):
        main(["--profile", "cds", "--tenant", "all"], session_factory=factory)

    assert called is False


@pytest.mark.security
def test_dry_run_writes_nothing_and_a_second_apply_is_zero() -> None:
    dynamo, steps, objects, clients = _world()
    before = dynamo.writes

    counted = reset_tenant(clients, "t1", apply=False, reseed=False)

    assert dynamo.writes == before
    assert steps.stopped == []
    assert objects.deleted == []
    assert counted["executions"] == 2
    assert counted["incident_items"] > 0
    assert counted["shipment_locks"] == 3
    assert _exists(dynamo, TENANT_TABLE, "TENANT#t1", "COUNTER#INCIDENT")

    applied = reset_tenant(clients, "t1", apply=True, reseed=False)
    assert steps.stopped == [
        "arn:aws:states:us-east-1:0:execution:oerf-engage-party:child",
        "arn:aws:states:us-east-1:0:execution:oerf-cold-chain:t1-INC-000001",
    ]
    assert objects.deleted == ["tenants/t1/photo"]
    assert "reports/t1/audit" in objects.keys
    assert "tenants/t10/photo" in objects.keys
    assert _exists(dynamo, INCIDENT, "INGEST#event-1", "META")
    assert _exists(dynamo, INCIDENT, "DEBOUNCE#t2#PH-9#RF-7782", "META")
    assert _exists(dynamo, INCIDENT, "DEBOUNCE#t10#PH-1#RF", "META")
    assert not _exists(dynamo, INCIDENT, "DEBOUNCE#t1#PH-1#RF-7781", "META")
    assert _exists(dynamo, ROUTING, "INMSG#bare", "META")
    assert _exists(dynamo, ROUTING, "INMSG#t2-msg", "META")
    assert not _exists(dynamo, ROUTING, "INMSG#t1-msg", "META")
    assert not _exists(dynamo, ROUTING, "OUTMSG#provider-1", "ENG#t1#INC-000001#eng-1")
    assert _exists(dynamo, ROUTING, "WS#socket-1", "META")
    assert _exists(dynamo, ROUTING, "DEVICE#RF-7781", "META")
    assert _text_tenant(dynamo, "DEVICE#RF-7781") == "t2"
    assert _exists(dynamo, TENANT_TABLE, "TENANT#t1", "COUNTER#INCIDENT")
    assert _exists(dynamo, TENANT_TABLE, "TENANT#t1", f"CONFIG#{DOMAIN}#v1")
    assert _exists(dynamo, INCIDENT, "TENANT#t2#SHIP#PH-9", "OPEN_INCIDENT")
    assert not _exists(dynamo, INCIDENT, "TENANT#t1#SHIP#PH-1", "OPEN_INCIDENT")
    assert applied["shipment_locks"] == 3

    again = reset_tenant(clients, "t1", apply=True, reseed=False)
    assert again == dict.fromkeys(again, 0)

    restarted = DynamoIncidentStore(
        dynamo, INCIDENT, tenant_table=TENANT_TABLE, clock=lambda: NOW
    )
    created = restarted.create(
        tenant_id="t1", shipment_id="PH-1", reason="TEMPERATURE_ABOVE_MAX"
    )
    assert created.incident_id == "INC-000004"


@pytest.mark.security
def test_reset_deletes_one_tenants_outbox_rows() -> None:
    dynamo, _steps, _objects, clients = _world()
    _put(dynamo, ROUTING, "TENANT#t1#OUTBOX#INC-000001:driver_ack", "META", tenant_id="t1")
    _put(dynamo, ROUTING, "TENANT#t2#OUTBOX#INC-000001:decision", "META", tenant_id="t2")

    counted = reset_tenant(clients, "t1", apply=False, reseed=False)

    assert counted["routing_rows"] >= 1
    assert _exists(dynamo, ROUTING, "TENANT#t1#OUTBOX#INC-000001:driver_ack", "META")
    assert _exists(dynamo, ROUTING, "TENANT#t2#OUTBOX#INC-000001:decision", "META")

    reset_tenant(clients, "t1", apply=True, reseed=False)

    assert not _exists(dynamo, ROUTING, "TENANT#t1#OUTBOX#INC-000001:driver_ack", "META")
    assert _exists(dynamo, ROUTING, "TENANT#t2#OUTBOX#INC-000001:decision", "META")


@pytest.mark.security
def test_reseed_restores_only_the_target_allowance() -> None:
    dynamo, _steps, _objects, clients = _world()
    reset_tenant(clients, "t1", apply=True, reseed=False)

    first = reset_tenant(clients, "t1", apply=True, reseed=True)
    second = reset_tenant(clients, "t1", apply=True, reseed=True)
    current = DynamoConfigRepository(dynamo, TENANT_TABLE).read_tenant_config("t1", DOMAIN)

    assert first["config"] == 1
    assert second["config"] == 0
    assert second["devices"] == 0
    assert current["product_profiles"][0]["excursion_allowance_min"] == 45
    assert _text_tenant(dynamo, "DEVICE#RF-7781") == "t2"


def _text_tenant(client: FakeDynamoClient, pk: str) -> str:
    item = client.get_item(TableName=ROUTING, Key=key(pk, "META"))["Item"]
    return item["tenant_id"]["S"]
