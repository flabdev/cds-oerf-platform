"""Tenant-scoped incident reads and action enqueue for OERF-208."""

import json

import pytest

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


@pytest.mark.security
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
    assert set(listed[1]["incidents"][0]) == {"incident_id", "state", "stage", "version"}
    assert detail[1]["shipment_id"] == "PH-2841"
    assert detail[1]["config_snapshot"]["fault_code"] == "E17"
    assert "phone" not in detail[1]["config_snapshot"]
    assert "secret-token" not in encoded
    assert "task_token" not in encoded
    assert "pk" not in detail[1]
    assert other == (404, {"error": "not_found"})


def test_detail_returns_timeline_readings_assessment_and_hides_the_token() -> None:
    client = _PagingClient()
    queries = DynamoIncidentQuery(client, "incidents")
    services = DashboardServices(queries=queries, queue=MemoryActionQueue())

    status, body = dispatch(
        "GET",
        "/incidents/INC-1",
        Principal("t1", "ops_user", "user-1", True),
        None,
        services,
    )

    assert status == 200
    assert body["shipment_id"] == "PH-2841"
    assert body["version"] == 6
    assert body["allowance_remaining"] == 35.0
    assert body["evidence"] == {"fault_codes": ["AL02"]}
    assert body["recommendation"]["recommended_option"] == "FACILITY_A"
    assert body["options"][0]["spare_minutes"] == 12.4
    assert body["engagement_status"] == "WAITING"
    assert body["engagement_id"] == "eng-1"
    assert [row["to_state"] for row in body["timeline"]] == ["OPEN", "DIAGNOSING"]
    assert [row["temperature_c"] for row in body["readings"]] == [9.0, 9.0]
    assert client.event_pages == 2
    encoded = json.dumps(body)
    assert "secret-token" not in encoded
    assert "task_token" not in encoded
    assert "step_output" not in body


def test_cancel_does_not_need_an_engagement() -> None:
    services = DashboardServices(
        queries=MemoryIncidentQuery(
            [
                {
                    "tenant_id": "t1",
                    "incident_id": "INC-1",
                    "state": "IN_TRANSIT",
                    "shipment_id": "PH-2841",
                }
            ]
        ),
        queue=MemoryActionQueue(),
    )

    queued = dispatch(
        "POST",
        "/incidents/INC-1/actions",
        Principal("t1", "ops_user", "user-1", True),
        {"action": "cancel", "action_id": "act-1"},
        services,
        idempotency_key="act-1",
    )

    assert queued == (202, {"action_id": "act-1", "status": "queued"})
    assert services.queue.messages[0]["action"] == "cancel"
    assert services.queue.messages[0]["engagement_id"] is None


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


@pytest.mark.security
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

    assert rejected == (400, {"error": "invalid_body"})
    assert services.queue.messages == []


@pytest.mark.security
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
        {"action": "confirm_transfer", "action_id": "act-1", "engagement_id": "ENG-03"},
        services,
        idempotency_key="act-1",
    )

    assert failed == (503, {"error": "queue_unavailable"})
    assert services.queries.get_incident("t1", "INC-1")["state"] == "ASSESSING"


def test_sqs_send_sets_fifo_ids_only_for_a_fifo_url() -> None:
    body = {"tenant_id": "t1", "action_id": "act-1"}
    fifo = _RecordingQueue()
    standard = _RecordingQueue()

    assert SqsActionQueue(fifo, "https://sqs.us-east-1.amazonaws.com/123/in.fifo").send(
        body=body, dedup_id="act-1"
    )
    assert SqsActionQueue(standard, "https://sqs.us-east-1.amazonaws.com/123/in").send(
        body=body, dedup_id="act-1"
    )

    assert fifo.messages[0]["QueueUrl"].endswith(".fifo")
    assert fifo.messages[0]["MessageDeduplicationId"] == "act-1"
    assert fifo.messages[0]["MessageGroupId"] == "t1"
    assert json.loads(fifo.messages[0]["MessageBody"]) == body
    assert standard.messages[0]["QueueUrl"].endswith("/in")
    assert "MessageDeduplicationId" not in standard.messages[0]
    assert "MessageGroupId" not in standard.messages[0]
    assert json.loads(standard.messages[0]["MessageBody"]) == body


