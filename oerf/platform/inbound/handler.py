"""Inbound SQS entry point: normalize, dedupe, correlate, and resume engagements.

Ordering for a provider message follows ``docs/interfaces.md`` section 5:
normalize, dedupe on ``INMSG#{provider_msg_id}``, opt-out keywords, correlate on
``OUTMSG#{context_msg_id}`` when quoted, otherwise match exactly one waiting
engagement on ``ADDR#{channel}#{sender}``, validate sender ownership, store inbound
media as evidence, and then resume the persisted task token. A dashboard action
skips opt-out and correlation: the API already wrote
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

The inbound Lambda uses the deployed evidence bucket and accesses only the
``oerf/whatsapp/access_token`` secret for Meta media download. A missing or
ambiguous address match is acknowledged without resuming; a storage or provider
failure releases the dedupe id for retry. The ``JOIN`` sandbox path remains
separate from this provider reply handler.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.parse
import urllib.request
import uuid
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

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
INCIDENT_TABLE_NAME = os.environ.get("INCIDENT_TABLE_NAME", "")
TENANT_TABLE_NAME = os.environ.get("TENANT_TABLE_NAME", "")
ROUTING_TABLE_NAME = os.environ.get("ROUTING_TABLE_NAME", "")
MAX_REPLY_TEXT = 4000
MAX_MEDIA_ITEMS = 10
MAX_MEDIA_FIELD = 256
MAX_CALLBACK_BYTES = 8192
MAX_MEDIA_BYTES = 15 * 1024 * 1024
WHATSAPP_ACCESS_TOKEN_SECRET = "oerf/whatsapp/access_token"
WHATSAPP_GRAPH_API_VERSION = "v21.0"
META_MEDIA_HOSTS = frozenset({"lookaside.fbsbx.com"})
DASHBOARD_ACTIONS = frozenset({"confirm_transfer", "pick_option", "cancel"})


class _MetaMediaRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Permit redirects only to the expected HTTPS Meta media host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme != "https" or parsed.hostname not in META_MEDIA_HOSTS:
            raise ValueError("WhatsApp media redirect is not an approved Meta host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class MediaEvidenceStore(Protocol):
    def store(
        self, tenant_id: str, message_id: str, media_id: str, mime_type: str | None
    ) -> str | None:
        """Download one provider media item and return its tenant-scoped S3 key."""


class WhatsAppMediaEvidenceStore:
    """Fetch Meta media using its ID API, then persist bytes in the evidence bucket."""

    def __init__(self, bucket_name: str) -> None:
        self._bucket = bucket_name
        self._s3 = boto3.client("s3")
        self._secrets = boto3.client("secretsmanager")
        self._token: str | None = None

    def _access_token(self) -> str:
        if self._token is None:
            secret = self._secrets.get_secret_value(SecretId=WHATSAPP_ACCESS_TOKEN_SECRET)
            value = secret.get("SecretString", "")
            try:
                parsed = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                parsed = value
            if isinstance(parsed, dict):
                value = parsed.get("access_token", "")
            if not isinstance(value, str) or not value.strip():
                raise ValueError("WhatsApp media access token is unavailable")
            self._token = value.strip()
        return self._token

    def store(self, tenant_id: str, message_id: str, media_id: str, mime_type: str | None) -> str:
        token = self._access_token()
        metadata_url = (
            f"https://graph.facebook.com/{WHATSAPP_GRAPH_API_VERSION}/"
            f"{urllib.parse.quote(media_id, safe='')}"
        )
        opener = urllib.request.build_opener(_MetaMediaRedirectHandler())
        metadata_request = urllib.request.Request(
            metadata_url,
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with opener.open(metadata_request, timeout=10) as response:
                metadata = json.loads(response.read(64 * 1024))
        except Exception:
            raise ValueError("WhatsApp media metadata request failed") from None
        download_url = metadata.get("url") if isinstance(metadata, dict) else None
        if not isinstance(download_url, str):
            raise ValueError("WhatsApp media response did not include a download URL")
        parsed_url = urllib.parse.urlparse(download_url)
        if parsed_url.scheme != "https" or parsed_url.hostname not in META_MEDIA_HOSTS:
            raise ValueError("WhatsApp media download URL is not an approved Meta host")
        download_request = urllib.request.Request(
            download_url,
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with opener.open(download_request, timeout=20) as response:
                content = response.read(MAX_MEDIA_BYTES + 1)
        except Exception:
            raise ValueError("WhatsApp media download failed") from None
        if len(content) > MAX_MEDIA_BYTES:
            raise ValueError("WhatsApp media exceeds the maximum evidence size")
        content_type = mime_type or str(metadata.get("mime_type") or "application/octet-stream")
        extension = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "audio/ogg": ".ogg",
            "application/pdf": ".pdf",
        }.get(content_type.split(";", 1)[0].lower(), ".bin")
        message_ref = hashlib.sha256(message_id.encode("utf-8")).hexdigest()
        evidence_key = (
            f"tenants/{tenant_id}/inbound/{message_ref}/{uuid.uuid4().hex}{extension}"
        )
        self._s3.put_object(
            Bucket=self._bucket,
            Key=evidence_key,
            Body=content,
            ContentType=content_type,
            ServerSideEncryption="AES256",
        )
        return evidence_key


def _evidence_store() -> MediaEvidenceStore | None:
    bucket = os.environ.get("EVIDENCE_BUCKET_NAME", "").strip()
    return WhatsAppMediaEvidenceStore(bucket) if bucket else None


def _persist_media(
    message: NormalizedMessage,
    tenant_id: str,
    store: MediaEvidenceStore | None,
) -> list[dict[str, str | None]]:
    """Store provider media and project only safe metadata into the callback."""
    projected: list[dict[str, str | None]] = []
    for item in message.media[:MAX_MEDIA_ITEMS]:
        if not item.id:
            raise ValueError("provider media id is required for evidence storage")
        if store is None:
            raise RuntimeError("evidence bucket is required for inbound media")
        evidence_key = store.store(
            tenant_id,
            message.provider_msg_id,
            item.id,
            item.mime_type,
        )
        projected.append(
            {
                "id": item.id[:MAX_MEDIA_FIELD],
                "mime_type": item.mime_type[:MAX_MEDIA_FIELD]
                if item.mime_type is not None
                else None,
                **({"evidence_key": evidence_key} if evidence_key else {}),
            }
        )
    return projected


class TaskCallbacks(Protocol):
    def send_task_success(self, *, taskToken: str, output: str) -> Any: ...


@dataclass(frozen=True)
class InboundDependencies:
    dedupe: MessageDedupePort
    engagements: EngagementStore
    callbacks: TaskCallbacks
    registry: PhoneRegistry | None = None
    media_evidence: MediaEvidenceStore | None = None


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
    return InboundDependencies(
        dedupe=DynamoMessageDedupe(_client, ROUTING_TABLE_NAME),
        engagements=DynamoEngagementStore(_client, INCIDENT_TABLE_NAME, ROUTING_TABLE_NAME),
        callbacks=boto3.client("stepfunctions"),
        registry=DynamoPhoneRegistry(_client, ROUTING_TABLE_NAME, TENANT_TABLE_NAME),
        media_evidence=_evidence_store(),
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
            logger.exception("Inbound record processing failed; requesting SQS retry")
            # The id was accepted before the failure, so it is released only
            # then, and only so a retried record can finish the callback.
            if accepted is not None:
                _release(dependencies.dedupe, accepted)
            if isinstance(message_id, str) and message_id:
                failures.append({"itemIdentifier": message_id})
    # Record content is untrusted, so the batch is reported by count only.
    logger.info("Processed inbound records=%d failures=%d", len(messages), len(failures))
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
        logger.info("Ignoring duplicate inbound id=%s", message.provider_msg_id)
        return "duplicate"
    if dependencies.registry is None:
        raise RuntimeError("provider inbound requires a sender registry")
    reply = handle_reply(
        message.text,
        context_id,
        message.sender,
        engagements=dependencies.engagements,
        registry=dependencies.registry,
    )
    if reply.kind == "opt_out":
        logger.info("Handled provider opt-out; stopped=%d", len(reply.stopped_ids))
        return "opt_out"
    engagement = reply.engagement
    if context_id is None:
        candidates = dependencies.engagements.for_address(message.channel, message.sender)
        if len(candidates) != 1:
            logger.info(
                "Ignoring unquoted reply with candidate_count=%d id=%s",
                len(candidates),
                message.provider_msg_id,
            )
            return "ignored"
        engagement = candidates[0]
    if engagement is None:
        logger.info("Ignoring unknown or late provider reply id=%s", message.provider_msg_id)
        return "ignored"
    resolution = dependencies.registry.resolve_sender(message.sender)
    if not _sender_owns_engagement(engagement, resolution):
        logger.warning(
            "Ignoring reply from a sender that does not own engagement=%s",
            engagement.engagement_id,
        )
        return "ignored"
    media = _persist_media(message, engagement.tenant_id, dependencies.media_evidence)
    _resume(engagement, _provider_payload(message, media), dependencies)
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
        logger.info("Ignoring duplicate dashboard action id=%s", action_id)
        return "duplicate"
    if not engagement_id:
        logger.info("Ignoring dashboard action without engagement id=%s", action_id)
        return "ignored"
    engagement = dependencies.engagements.get(tenant_id, incident_id, engagement_id)
    if engagement is None or engagement.status != "WAITING":
        logger.info("Ignoring unknown or late dashboard action id=%s", action_id)
        return "ignored"
    action, option_id = body.get("action"), _nonblank(body.get("option_id"))
    if action not in DASHBOARD_ACTIONS or (action == "pick_option" and not option_id):
        logger.warning("Ignoring invalid dashboard action id=%s", action_id)
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


def _resume(
    engagement: Engagement, payload: dict[str, Any], dependencies: InboundDependencies
) -> None:
    """Send a bounded allowlisted response, then consume the waiting engagement."""
    if engagement.status != "WAITING":
        logger.info("Ignoring non-waiting engagement=%s", engagement.engagement_id)
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
        logger.info("Ignoring expired task token engagement=%s", engagement.engagement_id)
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


def _provider_payload(
    message: NormalizedMessage,
    media: list[dict[str, str | None]] | None = None,
) -> dict[str, Any]:
    """Strip the provider envelope and cap user-controlled values before callback."""
    return {
        "channel": message.channel,
        "provider_msg_id": message.provider_msg_id,
        "received_at": message.received_at,
        "type": message.type,
        "text": message.text[:MAX_REPLY_TEXT] if message.text is not None else None,
        "media": media
        if media is not None
        else [
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
        logger.warning("Could not release dedupe id=%s for retry", message_id)


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
