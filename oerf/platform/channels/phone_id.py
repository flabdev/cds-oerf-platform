"""WhatsApp origination phone-number id.

A non-empty value is cached for the process. A failed or empty SSM read
returns "" and is not cached, so the next call reads SSM again.
"""

from __future__ import annotations

import logging
from typing import Any

import boto3

from oerf.platform.observability.logging import log

_PHONE_UNSET = object()
_cached_phone_number_id: object = _PHONE_UNSET
_ssm = None
_REGION = "us-east-1"


def _ssm_client() -> Any:
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm", region_name=_REGION)
    return _ssm


def _read_phone_number_id(
    name: str, logger: logging.Logger, *, channel: str | None = None
) -> str:
    try:
        response = _ssm_client().get_parameter(Name=name, WithDecryption=True)
    except Exception:
        fields: dict[str, object] = {"outcome": "error", "count": 1}
        if channel:
            fields["channel"] = channel
        log(logger, logging.ERROR, "wa_phone_id_unreadable", **fields)
        return ""
    parameter = response.get("Parameter") if isinstance(response, dict) else None
    raw = parameter.get("Value") if isinstance(parameter, dict) else None
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return ""


def _whatsapp_phone_number_id(
    param_name: str,
    env_value: str,
    logger: logging.Logger,
    *,
    channel: str | None = None,
) -> str:
    """Return the live WhatsApp phone-number id.

    ``param_name`` is read from SSM. ``env_value`` is used only when that name
    is unset. A failed or empty parameter does not fall back to ``env_value``
    and is not cached. A non-empty value is cached for the process.
    """

    global _cached_phone_number_id
    if _cached_phone_number_id is not _PHONE_UNSET:
        return str(_cached_phone_number_id)
    name = param_name.strip()
    if name:
        value = _read_phone_number_id(name, logger, channel=channel)
    else:
        value = env_value.strip()
    if value:
        _cached_phone_number_id = value
    return value
