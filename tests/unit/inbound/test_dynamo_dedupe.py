"""DynamoDB inbound dedupe, exercised with a fake client."""

from datetime import UTC, datetime, timedelta

import pytest

from oerf.platform.inbound.dedupe import RETENTION
from oerf.platform.inbound.dynamo import DynamoMessageDedupe
from tests.unit.support.fake_dynamo import FakeDynamoClient

pytestmark = pytest.mark.security


def test_first_delivery_stores_seven_day_expiry() -> None:
    now = {"at": datetime(2026, 10, 8, 10, 0, tzinfo=UTC)}
    client = FakeDynamoClient()
    dedupe = DynamoMessageDedupe(client, "routing", clock=lambda: now["at"])

    result = dedupe.register(
        message_id="wamid-123",
        channel="whatsapp",
        received_at=now["at"],
        tenant_id="t1",
    )

    assert result.is_duplicate is False
    stored = dedupe.stored("wamid-123")
    assert stored is not None
    assert stored["pk"] == "INMSG#wamid-123"
    assert stored["channel"] == "whatsapp"
    assert stored["tenant_id"] == "t1"
    assert stored["expires_at"] == now["at"] + RETENTION
    assert RETENTION == timedelta(days=7)


def test_duplicate_does_not_write() -> None:
    now = {"at": datetime(2026, 10, 8, 10, 0, tzinfo=UTC)}
    client = FakeDynamoClient()
    dedupe = DynamoMessageDedupe(client, "routing", clock=lambda: now["at"])
    dedupe.register(
        message_id="wamid-123",
        channel="whatsapp",
        received_at=now["at"],
        tenant_id="t1",
    )
    writes = client.writes

    result = dedupe.register(
        message_id="wamid-123",
        channel="whatsapp",
        received_at=now["at"],
        tenant_id="t1",
    )

    assert result.is_duplicate is True
    assert client.writes == writes


def test_expired_id_can_be_accepted_again() -> None:
    now = {"at": datetime(2026, 10, 8, 10, 0, tzinfo=UTC)}
    dedupe = DynamoMessageDedupe(FakeDynamoClient(), "routing", clock=lambda: now["at"])
    dedupe.register(message_id="wamid-123", channel="whatsapp", received_at=now["at"])

    now["at"] = now["at"] + RETENTION
    result = dedupe.register(message_id="wamid-123", channel="whatsapp", received_at=now["at"])

    assert result.is_duplicate is False
