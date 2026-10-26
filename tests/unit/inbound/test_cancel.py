"""Dashboard cancel stops the ingest execution and does not resume a token."""

import json
from datetime import UTC, datetime
from itertools import count
from typing import Any

import pytest
from botocore.exceptions import ClientError

from oerf.platform.channels.engagement import MemoryEngagementStore
from oerf.platform.inbound.dedupe import InboundDedupe
from oerf.platform.inbound.handler import (
    InboundDependencies,
    _execution_arn,
    configure_dependencies,
    handler,
    process_message,
)
from oerf.platform.incidents.repository import IncidentRepository
from oerf.platform.ingest.handler import _execution_name

NOW = datetime(2026, 10, 20, 10, 20, tzinfo=UTC)
DEADLINE = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
MACHINE = "arn:aws:states:us-east-1:000000000000:stateMachine:oerf-cold-chain"
TENANT = "trusted-tenant"
INCIDENT = "incident-1"
SHIPMENT = "ship-1"
BODY_ARN = "arn:aws:states:us-east-1:000000000000:execution:evil:from-body"


class _Callbacks:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def send_task_success(self, *, taskToken: str, output: str) -> None:
        self.calls.append({"taskToken": taskToken, "output": output})


class _Stop:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, str]] = []
        self.error = error

    def stop_execution(self, *, executionArn: str, error: str, cause: str) -> dict[str, str]:
        self.calls.append({"executionArn": executionArn, "error": error, "cause": cause})
        if self.error is not None:
            raise self.error
        return {"stopDate": "2026-10-20T10:20:00Z"}


def _world(
    *,
    role: str = "ops_user",
    engagement: bool = True,
    answered: bool = False,
    error: Exception | None = None,
) -> tuple[InboundDependencies, IncidentRepository, _Stop, _Callbacks]:
    incidents = IncidentRepository(clock=lambda: NOW, next_id=lambda: INCIDENT)
    incidents.create(tenant_id=TENANT, shipment_id=SHIPMENT, reason="TEMPERATURE_EXCURSION")
    engagements = MemoryEngagementStore(clock=lambda: NOW)
    if engagement:
        opened = engagements.open_engagement(
            TENANT,
            INCIDENT,
            "engagement-1",
            "driver",
            "confirm",
            "policy-1",
            "secret-task-token",
            {"shipment_id": SHIPMENT},
            contact_id="contact-1",
            rung=1,
            deadline=DEADLINE,
        )
        if answered:
            engagements.answer(opened.tenant_id, opened.incident_id, opened.engagement_id)
    stopper = _Stop(error)
    callbacks = _Callbacks()
    dependencies = InboundDependencies(
        InboundDedupe(clock=lambda: NOW),
        engagements,
        callbacks,
        incidents=incidents,
        executions=stopper,
        state_machine_arn=MACHINE,
    )
    return dependencies, incidents, stopper, callbacks


def _cancel(**overrides: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "tenant_id": TENANT,
        "incident_id": INCIDENT,
        "engagement_id": "engagement-1",
        "channel": "dashboard",
        "action_id": "act-cancel-1",
        "action": "cancel",
        "role": "ops_user",
        "user_id": "user-1",
        "received_at": "2026-10-20T10:20:00Z",
        "execution_arn": BODY_ARN,
        "reason": "do-not-log-this-reason",
        "task_token": "secret-task-token",
    }
    message.update(overrides)
    return message


def _expected_arn() -> str:
    return (
        "arn:aws:states:us-east-1:000000000000:execution:oerf-cold-chain:"
        + _execution_name(TENANT, INCIDENT)
    )


