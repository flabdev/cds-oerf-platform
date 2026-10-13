"""Normalize provider payloads into one inbound message shape.

Classification and workflow correlation are out of scope.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Channel = Literal["WHATSAPP", "SMS", "RCS", "EMAIL"]


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
        channel="WHATSAPP",
        sender=sender.strip(),
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


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise NormalizationError("media fields must be strings")
    return value
