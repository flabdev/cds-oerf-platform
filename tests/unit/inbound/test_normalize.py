"""Tests for inbound message normalization."""

import pytest

from oerf.platform.inbound.normalize import NormalizationError, normalize_inbound


def test_whatsapp_text_is_normalized() -> None:
    message = normalize_inbound(
        {
            "provider": "whatsapp",
            "message_id": "wamid-123",
            "from": "+919999999999",
            "text": "E17 compressor error",
            "timestamp": "2026-10-08T10:00:00Z",
        }
    )

    assert message.model_dump() == {
        "provider_msg_id": "wamid-123",
        "channel": "WHATSAPP",
        "sender": "+919999999999",
        "received_at": "2026-10-08T10:00:00Z",
        "text": "E17 compressor error",
        "type": "text",
        "media": [],
    }


def test_media_metadata_is_preserved() -> None:
    message = normalize_inbound(
        {
            "provider": "whatsapp",
            "message_id": "wamid-456",
            "from": "+919999999999",
            "timestamp": "2026-10-08T10:00:00Z",
            "media": [{"id": "media-1", "mime_type": "image/jpeg"}],
        }
    )

    assert message.type == "image"
    assert message.media[0].id == "media-1"
    assert message.text is None


def test_missing_message_id_is_rejected() -> None:
    with pytest.raises(NormalizationError, match="message_id"):
        normalize_inbound(
            {
                "provider": "whatsapp",
                "from": "+919999999999",
                "text": "hello",
                "timestamp": "2026-10-08T10:00:00Z",
            }
        )
