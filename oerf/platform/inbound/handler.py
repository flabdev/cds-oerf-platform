"""Inbound SQS entry point: normalize, dedupe, correlate, and resume engagements.

Ordering for a provider message follows ``docs/interfaces.md`` section 5:
normalize, dedupe on ``INMSG#{provider_msg_id}``, opt-out keywords, correlate on
``OUTMSG#{context_msg_id}`` (or nothing when the reply quotes nothing), check the
engagement is still ``WAITING``, then ``states:SendTaskSuccess`` on the persisted
token. A dashboard action skips opt-out and correlation: the API already wrote
``tenant_id``, ``incident_id`` and ``engagement_id`` from a verified token, so the
engagement is loaded with a tenant-scoped ``get``.

Boundaries this module keeps:

* The task token is never read from the message. Only the token stored with the
  engagement is resumed, and only once, because the replay guard then moves the
  engagement to ``ANSWERED``.
* The resumed payload carries the sanitized reply plus the engagement's own
  ``engagement_id``, ``role``, ``ask`` and ``rung``. Message text is capped, media
  entries are capped and projected to ``id``/``mime_type``, and nothing from the
  provider envelope (or the raw body) is forwarded wholesale.
* The callback value is the task result, so it is sent unwrapped. A
  ``lambda:invoke.waitForTaskToken`` step consumes this object at its state root;
  the Lambda request/response envelope (``Payload``, ``StatusCode``) belongs to the
  synchronous states. The placeholder ``Approval`` state in
  ``domains/cold_chain_excursion/statemachine.asl.json`` still carries the
  request/response ``OutputPath: $.Payload`` and resolves to ``__STUB_ARN__``; the
  engagement step that replaces it owns the reply contract (ADR-017), and OERF-203
  confirms the wiring against this output.
* Duplicate, unknown, late, or sender-mismatched messages are acknowledged and
  logged without a Step Functions call. A failure before the callback releases the
  dedupe id and is reported as an SQS partial-batch failure so it can be retried.
* No ``states:SendTaskFailure`` is sent. The agreement in section 5 keeps reply
  classification in the engagement Interpret step (ADR-017), and the state machine
  already routes a decline or cancellation through its own decision branch. Failing
  the execution because a reply was unusable would let whoever holds a contacted
  number stop a workflow, so an unusable reply leaves the rung waiting instead.

``TODO(OERF-301)``: deploy this entry point on the inbound SQS queue with
``ReportBatchItemFailures``, and grant ``states:SendTaskSuccess`` to this role
while removing it from the action handler (ADR-028). The module reads
``INCIDENT_TABLE_NAME``, ``ROUTING_TABLE_NAME`` and ``TENANT_TABLE_NAME``, all
of which the deployed ``oerf-inbound`` function already sets; no infrastructure
is changed here. Section 5 items still owned elsewhere: the ``ADDR#`` fallback
with its ``disambiguation`` and out-of-context replies, the ``JOIN`` sandbox
path, and evidence storage of inbound media (OERF-207).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import boto3
from botocore.exceptions import ClientError

from oerf.platform.channels.engagement import DynamoEngagementStore, Engagement, EngagementStore
from oerf.platform.channels.optout import handle_reply
from oerf.platform.channels.registry import (
    DynamoPhoneRegistry,
    PhoneRegistry,
    SenderResolution,
)
from oerf.platform.inbound.dedupe import MessageDedupePort
from oerf.platform.inbound.dynamo import DynamoMessageDedupe
from oerf.platform.inbound.normalize import NormalizationError, NormalizedMessage, normalize_inbound
from oerf.platform.incidents.dynamo import DynamoIncidentStore
from oerf.platform.incidents.repository import (
    TERMINAL_STATES,
    IncidentNotFound,
    IncidentStore,
    StaleIncidentUpdate,
)
from oerf.platform.observability.logging import get_logger, log

logger = get_logger(__name__)
logger.setLevel(logging.INFO)
INCIDENT_TABLE_NAME = os.environ.get("INCIDENT_TABLE_NAME", "")
TENANT_TABLE_NAME = os.environ.get("TENANT_TABLE_NAME", "")
ROUTING_TABLE_NAME = os.environ.get("ROUTING_TABLE_NAME", "")
MAX_REPLY_TEXT = 4000
MAX_MEDIA_ITEMS = 10
MAX_MEDIA_FIELD = 256
MAX_CALLBACK_BYTES = 8192
DASHBOARD_ACTIONS = frozenset({"confirm_transfer", "pick_option", "cancel"})
CANCEL_ROLES = frozenset({"platform_admin", "tenant_admin", "ops_user"})
_FINISHED_EXECUTION = frozenset({"ExecutionDoesNotExist"})
_STATE_MACHINE = ":stateMachine:"


class TaskCallbacks(Protocol):
    def send_task_success(self, *, taskToken: str, output: str) -> Any: ...


class ExecutionStop(Protocol):
    def stop_execution(self, *, executionArn: str, error: str, cause: str) -> Any: ...


@dataclass(frozen=True)
class InboundDependencies:
    dedupe: MessageDedupePort
    engagements: EngagementStore
    callbacks: TaskCallbacks
    registry: PhoneRegistry | None = None
    incidents: IncidentStore | None = None
    executions: ExecutionStop | None = None
    state_machine_arn: str = ""


_dependencies: InboundDependencies | None = None
_client: Any = None


def build_dependencies() -> InboundDependencies:
    """Build runtime adapters lazily from the OERF-301 environment contract.

    ``TODO(OERF-301)``: the inbound function needs ``INCIDENT_TABLE_NAME``,
    ``ROUTING_TABLE_NAME`` and ``TENANT_TABLE_NAME`` plus read/write access to
    the incident and routing tables. Missing configuration fails the invocation
    rather than acknowledging records that were never processed.
    """
    global _client
    if not ROUTING_TABLE_NAME or not INCIDENT_TABLE_NAME or not TENANT_TABLE_NAME:
        raise RuntimeError(
            "INCIDENT_TABLE_NAME, TENANT_TABLE_NAME and ROUTING_TABLE_NAME are required"
        )
    if _client is None:
        _client = boto3.client("dynamodb")
    stepfunctions = boto3.client("stepfunctions")
    return InboundDependencies(
        dedupe=DynamoMessageDedupe(_client, ROUTING_TABLE_NAME),
        engagements=DynamoEngagementStore(_client, INCIDENT_TABLE_NAME, ROUTING_TABLE_NAME),
        callbacks=stepfunctions,
        registry=DynamoPhoneRegistry(_client, ROUTING_TABLE_NAME, TENANT_TABLE_NAME),
        incidents=DynamoIncidentStore(_client, INCIDENT_TABLE_NAME),
        executions=stepfunctions,
        state_machine_arn=os.environ.get("STATE_MACHINE_ARN", ""),
    )


def handler(event: dict[str, Any], context: Any) -> dict[str, list[dict[str, str]]]:
    """Process SQS records and return an AWS partial-batch response."""
    del context
    dependencies = _dependencies or build_dependencies()
    records = event.get("Records") if isinstance(event, dict) else None
    messages = records if isinstance(records, list) else []
    failures: list[dict[str, str]] = []
    for record in messages:
        message_id = record.get("messageId") if isinstance(record, dict) else None
        accepted: str | None = None
        try:
            if not isinstance(record, dict):
                raise ValueError("SQS record must be an object")
            body = json.loads(record.get("body") or "")
            if not isinstance(body, dict):
                raise ValueError("SQS body must be a JSON object")
            accepted = _dedupe_id(body)
            process_message(body, dependencies)
        except Exception:
            log(
                logger,
                logging.ERROR,
                "Inbound record processing failed; requesting SQS retry",
                outcome="error",
                count=1,
            )
            # The id was accepted before the failure, so it is released only
            # then, and only so a retried record can finish the callback.
            if accepted is not None:
                _release(dependencies.dedupe, accepted)
            if isinstance(message_id, str) and message_id:
                failures.append({"itemIdentifier": message_id})
    # Record content is untrusted, so the batch is reported by count only.
    log(
        logger,
        logging.INFO,
        f"Processed inbound records={len(messages)} failures={len(failures)}",
        outcome="received",
        count=len(failures),
    )
    return {"batchItemFailures": failures}


def process_message(body: dict[str, Any], dependencies: InboundDependencies) -> str:
    """Process one provider message or dashboard action; return its disposition."""
    if body.get("channel") == "dashboard":
        return _process_dashboard(body, dependencies)
    message, context_id = _normalize_provider(body)
    received = _parse_received_at(message.received_at)
    if dependencies.dedupe.register(
        message_id=message.provider_msg_id,
        channel=message.channel,
        received_at=received,
    ).is_duplicate:
        log(
            logger,
            logging.INFO,
            f"Ignoring duplicate inbound id={message.provider_msg_id}",
            outcome="duplicate",
            count=1,
        )
        return "duplicate"
    if dependencies.registry is None:
        raise RuntimeError("provider inbound requires a sender registry")
    reply = handle_reply(
        message.text,
        context_id or message.provider_msg_id,
        message.sender,
        engagements=dependencies.engagements,
        registry=dependencies.registry,
    )
    if reply.kind == "opt_out":
        log(
            logger,
            logging.INFO,
            f"Handled provider opt-out; stopped={len(reply.stopped_ids)}",
            outcome="opt_out",
            count=len(reply.stopped_ids),
        )
        return "opt_out"
    if reply.engagement is None:
        log(
            logger,
            logging.INFO,
            f"Ignoring unknown or late provider reply id={message.provider_msg_id}",
            outcome="ignored",
            count=1,
        )
        return "ignored"
    resolution = dependencies.registry.resolve_sender(message.sender)
    if not _sender_owns_engagement(reply.engagement, resolution):
        log(
            logger,
            logging.WARNING,
            (
                "Ignoring reply from a sender that does not own "
                f"engagement={reply.engagement.engagement_id}"
            ),
            tenant_id=reply.engagement.tenant_id,
            incident_id=reply.engagement.incident_id,
            engagement_id=reply.engagement.engagement_id,
            outcome="ignored",
        )
        return "ignored"
    _resume(reply.engagement, _provider_payload(message), dependencies)
    return "resumed"


def _sender_owns_engagement(engagement: Engagement, resolution: SenderResolution) -> bool:
    """A reply may only resume an engagement opened to that same contact.

    Correlation supplies the engagement, and the registry supplies the sender's
    tenant and contact. Requiring both to agree keeps one tenant's contact from
    resuming another tenant's rung by quoting a message id, and keeps a shared
    number bound to the contact the engagement was actually opened to.
    """
    return any(
        match.tenant_id == engagement.tenant_id and match.contact_id == engagement.contact_id
        for match in resolution.matches
    )


def _normalize_provider(body: dict[str, Any]) -> tuple[NormalizedMessage, str | None]:
    """Normalize a webhook or frozen canonical provider SQS envelope."""
    raw = body.get("payload")
    payload = raw if isinstance(raw, dict) else body
    context_id = _nonblank(body.get("context_msg_id")) or _nonblank(payload.get("context_msg_id"))
    if "provider" in payload:
        return normalize_inbound(payload), context_id
    message_id, sender, timestamp = (
        payload.get("provider_msg_id"),
        payload.get("sender"),
        payload.get("received_at"),
    )
    media = payload.get("media", [])
    if not isinstance(message_id, str) or not isinstance(sender, str):
        raise NormalizationError("provider_msg_id and sender are required")
    if not isinstance(timestamp, str) or not isinstance(media, list):
        raise NormalizationError("received_at and media are invalid")
    normalized = normalize_inbound(
        {
            "provider": "whatsapp",
            "message_id": message_id,
            "from": sender,
            "timestamp": timestamp,
            "text": payload.get("text"),
            "media": media,
        }
    )
    return normalized, context_id


def _process_dashboard(body: dict[str, Any], dependencies: InboundDependencies) -> str:
    action_id = _nonblank(body.get("action_id"))
    tenant_id = _nonblank(body.get("tenant_id"))
    incident_id = _nonblank(body.get("incident_id"))
    engagement_id = _nonblank(body.get("engagement_id"))
    timestamp = _nonblank(body.get("received_at"))
    if not action_id or not tenant_id or not incident_id or not timestamp:
        raise ValueError(
            "dashboard action requires action_id, tenant_id, incident_id and received_at"
        )
    if dependencies.dedupe.register(
        message_id=action_id,
        channel="dashboard",
        received_at=_parse_received_at(timestamp),
        tenant_id=tenant_id,
    ).is_duplicate:
        log(
            logger,
            logging.INFO,
            f"Ignoring duplicate dashboard action id={action_id}",
            tenant_id=tenant_id,
            incident_id=incident_id,
            outcome="duplicate",
            count=1,
        )
        return "duplicate"
    if body.get("action") == "cancel":
        return _cancel(body, tenant_id, incident_id, engagement_id, action_id, dependencies)
    if not engagement_id:
        log(
            logger,
            logging.INFO,
            f"Ignoring dashboard action without engagement id={action_id}",
            tenant_id=tenant_id,
            incident_id=incident_id,
            outcome="ignored",
            count=1,
        )
        return "ignored"
    engagement = dependencies.engagements.get(tenant_id, incident_id, engagement_id)
    if engagement is None or engagement.status != "WAITING":
        log(
            logger,
            logging.INFO,
            f"Ignoring unknown or late dashboard action id={action_id}",
            tenant_id=tenant_id,
            incident_id=incident_id,
            outcome="ignored",
            count=1,
        )
        return "ignored"
    action, option_id = body.get("action"), _nonblank(body.get("option_id"))
    if action not in DASHBOARD_ACTIONS or (action == "pick_option" and not option_id):
        log(
            logger,
            logging.WARNING,
            f"Ignoring invalid dashboard action id={action_id}",
            tenant_id=tenant_id,
            incident_id=incident_id,
            outcome="invalid",
            count=1,
        )
        return "invalid"
    payload: dict[str, Any] = {
        "channel": "dashboard",
        "action_id": action_id,
        "action": action,
        "received_at": timestamp,
    }
    if action == "pick_option":
        payload["option_id"] = option_id
    _resume(engagement, payload, dependencies)
    return "resumed"


def _cancel(
    body: dict[str, Any],
    lookup_tenant: str,
    lookup_incident: str,
    engagement_id: str | None,
    action_id: str,
    dependencies: InboundDependencies,
) -> str:
    """Stop the ingest execution and cancel the stored incident.

    The message tenant only selects the row. The engagement, when present,
    and then the incident row supply the tenant that is cancelled. A body
    execution ARN is ignored. This path does not resume a task token.
    """

    role = _nonblank(body.get("role"))
    if role not in CANCEL_ROLES:
        log(
            logger,
            logging.WARNING,
            f"Ignoring cancel from an unauthorised role id={action_id}",
            outcome="denied",
            count=1,
        )
        return "ignored"
    if dependencies.incidents is None or dependencies.executions is None:
        raise RuntimeError("incident store and step functions are required to cancel")
    tenant_id, incident_id = _cancel_target(
        dependencies, lookup_tenant, lookup_incident, engagement_id
    )
    if tenant_id is None or incident_id is None:
        log(
            logger,
            logging.INFO,
            f"Ignoring cancel with no stored incident id={action_id}",
            tenant_id=lookup_tenant,
            incident_id=lookup_incident,
            outcome="ignored",
            count=1,
        )
        return "ignored"
    try:
        incident = dependencies.incidents.get(tenant_id, incident_id)
    except IncidentNotFound:
        log(
            logger,
            logging.INFO,
            f"Ignoring cancel for a missing incident id={action_id}",
            tenant_id=tenant_id,
            incident_id=incident_id,
            outcome="ignored",
            count=1,
        )
        return "ignored"
    if incident.tenant_id != tenant_id or incident.incident_id != incident_id:
        return "ignored"
    if incident.state in TERMINAL_STATES:
        log(
            logger,
            logging.INFO,
            f"Ignoring cancel for a terminal incident id={action_id}",
            tenant_id=incident.tenant_id,
            incident_id=incident.incident_id,
            outcome="ignored",
            count=1,
        )
        return "ignored"
    _stop_execution(dependencies, incident.incident_id)
    try:
        dependencies.incidents.transition(
            incident.tenant_id,
            incident.incident_id,
            from_state=incident.state,
            to_state="CANCELLED",
        )
    except StaleIncidentUpdate:
        latest = dependencies.incidents.get(incident.tenant_id, incident.incident_id)
        if latest.state not in TERMINAL_STATES:
            raise
    log(
        logger,
        logging.INFO,
        f"Cancelled incident id={action_id}",
        tenant_id=incident.tenant_id,
        incident_id=incident.incident_id,
        outcome="cancelled",
        count=1,
    )
    return "cancelled"


def _cancel_target(
    dependencies: InboundDependencies,
    lookup_tenant: str,
    lookup_incident: str,
    engagement_id: str | None,
) -> tuple[str | None, str | None]:
    """Return the stored tenant and incident, ignoring a mismatched body."""

    if not engagement_id:
        return lookup_tenant, lookup_incident
    engagement = dependencies.engagements.get(lookup_tenant, lookup_incident, engagement_id)
    if engagement is None:
        return None, None
    if engagement.tenant_id != lookup_tenant or engagement.incident_id != lookup_incident:
        return None, None
    return engagement.tenant_id, engagement.incident_id


def _stop_execution(dependencies: InboundDependencies, incident_id: str) -> None:
    """Stop the execution, or accept that it is already gone.

    A successful ``StopExecution`` covers an execution that is still running
    and one the service accepts because it has already finished. The only
    error treated the same way is ``ExecutionDoesNotExist``. Throttling,
    access denied, and every other error propagate. The caller has not
    changed the incident yet, and the SQS handler returns that record as a
    partial-batch failure.
    """

    arn = _execution_arn(dependencies.state_machine_arn, incident_id)
    executions = dependencies.executions
    if executions is None:
        raise RuntimeError("step functions are required to cancel")
    try:
        executions.stop_execution(executionArn=arn, error="Cancelled", cause="dashboard_cancel")
    except ClientError as exc:
        if not _is_finished_execution(exc):
            raise


def _execution_arn(state_machine_arn: str, incident_id: str) -> str:
    """The execution ARN ingest would have started for this incident."""

    from oerf.platform.ingest.handler import _execution_name

    if _STATE_MACHINE not in state_machine_arn:
        raise RuntimeError("STATE_MACHINE_ARN is required to cancel")
    prefix, machine = state_machine_arn.split(_STATE_MACHINE, 1)
    return f"{prefix}:execution:{machine}:{_execution_name(incident_id)}"


def _is_finished_execution(exc: BaseException) -> bool:
    code = None
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code")
    return code in _FINISHED_EXECUTION


def _resume(
    engagement: Engagement, payload: dict[str, Any], dependencies: InboundDependencies
) -> None:
    """Send a bounded allowlisted response, then consume the waiting engagement."""
    if engagement.status != "WAITING":
        log(
            logger,
            logging.INFO,
            f"Ignoring non-waiting engagement={engagement.engagement_id}",
            tenant_id=engagement.tenant_id,
            incident_id=engagement.incident_id,
            engagement_id=engagement.engagement_id,
            outcome="ignored",
        )
        return
    resumed = {**payload, "engagement": _engagement_block(engagement)}
    output = json.dumps(resumed, separators=(",", ":"), ensure_ascii=False)
    if len(output.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise ValueError("sanitized callback payload exceeds maximum size")
    try:
        dependencies.callbacks.send_task_success(taskToken=engagement.task_token, output=output)
    except Exception as exc:
        if not _is_expired_token(exc):
            raise
        # The wait state timed out or was already resumed, so the rung closes
        # without a second callback and a later reply correlates to nothing.
        log(
            logger,
            logging.INFO,
            f"Ignoring expired task token engagement={engagement.engagement_id}",
            tenant_id=engagement.tenant_id,
            incident_id=engagement.incident_id,
            engagement_id=engagement.engagement_id,
            outcome="ignored",
        )
    dependencies.engagements.answer(
        engagement.tenant_id, engagement.incident_id, engagement.engagement_id
    )


def _engagement_block(engagement: Engagement) -> dict[str, Any]:
    """The role/ask contract of the rung being answered, minus its token and facts."""
    return {
        "engagement_id": engagement.engagement_id,
        "role": engagement.role,
        "ask": engagement.ask,
        "rung": engagement.rung,
    }


def _provider_payload(message: NormalizedMessage) -> dict[str, Any]:
    """Strip the provider envelope and cap user-controlled values before callback."""
    return {
        "channel": message.channel,
        "provider_msg_id": message.provider_msg_id,
        "received_at": message.received_at,
        "type": message.type,
        "text": message.text[:MAX_REPLY_TEXT] if message.text is not None else None,
        "media": [
            {
                "id": item.id[:MAX_MEDIA_FIELD] if item.id is not None else None,
                "mime_type": (
                    item.mime_type[:MAX_MEDIA_FIELD] if item.mime_type is not None else None
                ),
            }
            for item in message.media[:MAX_MEDIA_ITEMS]
        ],
    }


def _parse_received_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("received_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("received_at must include timezone")
    return parsed.astimezone(UTC)


def _dedupe_id(body: dict[str, Any]) -> str | None:
    """The id this record would dedupe under, or None when it cannot be parsed."""
    if body.get("channel") == "dashboard":
        return _nonblank(body.get("action_id"))
    raw = body.get("payload")
    payload = raw if isinstance(raw, dict) else body
    return (
        _nonblank(payload.get("provider_msg_id"))
        or _nonblank(payload.get("message_id"))
        or _nonblank(body.get("action_id"))
    )


def _release(dedupe: MessageDedupePort, message_id: str) -> None:
    """Let a retried record run again after a failure before completion."""
    try:
        dedupe.forget(message_id)
    except Exception:
        log(
            logger,
            logging.WARNING,
            f"Could not release dedupe id={message_id} for retry",
            outcome="error",
            count=1,
        )


def _nonblank(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _is_expired_token(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    error = response.get("Error", {}) if isinstance(response, Mapping) else {}
    code = error.get("Code")
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code")
    return code in {"TaskDoesNotExist", "InvalidToken", "TaskTimedOut"}


def configure_dependencies(dependencies: InboundDependencies | None) -> None:
    """Install injected dependencies for tests/local runs; None restores AWS setup."""
    global _dependencies
    _dependencies = dependencies
