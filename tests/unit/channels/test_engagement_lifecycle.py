"""Task-token advancement contract for persisted engagement waits."""

from datetime import UTC, datetime, timedelta

import pytest

from oerf.platform.channels.engagement import DynamoEngagementStore, MemoryEngagementStore
from tests.unit.support.fake_dynamo import FakeDynamoClient

NOW = datetime(2026, 10, 20, 10, 16, tzinfo=UTC)
DEADLINE = NOW + timedelta(minutes=10)


@pytest.fixture(params=["memory", "dynamo"])
def store(request):
    if request.param == "memory":
        return MemoryEngagementStore(clock=lambda: NOW)
    return DynamoEngagementStore(FakeDynamoClient(), "incident", "routing", clock=lambda: NOW)


def _open(store):
    return store.open_engagement(
        "t1",
        "INC-1",
        "ENG-1",
        "driver",
        "diagnose",
        "critical_field",
        "token-rung-1",
        {"shipment_id": "PH-2841"},
        contact_id="C-1",
        rung=1,
        deadline=DEADLINE,
    )


def test_retrying_initial_open_does_not_replace_token_or_reset_engagement(store) -> None:
    opened = _open(store)
    retried = store.open_engagement(
        opened.tenant_id,
        opened.incident_id,
        opened.engagement_id,
        opened.role,
        opened.ask,
        opened.policy_id,
        "retry-token",
        opened.facts,
        contact_id=opened.contact_id,
        rung=2,
        deadline=NOW + timedelta(minutes=20),
    )

    assert retried == opened
    assert retried.task_token == "token-rung-1"
    assert retried.rung == 1
    assert retried.deadline == DEADLINE


def test_new_rung_replaces_current_token_before_registering_message(store) -> None:
    opened = _open(store)
    updated = store.prepare_wait(
        opened.tenant_id,
        opened.incident_id,
        opened.engagement_id,
        "token-rung-2",
        rung=2,
        deadline=opened.deadline,
    )
    store.register_outbound("out-2", updated)

    assert store.get("t1", "INC-1", "ENG-1") == updated
    assert store.correlate("out-2").task_token == "token-rung-2"
    with pytest.raises(ValueError, match="current task token"):
        store.register_outbound("out-stale", opened)


def test_answered_or_stopped_engagement_cannot_install_a_new_wait(store) -> None:
    opened = _open(store)
    store.answer(opened.tenant_id, opened.incident_id, opened.engagement_id)

    with pytest.raises(ValueError, match="WAITING"):
        store.prepare_wait(
            opened.tenant_id,
            opened.incident_id,
            opened.engagement_id,
            "late-token",
            rung=2,
            deadline=opened.deadline,
        )