@pytest.mark.security
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
        {"action": "confirm_transfer", "action_id": "act-1", "engagement_id": "ENG-03"},
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
    assert body["engagement_id"] == "ENG-03"
    assert body["user_id"] == "user-1"
    assert body["role"] == "ops_user"
    assert body["text"] is None
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
        "reason": s("excursion"),
        "opened_at": s("2026-10-20T09:00:00Z"),
        "GSI1PK": s(f"TENANT#{tenant_id}#STATUS#{state}"),
        "GSI1SK": s("2026-10-20T09:00:00Z"),
        "config_snapshot": s(json.dumps({"fault_code": "E17", "phone": "+910000000101"})),
    }
    if allowance is not None:
        item["allowance_remaining"] = {"N": str(allowance)}
    client.put_item(TableName="incidents", Item=item)


class _PagingClient:
    """One incident whose timeline comes back a page at a time, newest page first."""

    def __init__(self) -> None:
        self.event_pages = 0

    def get_item(self, *, TableName: str, Key: dict, **_: object) -> dict:
        del TableName
        pk = Key["pk"]["S"]
        sk = Key["sk"]["S"]
        if sk == "META":
            return {
                "Item": {
                    **key(pk, "META"),
                    "incident_id": s("INC-1"),
                    "tenant_id": s("t1"),
                    "shipment_id": s("PH-2841"),
                    "state": s("IN_TRANSIT"),
                    "reason": s("excursion"),
                    "opened_at": s("2026-10-20T10:00:00Z"),
                    "event_seq": {"N": "6"},
                    "config_snapshot": s(
                        json.dumps(
                            {
                                "product_profiles": [
                                    {
                                        "id": "PP-2-8-45",
                                        "range_c": [2, 8],
                                        "excursion_allowance_min": 45,
                                    }
                                ]
                            }
                        )
                    ),
                    "evidence_analysis": {
                        "M": {
                            "fault_codes": {"L": [{"S": "AL02"}]},
                            "display_readout_summary": {"S": "secret-token in the summary"},
                        }
                    },
                }
            }
        if sk == "STEP#Assess":
            return {
                "Item": {
                    **key(pk, sk),
                    "decision": s(
                        json.dumps(
                            {
                                "recommended_option": "FACILITY_A",
                                "ranked_options": [
                                    {
                                        "id": "FACILITY_A",
                                        "spare_minutes": 12.4,
                                        "projected_minutes": 20.8,
                                        "feasible": True,
                                    }
                                ],
                                "task_token": "secret-token",
                            }
                        )
                    ),
                    "step_output": s(json.dumps({"task_token": "secret-token"})),
                }
            }
        return {}

    def query(self, **kwargs: object) -> dict:
        values = kwargs["ExpressionAttributeValues"]
        prefix = values[":prefix"]["S"]
        pk = values[":pk"]["S"]
        if prefix == "EVT#":
            self.event_pages += 1
            later = {
                **key(pk, "EVT#2026-10-20T10:16:00Z#0002"),
                "previous_state": s("OPEN"),
                "state": s("DIAGNOSING"),
            }
            earlier = {
                **key(pk, "EVT#2026-10-20T10:00:00Z#0001"),
                "previous_state": s(""),
                "state": s("OPEN"),
            }
            if "ExclusiveStartKey" not in kwargs:
                return {
                    "Items": [later],
                    "LastEvaluatedKey": {"pk": {"S": pk}, "sk": {"S": "next"}},
                }
            return {"Items": [earlier]}
        if prefix == "READ#":
            return {
                "Items": [
                    {
                        **key(pk, "READ#2026-10-20T10:10:00Z#000000000002#aa"),
                        "ts": s("2026-10-20T10:10:00Z"),
                        "temperature_c": {"N": "9"},
                    },
                    {
                        **key(pk, "READ#2026-10-20T10:00:00Z#000000000001#bb"),
                        "ts": s("2026-10-20T10:00:00Z"),
                        "temperature_c": {"N": "9"},
                    },
                ]
            }
        if prefix == "ENG#":
            return {
                "Items": [
                    {
                        **key(pk, "ENG#eng-1"),
                        "status": s("WAITING"),
                        "document": s('{"task_token": "secret-token"}'),
                        "task_token": s("secret-token"),
                    }
                ]
            }
        return {"Items": []}


class _FailingQueue:
    def send_message(self, **kwargs):
        raise RuntimeError("queue down")


class _RecordingQueue:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return {"MessageId": "1"}
