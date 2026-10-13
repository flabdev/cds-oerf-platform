"""Tests for inbound message deduplication."""

import threading
from datetime import UTC, datetime, timedelta

from oerf.platform.inbound.dedupe import RETENTION, InboundDedupe


def test_first_delivery_is_accepted() -> None:
    dedupe = InboundDedupe()
    received = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)

    result = dedupe.register(message_id="wamid-123", channel="WHATSAPP", received_at=received)

    assert result.is_duplicate is False
    stored = dedupe.stored("wamid-123")
    assert stored is not None
    assert stored["pk"] == "INMSG#wamid-123"
    assert stored["sk"] == "META"
    assert stored["channel"] == "WHATSAPP"
    assert stored["expires_at"] == received + RETENTION
    assert RETENTION == timedelta(days=7)


def test_repeat_delivery_is_a_duplicate() -> None:
    dedupe = InboundDedupe()
    received = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
    dedupe.register(message_id="wamid-123", channel="WHATSAPP", received_at=received)

    result = dedupe.register(message_id="wamid-123", channel="WHATSAPP", received_at=received)

    assert result.is_duplicate is True


def test_expired_message_can_be_accepted_again() -> None:
    dedupe = InboundDedupe()
    received = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
    dedupe.register(message_id="wamid-123", channel="WHATSAPP", received_at=received)

    later = dedupe.register(
        message_id="wamid-123",
        channel="WHATSAPP",
        received_at=received + RETENTION,
    )

    assert later.is_duplicate is False


def test_concurrent_deliveries_accept_one() -> None:
    dedupe = InboundDedupe()
    received = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
    barrier = threading.Barrier(2)
    results = []

    def attempt() -> None:
        barrier.wait()
        results.append(
            dedupe.register(message_id="wamid-123", channel="WHATSAPP", received_at=received)
        )

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    accepted = [result for result in results if not result.is_duplicate]
    assert len(accepted) == 1
