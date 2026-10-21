"""Tenant-scoped incident reads and action enqueue for OERF-208."""

import json

from oerf.platform.api.dashboard import (
    DashboardServices,
    MemoryActionQueue,
    MemoryIncidentQuery,
    Principal,
    dispatch,
)
from oerf.platform.api.queries import DynamoIncidentQuery, SqsActionQueue
from oerf.platform.incidents.dynamo import incident_pk
from oerf.platform.storage.dynamo import key, s
from tests.unit.support.fake_dynamo import FakeDynamoClient


def test_list_and_detail_stay_in_one_tenant_and_hide_secrets() -> None:
    client = FakeDynamoClient()
    _incident(client, "t1", "INC-1", "PH-2841", "ASSESSING", allowance=41)
    _incident(client, "t2", "INC-2", "VX-3101", "OPEN")
    client.put_item(
        TableName="incidents",
        Item={
            **key(incident_pk("t1", "INC-1"), "ENG#eng-1"),
            "tenant_id": s("t1"),
            "status": s("WAITING"),
            "task_token": s("secret-token"),
            "document": s('{"task_token": "secret-token"}'),
        },
    )
    client.put_item(
        TableName="incidents",
        Item={
            **key(incident_pk("t1", "INC-1"), "EVT#2026-10-20T09:00:00Z#0001"),
            "tenant_id": s("t1"),
            "previous_state": s(""),
            "state": s("ASSESSING"),
            "reason": s("excursion"),
        },
    )
    queries = DynamoIncidentQuery(client, "incidents")
    services = DashboardServices(queries=queries, queue=MemoryActionQueue())
    principal = Principal("t1", "ops_user", "user-1", True)

    listed = dispatch("GET", "/incidents", principal, None, services)
    detail = dispatch("GET", "/incidents/INC-1", principal, None, services)
    other = dispatch("GET", "/incidents/INC-2", principal, None, services)
    encoded = json.dumps(detail[1])

    assert [row["incident_id"] for row in listed[1]["incidents"]] == ["INC-1"]
    assert detail[1]["engagement_status"] == "WAITING"
    assert detail[1]["timeline"][0]["state"] == "ASSESSING"
    assert detail[1]["evidence"] == {"fault_code": "E17"}
    assert "secret-token" not in encoded
    assert "task_token" not in encoded
    assert other == (404, {"error": "not_found"})


def test_list_pages_after_the_cursor() -> None:
    client = FakeDynamoClient()
    _incident(client, "t1", "INC-1", "PH-1", "OPEN")
    _incident(client, "t1", "INC-2", "PH-2", "OPEN")
    queries = DynamoIncidentQuery(client, "incidents")

    first = queries.list_incidents("t1", limit=1)
    second = queries.list_incidents("t1", limit=1, cursor=first["next_cursor"])

    assert [row["incident_id"] for row in first["incidents"]] == ["INC-1"]
    assert first["next_cursor"] == "INC-1"
    assert [row["incident_id"] for row in second["incidents"]] == ["INC-2"]
    assert second["next_cursor"] is None


def test_a_disallowed_action_is_rejected_and_the_queue_is_untouched() -> None:
    services = DashboardServices(
        queries=MemoryIncidentQuery(
            [{"tenant_id": "t1", "incident_id": "INC-1", "state": "OPEN", "shipment_id": "PH-1"}]
        ),
        queue=MemoryActionQueue(),
    )
    principal = Principal("t1", "ops_user", "user-1", True)

    rejected = dispatch(
        "POST",
        "/incidents/INC-1/actions",
        principal,
        {"action": "pick_option", "option_id": "FACILITY_A"},
        services,
        idempotency_key="act-1",
    )

    assert rejected == (409, {"error": "action_not_allowed"})
    assert services.queue.messages == []


def test_an_unknown_role_cannot_enqueue() -> None:
    services = DashboardServices(
        queries=MemoryIncidentQuery(
            [
                {
                    "tenant_id": "t1",
                    "incident_id": "INC-1",
                    "state": "ASSESSING",
                    "shipment_id": "PH-1",
                }
            ]
        ),
        queue=MemoryActionQueue(),
    )

    rejected = dispatch(
        "POST",
        "/incidents/INC-1/actions",
        Principal("t1", "viewer", "user-1", True),
        {"action": "cancel"},
        services,
    )

    assert rejected == (403, {"error": "forbidden"})
    assert services.queue.messages == []


def test_queue_failure_returns_503_and_does_not_claim_success() -> None:
    services = DashboardServices(
        queries=MemoryIncidentQuery(
            [
                {
                    "tenant_id": "t1",
                    "incident_id": "INC-1",
                    "state": "ASSESSING",
                    "shipment_id": "PH-1",
                }
            ]
        ),
        queue=SqsActionQueue(_FailingQueue(), "https://example/queue.fifo"),
    )

    failed = dispatch(
        "POST",
        "/incidents/INC-1/actions",
        Principal("t1", "ops_user", "user-1", True),
        {"action": "confirm_transfer"},
        services,
        idempotency_key="act-1",
    )

    assert failed == (503, {"error": "queue_unavailable"})
    assert services.queries.get_incident("t1", "INC-1")["state"] == "ASSESSING"


def test_sqs_message_uses_the_idempotency_id_and_omits_the_task_token() -> None:
    client = _RecordingQueue()
    services = DashboardServices(
        queries=MemoryIncidentQuery(
            [
                {
                    "tenant_id": "t1",
                    "incident_id": "INC-1",
                    "state": "ASSESSING",
                    "shipment_id": "PH-1",
                }
            ]
        ),
        queue=SqsActionQueue(client, "https://example/queue.fifo"),
    )

    overridden = dispatch(
        "POST",
        "/incidents/INC-1/actions",
        Principal("t1", "ops_user", "user-1", True),
        {"action": "confirm_transfer", "tenant_id": "t2"},
        services,
        idempotency_key="act-1",
    )
    queued = dispatch(
        "POST",
        "/incidents/INC-1/actions",
        Principal("t1", "ops_user", "user-1", True),
        {"action": "confirm_transfer"},
        services,
        idempotency_key="act-1",
    )
    body = json.loads(client.messages[0]["MessageBody"])

    assert overridden == (403, {"error": "tenant_mismatch"})

    assert queued[0] == 202
    assert client.messages[0]["MessageDeduplicationId"] == queued[1]["action_id"]
    assert client.messages[0]["MessageGroupId"] == "t1"
    assert body["tenant_id"] == "t1"
    assert body["channel"] == "dashboard"
    assert "task_token" not in body


def _incident(
    client: FakeDynamoClient,
    tenant_id: str,
    incident_id: str,
    shipment_id: str,
    state: str,
    allowance: int | None = None,
) -> None:
    item = {
        **key(incident_pk(tenant_id, incident_id), "META"),
        "incident_id": s(incident_id),
        "tenant_id": s(tenant_id),
        "shipment_id": s(shipment_id),
        "state": s(state),
        "evidence": s(json.dumps({"fault_code": "E17", "phone": "+910000000101"})),
    }
    if allowance is not None:
        item["allowance_remaining"] = {"N": str(allowance)}
    client.put_item(TableName="incidents", Item=item)


class _FailingQueue:
    def send_message(self, **kwargs):
        raise RuntimeError("queue down")


class _RecordingQueue:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return {"MessageId": "1"}
