"""Sanitized X-Ray annotations for one model call.

Records the model id, latency, token counts, guardrail action, and outcome.
A prompt, an image, raw model output, or PII is refused before anything is
written. OERF-206 and OERF-207 call this from their own handlers.

When ``aws_xray_sdk`` is installed and a segment is open, each field is an
annotation on that segment. The guardrail trace is not recorded: it can carry
the values the filter masked.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol

_REQUIRED = (
    "model_id",
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "guardrail_action",
    "outcome",
)
_COUNTS = frozenset({"latency_ms", "input_tokens", "output_tokens"})
_TOKENS = frozenset({"guardrail_action", "outcome"})
_REFUSED = frozenset(
    {
        "prompt",
        "prompts",
        "system_prompt",
        "user_prompt",
        "image",
        "images",
        "image_bytes",
        "raw_output",
        "model_output",
        "output_text",
        "completion",
        "phone",
        "phone_number",
        "email",
        "name",
        "pii",
        "address",
    }
)
_MODEL_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")


class TraceRejected(ValueError):
    """The metadata included a prompt, image, raw output, PII, or other extra field."""


class AnnotationSegment(Protocol):
    def put_annotation(self, key: str, value: str | int | float | bool) -> None:
        """Write one X-Ray annotation."""


def record_invocation(
    metadata: Mapping[str, object],
    *,
    segment: AnnotationSegment | None = None,
) -> dict[str, str | int]:
    """Validate ``metadata`` and write it as X-Ray annotations.

    Returns the annotation map. Pass ``segment`` in tests. In Lambda, a missing
    SDK or a missing segment leaves the map unwritten and still returns it.
    """

    annotations = annotation_map(metadata)
    target = segment if segment is not None else _open_segment()
    if target is not None:
        for key, value in annotations.items():
            target.put_annotation(key, value)
    return annotations


def annotation_map(metadata: Mapping[str, object]) -> dict[str, str | int]:
    """Return the annotation map, or raise ``TraceRejected``."""

    keys = set(metadata)
    refused = sorted(keys & _REFUSED)
    if refused:
        raise TraceRejected(
            "refusing fields that can carry a prompt, image, raw output, or PII: "
            + ", ".join(refused)
        )
    unknown = sorted(keys - set(_REQUIRED))
    if unknown:
        raise TraceRejected("refusing fields outside the sanitized set: " + ", ".join(unknown))
    missing = [name for name in _REQUIRED if name not in metadata]
    if missing:
        raise TraceRejected("missing sanitized fields: " + ", ".join(missing))
    return {name: _value(name, metadata[name]) for name in _REQUIRED}


def _value(name: str, value: object) -> str | int:
    if name in _COUNTS:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TraceRejected(f"{name} must be a non-negative integer")
        return value
    if name == "model_id":
        if not isinstance(value, str) or _MODEL_ID.fullmatch(value) is None:
            raise TraceRejected("model_id must be a model id, not prompt or contact text")
        return value
    if name in _TOKENS:
        if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
            raise TraceRejected(f"{name} must be a short status token, not raw output or PII")
        return value
    raise TraceRejected(f"refusing {name}")


def _open_segment() -> AnnotationSegment | None:
    try:
        from aws_xray_sdk.core import xray_recorder
    except ImportError:
        return None
    current = getattr(xray_recorder, "current_segment", None)
    if current is None:
        return None
    segment = current()
    if segment is None or not hasattr(segment, "put_annotation"):
        return None
    return segment