@pytest.mark.parametrize("role", ["ops_user", "tenant_admin", "platform_admin"])
def test_cancel_stops_the_derived_execution_and_releases_the_lock(role: str) -> None:
    dependencies, incidents, stopper, callbacks = _world()

    assert process_message(_cancel(role=role), dependencies) == "cancelled"

    assert stopper.calls == [
        {"executionArn": _expected_arn(), "error": "Cancelled", "cause": "dashboard_cancel"}
    ]
    assert callbacks.calls == []
    assert incidents.get(TENANT, INCIDENT).state == "CANCELLED"
    assert incidents.lock.get_lock(TENANT, SHIPMENT) is None
    event = incidents.events(TENANT, INCIDENT)[-1]
    assert event.previous_state == "OPEN"
    assert event.state == "CANCELLED"
    assert BODY_ARN not in stopper.calls[0]["executionArn"]
    assert "do-not-log-this-reason" not in stopper.calls[0]["cause"]
    assert "secret-task-token" not in stopper.calls[0]["cause"]


def test_cancel_derives_the_same_execution_name_ingest_stores() -> None:
    assert _execution_arn(MACHINE, TENANT, "INC/000123") == (
        "arn:aws:states:us-east-1:000000000000:execution:oerf-cold-chain:"
        + _execution_name(TENANT, "INC/000123")
    )


def test_throttled_stop_leaves_the_incident_unchanged_for_sqs_retry() -> None:
    throttled = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "StopExecution",
    )
    dependencies, incidents, stopper, callbacks = _world(error=throttled)
    body = json.dumps(_cancel())
    configure_dependencies(dependencies)
    try:
        failed = handler({"Records": [{"messageId": "sqs-throttle", "body": body}]}, None)
        assert incidents.get(TENANT, INCIDENT).state == "OPEN"
        assert incidents.lock.get_lock(TENANT, SHIPMENT) is not None
        stopper.error = None
        retried = handler({"Records": [{"messageId": "sqs-throttle", "body": body}]}, None)
    finally:
        configure_dependencies(None)

    assert failed == {"batchItemFailures": [{"itemIdentifier": "sqs-throttle"}]}
    assert callbacks.calls == []
    assert retried == {"batchItemFailures": []}
    assert incidents.get(TENANT, INCIDENT).state == "CANCELLED"
    assert len(stopper.calls) == 2


@pytest.mark.security
def test_cancel_without_an_engagement_uses_the_stored_incident(capsys) -> None:
    dependencies, incidents, stopper, callbacks = _world(engagement=False)

    assert process_message(_cancel(engagement_id=None), dependencies) == "cancelled"

    text = capsys.readouterr().out
    assert stopper.calls[0]["executionArn"] == _expected_arn()
    assert incidents.get(TENANT, INCIDENT).state == "CANCELLED"
    assert callbacks.calls == []
    assert "secret-task-token" not in text
    assert "do-not-log-this-reason" not in text
    assert BODY_ARN not in text


@pytest.mark.security
def test_duplicate_cancel_does_not_stop_again() -> None:
    dependencies, incidents, stopper, callbacks = _world()
    message = _cancel()

    assert process_message(message, dependencies) == "cancelled"
    assert process_message(message, dependencies) == "duplicate"

    assert len(stopper.calls) == 1
    assert callbacks.calls == []
    assert incidents.get(TENANT, INCIDENT).state == "CANCELLED"


def test_terminal_incident_is_not_stopped() -> None:
    dependencies, incidents, stopper, callbacks = _world()
    incidents.transition(TENANT, INCIDENT, from_state="OPEN", to_state="CANCELLED")

    assert process_message(_cancel(action_id="act-again"), dependencies) == "ignored"

    assert stopper.calls == []
    assert callbacks.calls == []
    assert incidents.get(TENANT, INCIDENT).state == "CANCELLED"


def test_second_cancel_after_a_finished_execution_does_not_stop_again() -> None:
    dependencies, incidents, stopper, callbacks = _world()

    assert process_message(_cancel(), dependencies) == "cancelled"
    assert process_message(_cancel(action_id="act-again"), dependencies) == "ignored"

    assert len(stopper.calls) == 1
    assert callbacks.calls == []
    assert incidents.get(TENANT, INCIDENT).state == "CANCELLED"


