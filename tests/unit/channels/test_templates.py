"""Template rendering rejects a body that names a fact the caller did not pass."""

import pytest

from oerf.platform.channels.templates import MissingFacts, MissingTemplate, render_message

MESSAGES = {
    "messages": {
        "driver_checkin": {
            "sms": "Temperature alert {shipment_id}. Pull over safely and check WhatsApp.",
            "whatsapp": "Shipment {shipment_id} is at {temperature_c} C.",
        }
    }
}


def test_render_fills_only_the_named_facts() -> None:
    body = render_message(
        MESSAGES,
        "driver_checkin",
        "sms",
        {"shipment_id": "PH-2841", "phone": "+910000000101"},
    )

    assert body == "Temperature alert PH-2841. Pull over safely and check WhatsApp."
    assert "+910000000101" not in body


def test_missing_fact_is_a_validation_error() -> None:
    with pytest.raises(MissingFacts, match="temperature_c"):
        render_message(MESSAGES, "driver_checkin", "whatsapp", {"shipment_id": "PH-2841"})


def test_missing_channel_body_is_explicit() -> None:
    with pytest.raises(MissingTemplate, match="email"):
        render_message(MESSAGES, "driver_checkin", "email", {"shipment_id": "PH-2841"})
