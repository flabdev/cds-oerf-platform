"""Normalize provider payloads into one inbound message shape.

Classification and workflow correlation are out of scope.
"""

import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from oerf.platform.storage.dynamo import canonical_e164

Channel = Literal["whatsapp", "sms", "rcs", "email", "dashboard"]


class NormalizationError(ValueError):
    """The provider payload cannot be normalized."""


class MediaMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str | None = None
    mime_type: str | None = None


class NormalizedMessage(BaseModel):
    """Inbound message fields known before correlation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_msg_id: str = Field(min_length=1)
    channel: Channel
    sender: str = Field(min_length=1)
    received_at: str = Field(min_length=1)
    text: str | None = None
    type: Literal["text", "image", "audio", "document"]
    media: list[MediaMetadata] = Field(default_factory=list)


def normalize_inbound(payload: dict[str, object]) -> NormalizedMessage:
    """Convert a provider payload into the common message model."""

    if not isinstance(payload, dict):
        raise NormalizationError("payload must be an object")
    provider = str(payload.get("provider", "")).strip().lower()
    if provider == "whatsapp":
        return _normalize_whatsapp(payload)
    raise NormalizationError(f"unsupported provider {provider!r}")


def _normalize_whatsapp(payload: dict[str, object]) -> NormalizedMessage:
    message_id = payload.get("message_id")
    sender = payload.get("from")
    timestamp = payload.get("timestamp")
    if not isinstance(message_id, str) or not message_id.strip():
        raise NormalizationError("message_id is required")
    if not isinstance(sender, str) or not sender.strip():
        raise NormalizationError("from is required")
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise NormalizationError("timestamp is required")
    media = _media(payload.get("media", []))
    text = payload.get("text")
    if text is None and not media:
        raise NormalizationError("text or media is required")
    if text is not None and not isinstance(text, str):
        raise NormalizationError("text must be a string")
    return NormalizedMessage(
        provider_msg_id=message_id.strip(),
        channel="whatsapp",
        sender=canonical_e164(sender.strip()),
        received_at=timestamp.strip(),
        text=text,
        type=_message_type(media),
        media=media,
    )


def _media(value: object) -> list[MediaMetadata]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise NormalizationError("media must be a list")
    items: list[MediaMetadata] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise NormalizationError("media entries must be objects")
        items.append(
            MediaMetadata(
                id=_optional_str(entry.get("id")),
                mime_type=_optional_str(entry.get("mime_type")),
            )
        )
    return items


def _message_type(media: list[MediaMetadata]) -> Literal["text", "image", "audio", "document"]:
    if not media:
        return "text"
    mime = media[0].mime_type or ""
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    return "document"


def whatsapp_event_body(body: dict[str, Any]) -> dict[str, Any] | None:
    """Turn an End User Messaging event into the canonical provider body.

    The SQS record is either that canonical body or an SNS notification whose
    ``Message`` carries ``whatsAppWebhookEntry``. The webhook ``from`` is the
    digits Meta sends, without ``+``. A status event with no inbound message
    returns ``None``. Any other body is returned unchanged.
    """

    event: dict[str, Any] = body
    raw_message = body.get("Message")
    if body.get("Type") == "Notification" and isinstance(raw_message, str):
        parsed = json.loads(raw_message)
        if not isinstance(parsed, dict):
            return body
        event = parsed
    entry = event.get("whatsAppWebhookEntry")
    if entry is None:
        return body
    webhook = json.loads(entry) if isinstance(entry, str) else entry
    if not isinstance(webhook, dict):
        return None
    message = _first_whatsapp_message(webhook)
    if message is None:
        return None
    sender = message.get("from")
    message_id = message.get("id") or event.get("messageId")
    if not isinstance(sender, str) or not isinstance(message_id, str):
        raise NormalizationError("whatsapp message needs from and id")
    text = _whatsapp_text(message)
    received_at = _whatsapp_time(message, event)
    canonical = {
        "channel": "whatsapp",
        "provider_msg_id": message_id.strip(),
        "sender": canonical_e164(sender),
        "received_at": received_at,
        "text": text,
        "type": "text",
        "media": [],
    }
    context = message.get("context")
    quoted = context.get("id") if isinstance(context, dict) else None
    if isinstance(quoted, str) and quoted.strip():
        canonical["context_msg_id"] = quoted.strip()
    return canonical


def _first_whatsapp_message(webhook: dict[str, Any]) -> dict[str, Any] | None:
    found: list[dict[str, Any]] = []
    changes = webhook.get("changes")
    if isinstance(changes, list):
        for change in changes:
            value = change.get("value") if isinstance(change, dict) else None
            messages = value.get("messages") if isinstance(value, dict) else None
            if isinstance(messages, list):
                found.extend(item for item in messages if isinstance(item, dict))
    value = webhook.get("value")
    messages = value.get("messages") if isinstance(value, dict) else None
    if isinstance(messages, list):
        found.extend(item for item in messages if isinstance(item, dict))
    if found:
        return found[0]
    return None


def _whatsapp_text(message: dict[str, Any]) -> str | None:
    text = message.get("text")
    if isinstance(text, dict) and isinstance(text.get("body"), str):
        return text["body"]
    button = message.get("button")
    if isinstance(button, dict) and isinstance(button.get("text"), str):
        return button["text"]
    return None


def _whatsapp_time(message: dict[str, Any], event: dict[str, Any]) -> str:
    stamp = event.get("message_timestamp")
    if isinstance(stamp, str) and "T" in stamp:
        return stamp.strip()
    raw = message.get("timestamp")
    if isinstance(raw, str) and raw.strip().isdigit():
        return datetime.fromtimestamp(int(raw.strip()), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    raise NormalizationError("whatsapp message needs a timestamp")


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise NormalizationError("media fields must be strings")
    return value