@pytest.mark.security
def test_unauthorised_role_does_not_stop_or_cancel() -> None:
    dependencies, incidents, stopper, callbacks = _world()

    denied = process_message(
        _cancel(role="driver", action_id="act-driver"), dependencies
    )
    missing = process_message(_cancel(role=None, action_id="act-missing"), dependencies)

    assert denied == "ignored"
    assert missing == "ignored"

    assert stopper.calls == []
    assert callbacks.calls == []
    assert incidents.get(TENANT, INCIDENT).state == "OPEN"
    assert incidents.lock.get_lock(TENANT, SHIPMENT) is not None


@pytest.mark.security
def test_a_different_body_tenant_does_not_stop() -> None:
    dependencies, incidents, stopper, callbacks = _world()

    assert process_message(_cancel(tenant_id="attacker-tenant"), dependencies) == "ignored"

    assert stopper.calls == []
    assert callbacks.calls == []
    assert incidents.get(TENANT, INCIDENT).state == "OPEN"


def test_execution_does_not_exist_still_cancels_the_incident() -> None:
    missing = ClientError(
        {"Error": {"Code": "ExecutionDoesNotExist", "Message": "gone"}},
        "StopExecution",
    )
    dependencies, incidents, stopper, callbacks = _world(error=missing)

    assert process_message(_cancel(), dependencies) == "cancelled"

    assert len(stopper.calls) == 1
    assert callbacks.calls == []
    assert incidents.get(TENANT, INCIDENT).state == "CANCELLED"
    assert incidents.lock.get_lock(TENANT, SHIPMENT) is None


def test_cancel_after_the_engagement_was_answered_does_not_resume_the_token() -> None:
    dependencies, incidents, stopper, callbacks = _world(answered=True)

    assert process_message(_cancel(), dependencies) == "cancelled"

    assert len(stopper.calls) == 1
    assert callbacks.calls == []
    assert incidents.get(TENANT, INCIDENT).state == "CANCELLED"


@pytest.mark.security
def test_dashboard_cancel_of_escalated_releases_the_lock() -> None:
    """An escalated incident stays locked until cancel, then a new breach can open."""

    ids = count(1)
    incidents = IncidentRepository(clock=lambda: NOW, next_id=lambda: f"INC-{next(ids):06d}")
    opened = incidents.create(
        tenant_id=TENANT, shipment_id=SHIPMENT, reason="TEMPERATURE_EXCURSION"
    )
    incidents.transition(TENANT, opened.incident_id, from_state="OPEN", to_state="DIAGNOSING")
    incidents.transition(TENANT, opened.incident_id, from_state="DIAGNOSING", to_state="ASSESSING")
    incidents.transition(TENANT, opened.incident_id, from_state="ASSESSING", to_state="ESCALATED")
    stopper = _Stop()
    dependencies = InboundDependencies(
        InboundDedupe(clock=lambda: NOW),
        MemoryEngagementStore(clock=lambda: NOW),
        _Callbacks(),
        incidents=incidents,
        executions=stopper,
        state_machine_arn=MACHINE,
    )

    assert (
        process_message(
            _cancel(incident_id=opened.incident_id, engagement_id=None, action_id="act-esc"),
            dependencies,
        )
        == "cancelled"
    )

    assert incidents.get(TENANT, opened.incident_id).state == "CANCELLED"
    assert incidents.lock.get_lock(TENANT, SHIPMENT) is None
    assert stopper.calls[0]["executionArn"].endswith(_execution_name(TENANT, opened.incident_id))
    opened_again = incidents.create(
        tenant_id=TENANT, shipment_id=SHIPMENT, reason="TEMPERATURE_EXCURSION"
    )
    assert opened_again.incident_id != opened.incident_id
    assert opened_again.state == "OPEN"
    assert incidents.lock.get_lock(TENANT, SHIPMENT)["incident_id"] == opened_again.incident_id
