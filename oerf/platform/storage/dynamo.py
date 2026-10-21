"""Key and attribute helpers for the low-level DynamoDB client.

Nothing in this module constructs a client.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

Attribute = dict[str, str]
Item = dict[str, Attribute]


def required_text(value: str, name: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{name} must be a non-empty string")
    return stripped


def iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch_seconds(moment: datetime) -> int:
    return int(moment.timestamp())


def from_epoch(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def s(value: str) -> Attribute:
    return {"S": value}


def n(value: int) -> Attribute:
    return {"N": str(value)}


def text(attribute: Attribute) -> str:
    return attribute["S"]


def number(attribute: Attribute) -> int:
    return int(attribute["N"])


def key(pk: str, sk: str) -> Item:
    return {"pk": s(pk), "sk": s(sk)}


def incident_pk(tenant_id: str, incident_id: str) -> str:
    return f"TENANT#{tenant_id}#INC#{incident_id}"


def lock_pk(tenant_id: str, shipment_id: str) -> str:
    return f"TENANT#{tenant_id}#SHIP#{shipment_id}"


def dedupe_pk(message_id: str) -> str:
    return f"INMSG#{message_id}"


def debounce_pk(tenant_id: str, shipment_id: str, device_id: str) -> str:
    return f"DEBOUNCE#{tenant_id}#{shipment_id}#{device_id}"


def engagement_sk(engagement_id: str) -> str:
    return f"ENG#{engagement_id}"


def outmsg_pk(provider_msg_id: str) -> str:
    return f"OUTMSG#{provider_msg_id}"


def addr_pk(channel: str, address: str) -> str:
    return f"ADDR#{channel}#{address}"


def device_pk(device_id: str) -> str:
    return f"DEVICE#{device_id}"


def phone_pk(e164: str) -> str:
    return f"PHONE#{e164}"


def contact_sk(tenant_id: str, contact_id: str) -> str:
    return f"TENANT#{tenant_id}#CONTACT#{contact_id}"


def is_conditional_failure(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    code = response.get("Error", {}).get("Code")
    return code in {"ConditionalCheckFailedException", "TransactionCanceledException"}


def client_error(code: str, reasons: list[dict[str, str]] | None = None) -> Any:
    """Build an exception whose ``response`` matches the boto3 client shape."""

    error = _ClientError(code)
    error.response = {"Error": {"Code": code, "Message": code}}
    if reasons is not None:
        error.response["CancellationReasons"] = reasons
    return error


class _ClientError(Exception):
    response: dict[str, Any]
