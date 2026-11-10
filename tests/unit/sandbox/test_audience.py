"""Audience driver join: cap, shipment pool, masked desk rows, expiry."""

from datetime import UTC, datetime, timedelta

from oerf.platform.channels.registry import DynamoPhoneRegistry, MemoryPhoneRegistry
from oerf.platform.inbound.normalize import NormalizedMessage
from oerf.platform.sandbox.audience import (
    DynamoAudienceStore,
    MemoryAudienceStore,
    audience_join_link,
    handle_audience_message,
    list_drivers,
    mask_number,
    public_driver_name,
)
from oerf.platform.storage.dynamo import key, s
from tests.unit.support.fake_dynamo import FakeDynamoClient

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


def _message(message_id: str, sender: str, text: str = "JOIN DRIVER") -> NormalizedMessage:
    return NormalizedMessage(
        provider_msg_id=message_id,
        channel="whatsapp",
        sender=sender,
        text=text,
        type="text",
        received_at="2026-11-10T09:00:00Z",
    )


def test_names_and_links_do_not_carry_a_full_number() -> None:
    assert mask_number("+155500019001") == "•••01"
    assert public_driver_name("Mudita") == "Mudita"
    assert public_driver_name("+155500019001") == ""
    link = audience_join_link("+155500019001")
    assert link == "https://wa.me/155500019001?text=JOIN%20DRIVER"


def test_a_team_number_is_rejected_and_expiry_removes_the_contact() -> None:
    registry = MemoryPhoneRegistry()
    registry.register_contact("t1", "C-T1-DRV", "driver", "+15550100001")
    store = MemoryAudienceStore(pool=("PH-2841",))
    rejected = handle_audience_message(
        _message("m-team", "+15550100001"),
        registry=registry,
        store=store,
        now=NOW,
    )
    created = handle_audience_message(
        _message("m-new", "+155500019009"),
        registry=registry,
        store=store,
        now=NOW,
    )

    assert rejected.outcome == "rejected"
    assert created.outcome == "created"
    assert created.session is not None
    assert registry.get_contact("t1", created.session.contact_id) is not None
    later = handle_audience_message(
        _message("m-later", "+155500019008"),
        registry=registry,
        store=store,
        now=NOW + timedelta(hours=25),
    )
    assert later.outcome == "created"
    assert registry.get_contact("t1", created.session.contact_id) is None
    assert list_drivers(store, NOW + timedelta(hours=25))[0]["shipment_id"] == "PH-2841"


def test_dynamo_join_stamps_a_label_and_assigns_the_shipment() -> None:
    client = FakeDynamoClient()
    client.put_item(
        TableName="tenant",
        Item={
            **key("TENANT#t1", "SHIPMENT#PH-2841"),
            "shipment_id": s("PH-2841"),
            "driver": s("C-T1-DRV"),
        },
    )
    registry = DynamoPhoneRegistry(client, "routing", "tenant")
    store = DynamoAudienceStore(client, "tenant", "routing", ("PH-2841",))
    result = store.claim(_message("m-1", "+155500019001"), NOW, registry)

    assert result.outcome == "created"
    assert result.session is not None
    contact = client.get_item(
        TableName="tenant",
        Key=key("TENANT#t1", f"CONTACT#{result.session.contact_id}"),
    )["Item"]
    shipment = client.get_item(
        TableName="tenant",
        Key=key("TENANT#t1", "SHIPMENT#PH-2841"),
    )["Item"]
    assert contact["label"]["S"] == result.session.display_name
    assert contact["audience"]["S"] == "true"
    assert shipment["driver"]["S"] == result.session.contact_id
    assert "155500019001" not in result.session.display_name
    again = store.claim(_message("m-2", "+155500019002"), NOW, registry)
    assert again.outcome == "full"
