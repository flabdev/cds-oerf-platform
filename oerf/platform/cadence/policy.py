"""Cadence policies loaded from domain data. The engine does not read this file."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from importlib.resources import files
from typing import Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field

_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


class DurationError(ValueError):
    """An ISO-8601 duration is missing, negative, or not understood."""


class CadenceRung(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    after: str
    anchor: Literal["engagement_start", "previous_rung"]
    action: Literal["send", "call", "escalate"]
    channel: Literal["whatsapp", "sms", "voice", "email"]
    message: str
    to_role: str | None = None


class CadencePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str
    urgency: Literal["critical", "normal"]
    respect_quiet_hours: bool
    exhaust_after: str
    exhaust_action: dict[str, str]
    final_question: str | None = None
    rungs: list[CadenceRung] = Field(min_length=1)


def parse_duration(value: str) -> timedelta:
    """Parse a non-negative ISO-8601 duration such as ``PT5M`` or ``PT30S``."""

    if value.startswith("-") or not value:
        raise DurationError(f"duration {value!r} must be a non-negative ISO-8601 duration")
    match = _DURATION.fullmatch(value)
    if match is None or not any(match.groups()):
        raise DurationError(f"duration {value!r} must be a non-negative ISO-8601 duration")
    parts = {name: int(group or 0) for name, group in match.groupdict().items()}
    empty_time = parts["hours"] == parts["minutes"] == parts["seconds"] == 0
    if "T" in value and empty_time and value.endswith("T"):
        raise DurationError(f"duration {value!r} must be a non-negative ISO-8601 duration")
    return timedelta(
        days=parts["days"],
        hours=parts["hours"],
        minutes=parts["minutes"],
        seconds=parts["seconds"],
    )


def load_policies() -> dict[str, CadencePolicy]:
    """Validate and return the packaged cold-chain cadence policies."""

    directory = files("domains") / "cold_chain_excursion"
    schema = json.loads((directory / "cadence_schema.json").read_text(encoding="utf-8"))
    document = json.loads((directory / "cadence.json").read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(document),
        key=lambda error: list(error.path),
    )
    if errors:
        detail = "; ".join(
            f"{'.'.join(str(part) for part in error.path) or '$'}: {error.message}"
            for error in errors
        )
        raise DurationError(detail)
    policies = [CadencePolicy.model_validate(item) for item in document["policies"]]
    for policy in policies:
        parse_duration(policy.exhaust_after)
        for rung in policy.rungs:
            parse_duration(rung.after)
    return {policy.policy_id: policy for policy in policies}


def select_policy(
    role: str,
    ask: str,
    mapping: dict[str, str],
    policies: dict[str, CadencePolicy],
) -> CadencePolicy:
    """Choose a policy by role and ask. The mapping is data, not a tenant rule."""

    policy_id = mapping.get(f"{role}.{ask}", mapping.get("default", ""))
    policy = policies.get(policy_id)
    if policy is None:
        raise KeyError(f"no cadence policy {policy_id!r} for {role}.{ask}")
    return policy
