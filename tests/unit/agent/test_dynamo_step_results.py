"""Dynamo adapter tests for replayable workflow step envelopes."""

from datetime import UTC, datetime

from oerf.platform.agent.dynamo_step_results import DynamoStepResultStore
from tests.unit.support.fake_dynamo import FakeDynamoClient

FIXED = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)


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
