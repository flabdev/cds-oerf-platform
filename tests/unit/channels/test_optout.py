"""STOP is handled before a waiting engagement can be resumed."""

from datetime import UTC, datetime

from oerf.platform.channels.engagement import MemoryEngagementStore
from oerf.platform.channels.optout import handle_reply, is_opt_out
from oerf.platform.channels.registry import MemoryPhoneRegistry

DEADLINE = datetime(2026, 10, 20, 10, 26, tzinfo=UTC)


def test_keyword_matches_are_trimmed_and_case_insensitive() -> None:
    assert is_opt_out("  Stop ")
    assert is_opt_out("unsubscribe")
    assert is_opt_out("BAND", extra=("BAND",))
    assert is_opt_out("please don't message me") is False
    assert is_opt_out("please stop") is False


def test_stop_blocks_correlation_and_stops_the_engagement() -> None:
    engagements, registry = _pair()
    opened = _open(engagements)
    engagements.register_outbound("wamid-1", opened)

    result = handle_reply(
        "STOP",
        "wamid-1",
        "+910000000141",
        engagements=engagements,
        registry=registry,
    )

    assert result.kind == "opt_out"
    assert result.engagement is None
    assert result.stopped_ids == ["eng-1"]
    assert engagements.correlate("wamid-1") is None
    contact = registry.get_contact("t1", "C-T1-FACA")
    assert contact is not None
    assert contact.opt_in is False


def test_free_text_still_correlates() -> None:
    engagements, registry = _pair()
    opened = _open(engagements)
    engagements.register_outbound("wamid-1", opened)

    result = handle_reply(
        "please don't message me",
        "wamid-1",
        "+910000000141",
        engagements=engagements,
        registry=registry,
    )

    contact = registry.get_contact("t1", "C-T1-FACA")
    assert result.kind == "correlated"
    assert result.engagement == opened
    assert contact is not None
    assert contact.opt_in is True


def test_unknown_stop_does_not_resume_someone_else() -> None:
    engagements, registry = _pair()
    opened = _open(engagements)
    engagements.register_outbound("wamid-1", opened)

    result = handle_reply(
        "UNSUBSCRIBE",
        "wamid-1",
        "+910000000999",
        engagements=engagements,
        registry=registry,
    )

    assert result.kind == "opt_out"
    assert result.engagement is None
    assert engagements.correlate("wamid-1") == opened


def test_configured_local_keyword_opts_out() -> None:
    engagements, registry = _pair()
    _open(engagements)

    result = handle_reply(
        " band ",
        "wamid-unused",
        "+910000000141",
        engagements=engagements,
        registry=registry,
        extra_keywords=("BAND",),
    )

    assert result.kind == "opt_out"
    assert result.stopped_ids == ["eng-1"]


def _pair():
    registry = MemoryPhoneRegistry()
    registry.register_contact("t1", "C-T1-FACA", "facility", "+910000000141")
    return MemoryEngagementStore(), registry


def _open(engagements):
    return engagements.open_engagement(
        "t1",
        "INC-000001",
        "eng-1",
        "facility",
        "accept",
        "partner_request",
        "task-token-1",
        {"shipment_id": "PH-2841"},
        contact_id="C-T1-FACA",
        rung=1,
        deadline=DEADLINE,
    )
