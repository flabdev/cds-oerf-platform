"""Tests for inbound message deduplication."""

import threading
from datetime import UTC, datetime, timedelta

import pytest

from oerf.platform.inbound.dedupe import RETENTION, InboundDedupe

pytestmark = pytest.mark.security


def test_first_delivery_is_accepted() -> None:
    received = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
    dedupe = InboundDedupe(clock=lambda: received)

    result = dedupe.register(message_id="wamid-123", channel="whatsapp", received_at=received)

    assert result.is_duplicate is False
    stored = dedupe.stored("wamid-123")
    assert stored is not None
    assert stored["pk"] == "INMSG#wamid-123"
    assert stored["sk"] == "META"
    assert stored["channel"] == "whatsapp"
    assert stored["expires_at"] == received + RETENTION
    assert RETENTION == timedelta(days=7)


def test_repeat_delivery_is_a_duplicate() -> None:
    dedupe = InboundDedupe()
    received = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
    dedupe.register(message_id="wamid-123", channel="whatsapp", received_at=received)

    result = dedupe.register(message_id="wamid-123", channel="whatsapp", received_at=received)

    assert result.is_duplicate is True


def test_retry_keeps_original_timestamp_until_retention_ends() -> None:
    received = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
    now = {"at": received}
    dedupe = InboundDedupe(clock=lambda: now["at"])
    dedupe.register(message_id="wamid-123", channel="whatsapp", received_at=received)

    now["at"] = received + timedelta(days=6)
    still_held = dedupe.register(
        message_id="wamid-123",
        channel="whatsapp",
        received_at=received,
    )
    assert still_held.is_duplicate is True

    now["at"] = received + RETENTION
    expired = dedupe.register(
        message_id="wamid-123",
        channel="whatsapp",
        received_at=received,
    )
    assert expired.is_duplicate is False
    stored = dedupe.stored("wamid-123")
    assert stored is not None
    assert stored["expires_at"] == received + RETENTION + RETENTION


def test_concurrent_deliveries_accept_one() -> None:
    dedupe = InboundDedupe()
    received = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
    barrier = threading.Barrier(2)
    results = []

    def attempt() -> None:
        barrier.wait()
        results.append(
            dedupe.register(message_id="wamid-123", channel="whatsapp", received_at=received)
        )

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    accepted = [result for result in results if not result.is_duplicate]
    assert len(accepted) == 1
