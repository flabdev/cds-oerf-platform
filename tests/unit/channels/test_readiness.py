"""Readiness probe, QA allowlist, and the onboarding runbook."""

from pathlib import Path

from oerf.platform.channels.readiness import (
    SECRET_NAMES,
    channel_readiness,
    qa_reply_allowed,
    registered_qa_emails,
)

RUNBOOK = Path("docs/provider-onboarding.md")


def test_probe_never_claims_a_channel_is_live() -> None:
    rows = channel_readiness({})

    assert {row.status for row in rows} <= {"simulated", "not_configured", "external_approval"}
    assert "production_ready" not in {row.status for row in rows}
    email = next(row for row in rows if row.channel == "email")
    assert email.status == "not_configured"
    voice = next(row for row in rows if row.channel == "voice")
    assert voice.status == "not_configured"


def test_mock_flags_are_simulated_and_do_not_call_a_provider() -> None:
    rows = channel_readiness(
        {
            "USE_MOCK_WHATSAPP": "true",
            "USE_MOCK_SMS": "true",
            "USE_MOCK_EMAIL": "true",
            "SES_SOURCE_EMAIL": "reports@example.com",
            "WHATSAPP_PHONE_NUMBER_ID": "phone-id",
        }
    )
    by_channel = {row.channel: row.status for row in rows}

    assert by_channel["whatsapp"] == "simulated"
    assert by_channel["sms"] == "simulated"
    assert by_channel["email"] == "simulated"
    assert by_channel["rcs"] == "external_approval"


def test_qa_reply_accepts_only_a_registered_qa_address() -> None:
    contacts = [
        {"party_role": "qa", "email": "QA@example.com", "opt_in": True},
        {"party_role": "qa_lead", "email": "lead@example.com", "opt_in": True},
        {"party_role": "driver", "email": "driver@example.com", "opt_in": True},
        {"party_role": "qa", "email": "former@example.com", "opt_in": False},
    ]
    allowed = registered_qa_emails(contacts)

    assert qa_reply_allowed("qa@example.com", allowed) is True
    assert qa_reply_allowed("  Lead@example.com ", allowed) is True
    assert qa_reply_allowed("driver@example.com", allowed) is False
    assert qa_reply_allowed("former@example.com", allowed) is False
    assert qa_reply_allowed("QA", allowed) is False
    assert qa_reply_allowed("qa@example.com", []) is False


def test_runbook_names_the_secrets_and_does_not_claim_production() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")

    for name in SECRET_NAMES.values():
        assert name in text
    assert "channel production-ready" in text
    assert "A row is never `production_ready`" in text
    assert "AKIA" not in text
    assert "GetSecretValue" in text
    assert "PutSecretValue" in text
