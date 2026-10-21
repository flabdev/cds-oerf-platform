"""Channel readiness. This module never calls AWS and never marks a channel live.

Secret names are references for OERF-301. The values are filled by the account
owner outside this repository.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict

QA_ROLES = frozenset({"qa", "qa_lead"})
Status = Literal["simulated", "not_configured", "external_approval"]

SECRET_NAMES = {
    "whatsapp_phone_number_id": "oerf/whatsapp/phone_number_id",
    "ses_source_identity": "oerf/ses/source_identity",
    "ses_inbound_rule": "oerf/ses/inbound_rule",
    "ses_qa_allowlist": "oerf/ses/qa_allowlist",
    "pinpoint_origination": "oerf/pinpoint/origination_number",
    "pinpoint_voice": "oerf/pinpoint/voice_origination",
    "rcs_agent": "oerf/rcs/agent",
}


class ChannelReadiness(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: str
    status: Status
    secret_name: str | None
    owner: str
    detail: str


def channel_readiness(env: Mapping[str, str] | None = None) -> list[ChannelReadiness]:
    """Report each channel from environment flags. No provider is called."""

    values = env if env is not None else os.environ
    return [
        _whatsapp(values),
        _sms(values),
        _email(values),
        _voice(),
        _rcs(),
    ]


def registered_qa_emails(contacts: Iterable[Mapping[str, object]]) -> set[str]:
    """Email addresses of opted-in QA contacts. Other roles are ignored."""

    found: set[str] = set()
    for contact in contacts:
        role = str(contact.get("party_role") or contact.get("role") or "").strip().casefold()
        email = contact.get("email")
        opted_in = contact.get("opt_in", True)
        if role not in QA_ROLES or opted_in is False:
            continue
        if isinstance(email, str) and email.strip():
            found.add(email.strip().casefold())
    return found


def qa_reply_allowed(sender: str, qa_emails: Iterable[str]) -> bool:
    """True only when the sender is exactly one registered QA address.

    A display name, a different role, or an empty allowlist does not pass.
    Passing this check does not mean the message was delivered.
    """

    token = sender.strip().casefold()
    if "@" not in token or token.startswith("@") or token.endswith("@"):
        return False
    allowed = {item.strip().casefold() for item in qa_emails if item.strip()}
    return token in allowed


def _whatsapp(env: Mapping[str, str]) -> ChannelReadiness:
    if _flag(env, "USE_MOCK_WHATSAPP"):
        return _row(
            "whatsapp",
            "simulated",
            SECRET_NAMES["whatsapp_phone_number_id"],
            "Mock mode is on. The router records a simulated attempt, not a delivery.",
        )
    if not env.get("WHATSAPP_PHONE_NUMBER_ID", "").strip():
        return _row(
            "whatsapp",
            "not_configured",
            SECRET_NAMES["whatsapp_phone_number_id"],
            "The phone number id is unset. Registration and template approval are still open.",
        )
    return _row(
        "whatsapp",
        "external_approval",
        SECRET_NAMES["whatsapp_phone_number_id"],
        "An id is present. Template approval and the webhook are not verified here.",
    )


def _sms(env: Mapping[str, str]) -> ChannelReadiness:
    if _flag(env, "USE_MOCK_SMS"):
        return _row(
            "sms",
            "simulated",
            SECRET_NAMES["pinpoint_origination"],
            "Mock mode is on. No origination number is used.",
        )
    return _row(
        "sms",
        "external_approval",
        SECRET_NAMES["pinpoint_origination"],
        "Pinpoint origination is not confirmed. The code path must not be treated as live.",
    )


def _email(env: Mapping[str, str]) -> ChannelReadiness:
    if _flag(env, "USE_MOCK_EMAIL"):
        return _row(
            "email",
            "simulated",
            SECRET_NAMES["ses_source_identity"],
            "Mock mode is on. SES is not called.",
        )
    if not env.get("SES_SOURCE_EMAIL", "").strip():
        return _row(
            "email",
            "not_configured",
            SECRET_NAMES["ses_source_identity"],
            "SES_SOURCE_EMAIL is unset. A send without it is a failure, not a delivery.",
        )
    return _row(
        "email",
        "external_approval",
        SECRET_NAMES["ses_source_identity"],
        "A source address is set. Domain verification and production access are not verified here.",
    )


def _voice() -> ChannelReadiness:
    return _row(
        "voice",
        "not_configured",
        SECRET_NAMES["pinpoint_voice"],
        "No voice provider is configured. The adapter returns voice_provider_not_configured.",
    )


def _rcs() -> ChannelReadiness:
    return _row(
        "rcs",
        "external_approval",
        SECRET_NAMES["rcs_agent"],
        "RCS is attempted only for an allowed role. Inbound reply approval is still open.",
    )


def _row(channel: str, status: Status, secret_name: str, detail: str) -> ChannelReadiness:
    return ChannelReadiness(
        channel=channel,
        status=status,
        secret_name=secret_name,
        owner="AWS account owner",
        detail=detail,
    )


def _flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() == "true"
