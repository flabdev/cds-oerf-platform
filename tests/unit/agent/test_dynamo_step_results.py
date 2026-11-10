"""Dynamo adapter tests for replayable workflow step envelopes."""

from datetime import UTC, datetime

from oerf.platform.agent.dynamo_step_results import DynamoStepResultStore
from oerf.platform.agent.qa_review import (
    QA_REVIEW_TTL_SECONDS,
    DynamoQAReviewStore,
    MemoryQAReviewStore,
)
from tests.unit.support.fake_dynamo import FakeDynamoClient

FIXED = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)


def test_qa_review_token_round_trips_and_is_tenant_scoped() -> None:
    client = FakeDynamoClient()
    store = DynamoQAReviewStore(client, "incident", clock=lambda: FIXED)
    store.put(tenant_id="t1", incident_id="INC-1", task_token="private-token")

    item = client.get_item(
        TableName="incident",
        Key={"pk": {"S": "TENANT#t1#INC#INC-1"}, "sk": {"S": "QA_REVIEW"}},
    )["Item"]
    assert int(item["expires_at"]["N"]) == int(FIXED.timestamp()) + QA_REVIEW_TTL_SECONDS
    assert store.get(tenant_id="t1", incident_id="INC-1") == "private-token"
    assert store.get(tenant_id="t2", incident_id="INC-1") is None
    store.delete(tenant_id="t1", incident_id="INC-1")
    assert store.get(tenant_id="t1", incident_id="INC-1") is None


def test_memory_qa_review_token_round_trips() -> None:
    store = MemoryQAReviewStore()
    store.put(tenant_id="t1", incident_id="INC-1", task_token="private-token")

    assert store.get(tenant_id="t1", incident_id="INC-1") == "private-token"
    store.delete(tenant_id="t1", incident_id="INC-1")
    assert store.get(tenant_id="t1", incident_id="INC-1") is None


def test_dynamo_step_output_round_trips_and_first_write_wins() -> None:
    client = FakeDynamoClient()
    store = DynamoStepResultStore(client, "incident", clock=lambda: FIXED)
    output = {
        "step": "Close",
        "tenant_id": "t1",
        "incident_id": "INC-1",
        "shipment_id": "PH-2841",
        "workflow": {"closed": True},
        "incident_state": "CLOSED",
    }

    first = store.put(
        tenant_id="t1",
        incident_id="INC-1",
        step="Close",
        decision={"status": "closed"},
        result={"disposition": "release"},
        step_output=output,
    )
    retry = store.put(
        tenant_id="t1",
        incident_id="INC-1",
        step="Close",
        decision={"status": "replaced"},
        result={"disposition": "reject"},
        step_output={"workflow": {"closed": False}},
    )

    assert retry == first
    assert store.get(tenant_id="t1", incident_id="INC-1", step="Close") == first
    assert first.step_output == output
    assert first.decision == {"status": "closed"}
    assert first.result == {"disposition": "release"}
    assert client.writes == 1
