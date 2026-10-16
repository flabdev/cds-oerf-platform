"""Engagement persistence, correlation, and tenant isolation."""

from datetime import UTC, datetime

import pytest

from oerf.platform.channels.engagement import (
    CORRELATION_TTL,
    DynamoEngagementStore,
    MemoryEngagementStore,
)
from tests.unit.support.fake_dynamo import FakeDynamoClient

DEADLINE = datetime(2026, 10, 20, 10, 21, tzinfo=UTC)


@pytest.fixture(params=["memory", "dynamo"])
def store(request):
    clock = {"at": datetime(2026, 10, 20, 10, 17, tzinfo=UTC)}
    if request.param == "memory":
        return MemoryEngagementStore(clock=lambda: clock["at"]), clock
    client = FakeDynamoClient()
    opened = DynamoEngagementStore(client, "incident", "routing", clock=lambda: clock["at"])
    return opened, clock


def test_open_persists_token_rung_and_deadline(store) -> None:
    engagements, _clock = store

    opened = engagements.open_engagement(
        "t1",
        "INC-000001",
        "eng-1",
        "driver",
        "diagnose",
        "critical_field",
        "task-token-1",
        {"shipment_id": "PH-2841"},
        contact_id="C-T1-DRV",
        rung=1,
        deadline=DEADLINE,
    )

    assert opened.status == "WAITING"
    assert opened.task_token == "task-token-1"
    assert opened.rung == 1
    assert opened.deadline == DEADLINE
    assert engagements.get("t1", "INC-000001", "eng-1") == opened


def test_waiting_reply_resolves_and_late_reply_does_not(store) -> None:
    engagements, clock = store
    opened = _open(engagements)
    engagements.register_outbound("wamid-1", opened)

    assert engagements.correlate("wamid-1") == opened

    engagements.stop("t1", "INC-000001", "eng-1")
    assert engagements.correlate("wamid-1") is None

    reopened = engagements.open_engagement(
        "t1",
        "INC-000002",
        "eng-2",
        "qa",
        "disposition",
        "review_normal",
        "task-token-2",
        {"shipment_id": "PH-2841"},
        contact_id="C-T1-QA",
        rung=1,
        deadline=DEADLINE,
    )
    engagements.register_outbound("wamid-2", reopened)
    clock["at"] = clock["at"] + CORRELATION_TTL
    assert engagements.correlate("wamid-2") is None


def test_answered_reply_returns_none(store) -> None:
    engagements, _clock = store
    opened = _open(engagements)
    engagements.register_outbound("wamid-1", opened)

    engagements.answer("t1", "INC-000001", "eng-1")

    assert engagements.correlate("wamid-1") is None


def test_second_open_does_not_reset_a_stopped_engagement(store) -> None:
    engagements, _clock = store
    _open(engagements)
    engagements.stop("t1", "INC-000001", "eng-1")

    again = _open(engagements)

    assert again.status == "STOPPED"


def test_other_tenant_cannot_read_the_engagement(store) -> None:
    engagements, _clock = store
    _open(engagements)

    assert engagements.get("t2", "INC-000001", "eng-1") is None
    assert engagements.for_contact("t2", "C-T1-DRV") == []


def test_duplicate_provider_id_for_another_engagement_is_rejected(store) -> None:
    engagements, _clock = store
    first = _open(engagements)
    second = engagements.open_engagement(
        "t1",
        "INC-000002",
        "eng-2",
        "facility",
        "accept",
        "partner_request",
        "task-token-2",
        {"shipment_id": "PH-2841"},
        contact_id="C-T1-FACA",
        rung=1,
        deadline=DEADLINE,
    )
    engagements.register_outbound("wamid-1", first)

    with pytest.raises(ValueError, match="already correlated"):
        engagements.register_outbound("wamid-1", second)


def _open(engagements):
    return engagements.open_engagement(
        "t1",
        "INC-000001",
        "eng-1",
        "driver",
        "diagnose",
        "critical_field",
        "task-token-1",
        {"shipment_id": "PH-2841"},
        contact_id="C-T1-DRV",
        rung=1,
        deadline=DEADLINE,
    )
