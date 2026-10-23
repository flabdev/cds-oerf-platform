"""Inbound SQS normalization, dedupe, correlation, and task-token callbacks."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.exceptions import ClientError

from oerf.platform.channels.engagement import MemoryEngagementStore
from oerf.platform.channels.registry import MemoryPhoneRegistry
from oerf.platform.inbound.dedupe import InboundDedupe
from oerf.platform.inbound.handler import (
    InboundDependencies,
    configure_dependencies,
    handler,
    process_message,
)

NOW = datetime(2026, 10, 20, 10, 20, tzinfo=UTC)
DEADLINE = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)


class CallbackClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, str]] = []
        self.error = error

    def send_task_success(self, *, taskToken: str, output: str) -> None:
        self.calls.append({"taskToken": taskToken, "output": output})
        if self.error:
            raise self.error


@pytest.fixture
def setup_inbound():
    engagements = MemoryEngagementStore(clock=lambda: NOW)
    registry = MemoryPhoneRegistry()
    registry.register_contact("trusted-tenant", "contact-1", "driver", "+15550000001")
    dedupe = InboundDedupe(clock=lambda: NOW)
    callbacks = CallbackClient()

    class _NoopEvidence:
        def store(self, tenant_id, message_id, media_id, mime_type):
            return None

    deps = InboundDependencies(dedupe, engagements, callbacks, registry, _NoopEvidence())
    engagement = engagements.open_engagement(
        "trusted-tenant",
        "incident-1",
        "engagement-1",
        "driver",
        "confirm",
        "policy-1",
        "secret-task-token",
        {"shipment_id": "ship-1"},
        contact_id="contact-1",
        rung=1,
        deadline=DEADLINE,
    )
    engagements.register_outbound("outbound-1", engagement)
    return deps, engagements, registry, callbacks, engagement


def _provider(message_id: str = "incoming-1", *, text: str = "yes") -> dict[str, Any]:
    return {
        "provider": "whatsapp",
        "message_id": message_id,
        "from": "+15550000001",
        "timestamp": "2026-10-20T10:20:00Z",
        "text": text,
        "context_msg_id": "outbound-1",
    }


def test_valid_reply_resumes_correlated_engagement_with_sanitized_output(setup_inbound) -> None:
    deps, engagements, _registry, callbacks, opened = setup_inbound
    body = _provider()
    body["tenant_id"] = "attacker-tenant"
    body["task_token"] = "attacker-token"
    body["untrusted"] = "must not pass"

    assert process_message(body, deps) == "resumed"

    assert len(callbacks.calls) == 1
    call = callbacks.calls[0]
    assert call["taskToken"] == "secret-task-token"
    payload = json.loads(call["output"])
    assert payload == {
        "channel": "whatsapp",
        "provider_msg_id": "incoming-1",
        "received_at": "2026-10-20T10:20:00Z",
        "type": "text",
        "text": "yes",
        "media": [],
        "engagement": {
            "engagement_id": "engagement-1",
            "role": "driver",
            "ask": "confirm",
            "rung": 1,
        },
    }
    assert "tenant_id" not in payload and "task_token" not in payload
    answered = engagements.get(opened.tenant_id, opened.incident_id, opened.engagement_id)
    assert answered is not None and answered.status == "ANSWERED"


def test_resume_output_is_the_task_result_not_a_lambda_response_envelope(setup_inbound) -> None:
    """A resumed step reads this object at its root, so it is sent unwrapped.

    ``Payload`` and ``StatusCode`` are the Lambda request/response envelope for the
    synchronous states. Adding that envelope here would nest the reply one level
    down and break the contract of the step that waits on the token.
    """
    deps, _engagements, _registry, callbacks, _opened = setup_inbound

    assert process_message(_provider(), deps) == "resumed"

    payload = json.loads(callbacks.calls[0]["output"])
    assert "Payload" not in payload
    assert set(payload) == {
        "channel",
        "provider_msg_id",
        "received_at",
        "type",
        "text",
        "media",
        "engagement",
    }


def test_duplicate_provider_message_does_not_resume_twice(setup_inbound) -> None:
    deps, _engagements, _registry, callbacks, _opened = setup_inbound
    assert process_message(_provider(), deps) == "resumed"

    assert process_message(_provider(), deps) == "duplicate"
    assert len(callbacks.calls) == 1


def test_opt_out_is_applied_before_correlation_and_never_resumes(setup_inbound) -> None:
    deps, engagements, registry, callbacks, opened = setup_inbound

    assert process_message(_provider(text="  stop "), deps) == "opt_out"

    assert callbacks.calls == []
    stopped = engagements.get(opened.tenant_id, opened.incident_id, opened.engagement_id)
    assert stopped is not None and stopped.status == "STOPPED"
    contact = registry.get_contact("trusted-tenant", "contact-1")
    assert contact is not None and contact.opt_in is False


def test_unknown_and_late_provider_replies_are_acknowledged(setup_inbound) -> None:
    deps, engagements, _registry, callbacks, opened = setup_inbound
    engagements.stop(opened.tenant_id, opened.incident_id, opened.engagement_id)

    assert process_message(_provider(), deps) == "ignored"
    assert callbacks.calls == []


def test_an_unregistered_sender_never_resumes_a_quoted_engagement(setup_inbound) -> None:
    deps, engagements, _registry, callbacks, opened = setup_inbound
    body = _provider()
    body["from"] = "+19999999999"

    assert process_message(body, deps) == "ignored"
    assert callbacks.calls == []
    waiting = engagements.get(opened.tenant_id, opened.incident_id, opened.engagement_id)
    assert waiting is not None and waiting.status == "WAITING"


def test_another_tenants_contact_cannot_resume_the_quoted_engagement(setup_inbound) -> None:
    deps, engagements, registry, callbacks, opened = setup_inbound
    registry.register_contact("other-tenant", "contact-2", "ops_manager", "+15550000002")
    body = _provider()
    body["from"] = "+15550000002"

    assert process_message(body, deps) == "ignored"
    assert callbacks.calls == []
    waiting = engagements.get(opened.tenant_id, opened.incident_id, opened.engagement_id)
    assert waiting is not None and waiting.status == "WAITING"


def test_callback_failure_is_returned_as_partial_batch_failure(setup_inbound) -> None:
    deps, _engagements, _registry, callbacks, _opened = setup_inbound
    callbacks.error = RuntimeError("Step Functions unavailable")

    configure_dependencies(deps)
    try:
        response = handler(
            {"Records": [{"messageId": "sqs-1", "body": json.dumps(_provider())}]}, None
        )
    finally:
        configure_dependencies(None)

    assert response == {"batchItemFailures": [{"itemIdentifier": "sqs-1"}]}
    assert len(callbacks.calls) == 1


def test_oversized_provider_text_is_capped_before_callback(setup_inbound) -> None:
    deps, _engagements, _registry, callbacks, _opened = setup_inbound

    process_message(_provider(text="x" * 5000), deps)

    assert len(json.loads(callbacks.calls[0]["output"])["text"]) == 4000


def test_media_is_capped_and_projected_before_callback(setup_inbound) -> None:
    deps, _engagements, _registry, callbacks, _opened = setup_inbound
    body = _provider()
    body["text"] = None
    body["media"] = [
        {"id": f"m-{index}", "mime_type": "image/jpeg", "url": "https://evil.example/leak"}
        for index in range(12)
    ]

    assert process_message(body, deps) == "resumed"

    payload = json.loads(callbacks.calls[0]["output"])
    assert payload["type"] == "image"
    assert len(payload["media"]) == 10
    assert payload["media"][0] == {"id": "m-0", "mime_type": "image/jpeg"}
    assert all(set(item) == {"id", "mime_type"} for item in payload["media"])


def test_an_unquoted_reply_uses_the_single_waiting_address_match(setup_inbound) -> None:
    deps, _engagements, _registry, callbacks, _opened = setup_inbound
    deps.engagements.register_address("whatsapp", "+15550000001", _opened)
    body = _provider()
    body.pop("context_msg_id")

    assert process_message(body, deps) == "resumed"
    assert len(callbacks.calls) == 1


def test_an_unquoted_reply_with_ambiguous_address_is_ignored(setup_inbound) -> None:
    deps, engagements, _registry, callbacks, _opened = setup_inbound
    extra = engagements.open_engagement(
        "trusted-tenant",
        "incident-2",
        "engagement-2",
        "driver",
        "confirm",
        "policy-1",
        "another-task-token",
        {},
        contact_id="contact-1",
        rung=1,
        deadline=DEADLINE,
    )
    engagements.register_address("whatsapp", "+15550000001", extra)
    engagements.register_address("whatsapp", "+15550000001", _opened)
    body = _provider()
    body.pop("context_msg_id")

    assert process_message(body, deps) == "ignored"
    assert callbacks.calls == []


def test_quoted_correlation_takes_precedence_over_address_matches(setup_inbound) -> None:
    deps, engagements, _registry, callbacks, opened = setup_inbound
    extra = engagements.open_engagement(
        "trusted-tenant",
        "incident-2",
        "engagement-2",
        "driver",
        "confirm",
        "policy-1",
        "another-task-token",
        {},
        contact_id="contact-1",
        rung=1,
        deadline=DEADLINE,
    )
    engagements.register_address("whatsapp", "+15550000001", extra)
    engagements.register_outbound("outbound-1", opened)

    assert process_message(_provider(), deps) == "resumed"
    assert callbacks.calls[0]["taskToken"] == "secret-task-token"


def test_media_evidence_is_stored_and_key_is_forwarded(setup_inbound) -> None:
    deps, _engagements, _registry, callbacks, opened = setup_inbound
    stored: list[tuple[str, str, str, str | None]] = []

    class _Evidence:
        def store(self, tenant_id, message_id, media_id, mime_type):
            stored.append((tenant_id, message_id, media_id, mime_type))
            return f"tenants/{tenant_id}/inbound/{message_id}/evidence.jpg"

    deps = InboundDependencies(
        deps.dedupe, deps.engagements, deps.callbacks, deps.registry, _Evidence()
    )
    body = _provider()
    body["media"] = [{"id": "meta-media-1", "mime_type": "image/jpeg"}]

    assert process_message(body, deps) == "resumed"
    payload = json.loads(callbacks.calls[0]["output"])
    assert stored == [(opened.tenant_id, "incoming-1", "meta-media-1", "image/jpeg")]
    assert payload["media"][0]["evidence_key"] == (
        "tenants/trusted-tenant/inbound/incoming-1/evidence.jpg"
    )


def test_oversized_provider_media_fields_are_capped_before_callback(setup_inbound) -> None:
    deps, _engagements, _registry, callbacks, _opened = setup_inbound
    body = _provider()
    body["text"] = None
    body["media"] = [{"id": "i" * 500, "mime_type": "m" * 500}]

    assert process_message(body, deps) == "resumed"

    media = json.loads(callbacks.calls[0]["output"])["media"][0]
    assert len(media["id"]) == 256 and len(media["mime_type"]) == 256


def _dashboard(**overrides: Any) -> dict[str, Any]:
    """The queued dashboard message in docs/interfaces.md section 3.2."""
    message: dict[str, Any] = {
        "tenant_id": "trusted-tenant",
        "incident_id": "incident-1",
        "engagement_id": "engagement-1",
        "channel": "dashboard",
        "provider_msg_id": None,
        "context_msg_id": None,
        "action_id": "act-1",
        "action": "pick_option",
        "option_id": "OPT-B",
        "user_id": "user-1",
        "type": "text",
        "text": None,
        "media_ref": None,
        "received_at": "2026-10-20T10:20:00Z",
    }
    message.update(overrides)
    return message


def test_dashboard_action_correlates_on_engagement_and_resumes(setup_inbound) -> None:
    deps, _engagements, _registry, callbacks, _opened = setup_inbound

    assert process_message(_dashboard(), deps) == "resumed"

    assert len(callbacks.calls) == 1
    assert callbacks.calls[0]["taskToken"] == "secret-task-token"
    payload = json.loads(callbacks.calls[0]["output"])
    assert payload == {
        "channel": "dashboard",
        "action_id": "act-1",
        "action": "pick_option",
        "option_id": "OPT-B",
        "received_at": "2026-10-20T10:20:00Z",
        "engagement": {
            "engagement_id": "engagement-1",
            "role": "driver",
            "ask": "confirm",
            "rung": 1,
        },
    }


def test_dashboard_action_is_not_resumed_for_another_tenant(setup_inbound) -> None:
    deps, _engagements, _registry, callbacks, _opened = setup_inbound

    assert process_message(_dashboard(tenant_id="attacker-tenant"), deps) == "ignored"
    assert callbacks.calls == []


def test_late_dashboard_action_after_the_engagement_answered_is_ignored(
    setup_inbound,
) -> None:
    deps, engagements, _registry, callbacks, opened = setup_inbound
    engagements.answer(opened.tenant_id, opened.incident_id, opened.engagement_id)

    assert process_message(_dashboard(), deps) == "ignored"
    assert callbacks.calls == []


def test_invalid_dashboard_action_is_not_resumed(setup_inbound) -> None:
    deps, _engagements, _registry, callbacks, _opened = setup_inbound

    assert process_message(_dashboard(action="pick_option", option_id=None), deps) == "invalid"
    assert callbacks.calls == []


def test_expired_task_token_is_acknowledged_without_a_second_call(setup_inbound) -> None:
    deps, engagements, _registry, callbacks, opened = setup_inbound
    callbacks.error = ClientError(
        {"Error": {"Code": "InvalidToken", "Message": "invalid token"}}, "SendTaskSuccess"
    )

    assert process_message(_provider(), deps) == "resumed"

    assert len(callbacks.calls) == 1
    closed = engagements.get(opened.tenant_id, opened.incident_id, opened.engagement_id)
    assert closed is not None and closed.status != "WAITING"
    assert process_message(_provider("incoming-2"), deps) == "ignored"


def test_a_failed_callback_releases_the_dedupe_id_for_retry(setup_inbound) -> None:
    deps, _engagements, _registry, callbacks, _opened = setup_inbound
    callbacks.error = RuntimeError("Step Functions unavailable")
    configure_dependencies(deps)
    body = json.dumps(_provider())
    try:
        first = handler({"Records": [{"messageId": "sqs-1", "body": body}]}, None)
        callbacks.error = None
        second = handler({"Records": [{"messageId": "sqs-1", "body": body}]}, None)
    finally:
        configure_dependencies(None)

    assert first == {"batchItemFailures": [{"itemIdentifier": "sqs-1"}]}
    assert second == {"batchItemFailures": []}
    assert len(callbacks.calls) == 2


def test_an_unparseable_record_is_reported_for_retry(setup_inbound) -> None:
    configure_dependencies(setup_inbound[0])
    try:
        response = handler({"Records": [{"messageId": "sqs-9", "body": "not json"}]}, None)
    finally:
        configure_dependencies(None)

    assert response == {"batchItemFailures": [{"itemIdentifier": "sqs-9"}]}
