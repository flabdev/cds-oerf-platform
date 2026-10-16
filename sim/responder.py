"""Deterministic replies. This module never calls a provider or SendTaskSuccess."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from sim.scenarios import EXCLUDED, REQUIRED


class Reply(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario: str
    role: str
    ask: str
    attempt: int
    body: str
    intent: str
    delay_min: float
    provider_msg_id: str
    channel: str = "whatsapp"


def reply(scenario: str, role: str, ask: str, attempt: int) -> Reply | None:
    """Return the scripted reply for this attempt, or None when the party stays silent."""

    if scenario in EXCLUDED and scenario != "sandbox_join":
        raise ValueError(EXCLUDED[scenario])
    key = (scenario, role, ask, attempt)
    if key in _SILENT:
        return None
    found = _INDEX.get(key)
    if found is not None:
        return found
    fallback = _INDEX.get(("happy", role, ask, attempt))
    if fallback is None or scenario not in _KNOWN:
        return None
    return fallback.model_copy(
        update={
            "scenario": scenario,
            "provider_msg_id": _message_id(scenario, role, ask, attempt),
        }
    )


def reply_script(scenario: str) -> list[Reply]:
    """Replies defined for this scenario, in send order."""

    own = [item for item in _ROWS if item.scenario == scenario]
    if own or scenario == "debounce_blip":
        return own
    silenced = {
        (role, ask, attempt)
        for name, role, ask, attempt in _SILENT
        if name == scenario
    }
    script: list[Reply] = []
    for item in _ROWS:
        if item.scenario != "happy":
            continue
        if (item.role, item.ask, item.attempt) in silenced:
            continue
        echoed = reply(scenario, item.role, item.ask, item.attempt)
        if echoed is not None:
            script.append(echoed)
    return script


def _message_id(scenario: str, role: str, ask: str, attempt: int) -> str:
    return f"sim-{scenario}-{role}-{ask}-{attempt}"


def _reply(
    scenario: str,
    role: str,
    ask: str,
    attempt: int,
    body: str,
    intent: str,
    delay_min: float,
    *,
    channel: str = "whatsapp",
) -> Reply:
    return Reply(
        scenario=scenario,
        role=role,
        ask=ask,
        attempt=attempt,
        body=body,
        intent=intent,
        delay_min=delay_min,
        provider_msg_id=_message_id(scenario, role, ask, attempt),
        channel=channel,
    )


_ROWS = (
    _reply("happy", "driver", "driver_checkin", 1, "BTN_RUNNING", "answer", 0.5),
    _reply("happy", "driver", "driver_checkin", 2, "E17 compressor fault", "answer", 1),
    _reply("happy", "ops_manager", "ops_approval", 1, "BTN_APPROVE", "answer", 0.5),
    _reply("happy", "facility", "facility_request", 1, "BTN_ACCEPT", "answer", 1),
    _reply("happy", "driver", "driver_reroute", 1, "BTN_ON_MY_WAY", "answer", 0.3),
    _reply("happy", "facility", "transfer_confirm_request", 1, "BTN_TRANSFERRED", "answer", 2),
    _reply("happy", "qa", "qa_report_ready", 1, "BTN_RELEASE", "answer", 20),
    _reply("facility_a_declines", "facility", "facility_request", 1, "BTN_DECLINE", "decline", 0.5),
    _reply("facility_a_declines", "facility", "facility_request", 2, "BTN_ACCEPT", "answer", 1),
    _reply("driver_silent_after_voice", "driver", "driver_checkin", 1, "BTN_STOPPED", "answer", 2),
    _reply("driver_silent_late", "driver", "driver_checkin", 1, "BTN_STOPPED", "answer", 1.5),
    _reply(
        "false_alarm_cancel",
        "driver",
        "driver_checkin",
        1,
        "display shows E40 and 5 degrees, unit running",
        "answer",
        0.5,
    ),
    _reply("call_me", "driver", "driver_checkin", 1, "BTN_CALL_ME", "call-me", 0.5),
    _reply("call_me_approver", "ops_manager", "ops_approval", 1, "BTN_OTHER", "answer", 0.5),
    _reply("call_me_approver", "ops_manager", "ops_approval", 2, "BTN_CALL_ME", "call-me", 0.5),
    _reply(
        "concern",
        "driver",
        "driver_reroute",
        1,
        "why do I have to go to another warehouse?",
        "concern",
        0.5,
    ),
    _reply(
        "misspelled_code",
        "driver",
        "driver_checkin",
        1,
        "compressor fault e-17",
        "answer",
        0.5,
    ),
    _reply(
        "hinglish",
        "driver",
        "driver_checkin",
        1,
        "bhai unit band ho gaya, E17 aa raha hai",
        "answer",
        0.5,
    ),
    _reply("opt_out", "facility", "facility_request", 1, "STOP", "opt_out", 0.5),
    _reply(
        "opt_out_freetext",
        "facility",
        "facility_request",
        1,
        "please don't message me",
        "unrelated",
        0.5,
    ),
    _reply("photo_evidence", "driver", "driver_checkin", 1, "PH-01", "answer", 0.5),
    _reply("photo_evidence", "driver", "evidence_confirm", 1, "BTN_YES", "answer", 1),
    _reply("voice_note", "driver", "driver_checkin", 1, "audio", "answer", 0.5, channel="whatsapp"),
    _reply("voice_note", "driver", "driver_checkin", 2, "BTN_RUNNING", "answer", 1),
    _reply("rise_during_qa", "qa", "qa_report_ready", 1, "BTN_RELEASE", "answer", 7),
    _reply("rise_during_qa", "qa", "qa_report_ready", 2, "BTN_RELEASE", "answer", 5),
    _reply(
        "transfer_unconfirmed",
        "ops_user",
        "confirm_transfer",
        1,
        "confirm_transfer",
        "answer",
        9,
        channel="dashboard",
    ),
    _reply("sandbox_join", "driver", "join", 1, "JOIN", "answer", 1),
)

_INDEX = {(item.scenario, item.role, item.ask, item.attempt): item for item in _ROWS}

_SILENT = {
    ("driver_silent", "driver", "driver_checkin", 1),
    ("driver_silent", "driver", "driver_checkin", 2),
    ("driver_silent", "driver", "driver_reminder", 1),
    ("driver_silent", "driver", "driver_call", 1),
    ("approver_silent_within", "ops_manager", "ops_approval", 1),
    ("approver_silent_within", "ops_manager", "approval_reminder", 1),
    ("approver_silent_outside", "ops_manager", "ops_approval", 1),
    ("approver_silent_outside", "ops_manager", "approval_reminder", 1),
    ("transfer_unconfirmed", "facility", "transfer_confirm_request", 1),
    ("reroute_unacked", "driver", "driver_reroute", 1),
    ("debounce_blip", "driver", "driver_checkin", 1),
}

_KNOWN = {
    *REQUIRED,
    "driver_silent_after_voice",
    "driver_silent_late",
    "call_me",
    "call_me_approver",
    "opt_out",
    "opt_out_freetext",
    "concern",
    "misspelled_code",
    "hinglish",
    "voice_note",
    "sandbox_dashboard",
    "sandbox_join",
    "a_no_capacity",
    "none_feasible",
    "facility_a_declines",
}
