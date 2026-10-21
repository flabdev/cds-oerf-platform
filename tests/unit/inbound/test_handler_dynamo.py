"""The inbound entry point over the DynamoDB adapters, exercised with a fake client."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from oerf.platform.channels.engagement import DynamoEngagementStore
from oerf.platform.channels.registry import DynamoPhoneRegistry
from oerf.platform.inbound.dynamo import DynamoMessageDedupe
from oerf.platform.inbound.handler import (
    InboundDependencies,
    configure_dependencies,
    handler,
)
from tests.unit.support.fake_dynamo import FakeDynamoClient

INCIDENT_TABLE = "incident"
ROUTING_TABLE = "routing"
TENANT_TABLE = "tenant"
NOW = datetime(2026, 10, 20, 10, 20, tzinfo=UTC)
DEADLINE = datetime(2026, 10, 20, 10, 50, tzinfo=UTC)


class CallbackClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def send_task_success(self, *, taskToken: str, output: str) -> None:
        self.calls.append({"taskToken": taskToken, "output": output})


@pytest.fixture
def wired() -> tuple[InboundDependencies, FakeDynamoClient, CallbackClient]:
    client = FakeDynamoClient()
    dedupe = DynamoMessageDedupe(client, ROUTING_TABLE, clock=lambda: NOW)
    engagements = DynamoEngagementStore(client, INCIDENT_TABLE, ROUTING_TABLE, clock=lambda: NOW)
    registry = DynamoPhoneRegistry(client, ROUTING_TABLE, TENANT_TABLE)
    registry.register_contact("t1", "C-T1-DRV", "driver", "+15550000001")
    registry.register_contact("t2", "C-T2-OPS", "ops_manager", "+15550000002")
    callbacks = CallbackClient()
    engagement = engagements.open_engagement(
        "t1",
        "INC-000001",
        "ENG-01",
        "driver",
        "diagnose",
        "critical_field",
        "token-t1",
        {"shipment_id": "PH-2841"},
        contact_id="C-T1-DRV",
        rung=1,
        deadline=DEADLINE,
    )
    engagements.register_outbound("wamid-outbound-1", engagement)
    return InboundDependencies(dedupe, engagements, callbacks, registry), client, callbacks


def _record(body: dict[str, Any], message_id: str = "sqs-1") -> dict[str, str]:
    return {"messageId": message_id, "body": json.dumps(body)}


def _provider(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "provider": "whatsapp",
        "message_id": "wamid-inbound-1",
        "from": "+15550000001",
        "timestamp": "2026-10-20T10:20:00Z",
        "text": "the trailer is at dock 4",
        "context_msg_id": "wamid-outbound-1",
    }
    body.update(overrides)
    return body


def _run(deps: InboundDependencies, *records: dict[str, str]) -> dict[str, list[dict[str, str]]]:
    configure_dependencies(deps)
    try:
        return handler({"Records": list(records)}, None)
    finally:
        configure_dependencies(None)


def test_correlated_reply_answers_the_engagement_and_stores_dedupe(
    wired: tuple[InboundDependencies, FakeDynamoClient, CallbackClient],
) -> None:
    deps, client, callbacks = wired

    response = _run(deps, _record(_provider()))

    assert response == {"batchItemFailures": []}
    assert len(callbacks.calls) == 1
    assert callbacks.calls[0]["taskToken"] == "token-t1"
    assert json.loads(callbacks.calls[0]["output"])["text"] == "the trailer is at dock 4"
    stored = deps.dedupe.stored("wamid-inbound-1")
    assert stored is not None and stored["channel"] == "whatsapp"
    assert client.writes > 0
    answered = deps.engagements.get("t1", "INC-000001", "ENG-01")
    assert answered is not None and answered.status == "ANSWERED"


def test_retrying_the_same_record_resumes_only_once(
    wired: tuple[InboundDependencies, FakeDynamoClient, CallbackClient],
) -> None:
    deps, _client, callbacks = wired
    record = _record(_provider())

    assert _run(deps, record) == {"batchItemFailures": []}
    assert _run(deps, record, _record(_provider(), "sqs-2")) == {"batchItemFailures": []}

    assert len(callbacks.calls) == 1


def test_an_unregistered_sender_is_acknowledged_without_a_callback(
    wired: tuple[InboundDependencies, FakeDynamoClient, CallbackClient],
) -> None:
    deps, _client, callbacks = wired

    response = _run(deps, _record(_provider(**{"from": "+19999999999"})))

    assert response == {"batchItemFailures": []}
    assert callbacks.calls == []


def test_a_late_reply_after_the_engagement_stopped_does_not_resume(
    wired: tuple[InboundDependencies, FakeDynamoClient, CallbackClient],
) -> None:
    deps, _client, callbacks = wired
    deps.engagements.stop("t1", "INC-000001", "ENG-01")

    assert _run(deps, _record(_provider())) == {"batchItemFailures": []}
    assert callbacks.calls == []


def test_dashboard_action_for_another_tenant_never_resumes(
    wired: tuple[InboundDependencies, FakeDynamoClient, CallbackClient],
) -> None:
    deps, _client, callbacks = wired
    action = {
        "tenant_id": "t2",
        "incident_id": "INC-000001",
        "engagement_id": "ENG-01",
        "channel": "dashboard",
        "action_id": "act-9f3c",
        "action": "confirm_transfer",
        "received_at": "2026-10-20T10:20:00Z",
    }

    assert _run(deps, _record(action)) == {"batchItemFailures": []}
    assert callbacks.calls == []


def test_only_the_failed_record_is_returned_for_retry(
    wired: tuple[InboundDependencies, FakeDynamoClient, CallbackClient],
) -> None:
    deps, _client, _callbacks = wired

    class Flaky(CallbackClient):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def send_task_success(self, *, taskToken: str, output: str) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("Step Functions throttled")
            super().send_task_success(taskToken=taskToken, output=output)

    flaky = Flaky()
    failing = InboundDependencies(deps.dedupe, deps.engagements, flaky, deps.registry)

    response = _run(
        failing,
        _record(_provider(), "sqs-bad"),
        _record(_provider(message_id="wamid-inbound-2"), "sqs-good"),
    )

    assert response == {"batchItemFailures": [{"itemIdentifier": "sqs-bad"}]}
    assert [call["taskToken"] for call in flaky.calls] == ["token-t1"]


def test_a_number_shared_by_two_tenants_resumes_the_correlated_contact(
    wired: tuple[InboundDependencies, FakeDynamoClient, CallbackClient],
) -> None:
    deps, _client, callbacks = wired
    deps.registry.register_contact("t2", "C-T2-OPS", "ops_manager", "+15550000001")

    assert _run(deps, _record(_provider())) == {"batchItemFailures": []}

    assert [call["taskToken"] for call in callbacks.calls] == ["token-t1"]


def test_a_sender_from_another_tenant_cannot_resume_a_quoted_engagement(
    wired: tuple[InboundDependencies, FakeDynamoClient, CallbackClient],
) -> None:
    deps, _client, callbacks = wired
    deps.registry.register_contact("t2", "C-T2-OPS", "ops_manager", "+15550000003")

    response = _run(deps, _record(_provider(**{"from": "+15550000003"})))

    assert response == {"batchItemFailures": []}
    assert callbacks.calls == []
    engagement = deps.engagements.get("t1", "INC-000001", "ENG-01")
    assert engagement is not None and engagement.status == "WAITING"
