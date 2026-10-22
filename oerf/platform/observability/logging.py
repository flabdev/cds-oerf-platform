"""JSON logs with correlation ids and a redaction filter (OERF-303).

One object per line, written to stdout: ``ts``, ``level``, ``msg``, and
optional ``tenant_id``, ``incident_id``, and ``engagement_id``. Callers may
also pass ``channel``, ``outcome``, and ``count``. Every other field is
dropped, including message text, recipients, and task tokens. Values that
look like an E.164 number, an email address, or a task token are masked
inside the fields that remain.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

_DROP = frozenset(
    {
        "task_token",
        "body",
        "text",
        "message",
        "phone",
        "to",
        "recipient",
        "e164",
        "email",
        "name",
        "address",
        "authorization",
    }
)
_KEEP = frozenset(
    {"tenant_id", "incident_id", "engagement_id", "channel", "outcome", "count"}
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_E164 = re.compile(r"\+[1-9]\d{7,14}")
_TASK_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_MARK = "_oerf_json"


def _mask(value: str) -> str:
    masked = _EMAIL.sub("[redacted-email]", value)
    masked = _E164.sub("[redacted-phone]", masked)
    return _TASK_TOKEN.sub("[redacted-token]", masked)


class RedactionFilter(logging.Filter):
    """Write one redacted JSON object to stdout and stop the record.

    A logging handler would let the Lambda runtime prefix the line, so the
    CloudWatch event would not be the JSON object.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, _MARK, False):
            return False
        payload: dict[str, object] = {
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "msg": _mask(record.getMessage()),
        }
        for key in ("tenant_id", "incident_id", "engagement_id", "channel", "outcome"):
            value = record.__dict__.get(key)
            if isinstance(value, str) and value.strip():
                payload[key] = _mask(value.strip())
        count = record.__dict__.get("count")
        if isinstance(count, int) and not isinstance(count, bool):
            payload["count"] = count
        line = json.dumps(payload, separators=(",", ":"))
        record.msg = line
        record.args = ()
        setattr(record, _MARK, True)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        return False


def get_logger(name: str) -> logging.Logger:
    """Return a logger that emits redacted JSON lines."""

    logger = logging.getLogger(name)
    if not any(isinstance(item, RedactionFilter) for item in logger.filters):
        logger.addFilter(RedactionFilter())
    return logger


def log(logger: logging.Logger, level: int, msg: str, **fields: object) -> None:
    """Log ``msg`` plus the correlation ids and outcome fields that are safe."""

    extra: dict[str, object] = {}
    for key, value in fields.items():
        if key.lower() in _DROP or key not in _KEEP:
            continue
        if key == "count":
            if isinstance(value, int) and not isinstance(value, bool):
                extra[key] = value
            continue
        if isinstance(value, str) and value.strip():
            extra[key] = _mask(value.strip())
    logger.log(level, _mask(msg), extra=extra, stacklevel=2)
