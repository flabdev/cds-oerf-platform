"""The Strands agent the runtime hosts, and its sanitized trace.

The agent's only job is to write a short advisory note about the incident the
rules half already read (``decision.py``). The model never sees a prompt, a
message body, an image, a phone number, a name, or a raw incident record: the
note request carries the state, the reason token, and the fault codes the
tenant catalog already verified.

The module imports ``strands`` lazily. The unit suite runs without the SDK, and
the handler degrades to the rules-only advisory, which is the documented
fallback.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from oerf.platform.observability.ai_trace import TraceRejected, record_invocation
from oerf.platform.observability.logging import get_logger, log

logger = get_logger(__name__)
logger.setLevel(logging.INFO)

#: The model the stack passes in ``BEDROCK_MODEL_ID``.
DEFAULT_MODEL_ID = "amazon.nova-lite-v1:0"
#: Enough for one short note, and nothing near a message draft.
MAX_TOKENS = 256
#: A note is one sentence of vocabulary, never a paragraph.
MAX_NOTE_CHARS = 200

SYSTEM_PROMPT = """You are the advisory half of a cold-chain incident workflow.
Rules already decided. Recommend one action from this exact list:
ASK_DRIVER_FOR_EVIDENCE, CONFIRM_TRANSFER, PICK_OPTION, ESCALATE_TO_SUPERVISOR,
REOPEN_EXCURSION, PROCEED_WITH_DECISION, WAIT_FOR_APPROVAL.
State no temperature, cost, time, or facility. Never address a person by name.
Reply with one line: the action code followed by at most one short reason."""


def model_id() -> str:
    """The configured model id, defaulting to Nova Lite."""

    return os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)


def build_agent(*, model: str | None = None) -> Any | None:
    """Build the Strands agent, or ``None`` when the SDK is not installed."""

    try:
        from strands import Agent
        from strands.models import BedrockModel
    except ImportError:
        log(
            logger,
            logging.INFO,
            "strands is not installed; the advisory stays rules-only",
            outcome="fallback",
            count=1,
        )
        return None

    kwargs: dict[str, Any] = {"model_id": model or model_id(), "max_tokens": MAX_TOKENS}
    region = os.environ.get("AWS_REGION", "")
    if region:
        kwargs["region_name"] = region
    guardrail_id = os.environ.get("GUARDRAIL_ID", "")
    if guardrail_id:
        kwargs["guardrail_id"] = guardrail_id
        kwargs["guardrail_version"] = os.environ.get("GUARDRAIL_VERSION", "")
    try:
        bedrock = BedrockModel(**kwargs)
        return Agent(model=bedrock, system_prompt=SYSTEM_PROMPT)
    except Exception:  # noqa: BLE001 - an agent that cannot start is a fallback
        log(
            logger,
            logging.WARNING,
            "the advisory agent did not build",
            outcome="fallback",
            count=1,
        )
        return None


def note_for(state: str, reason: str, fault_codes: list[str], agent: Any = None) -> str | None:
    """Ask the agent for one advisory note, and record the call.

    Returns ``None`` when the agent is absent or the call fails. The caller
    keeps its rules-only advisory in that case.
    """

    if agent is None:
        return None
    readable = ", ".join(sorted(fault_codes)) or "none"
    prompt = f"Incident state {state}. Reason code {reason}. Verified fault codes: {readable}."
    started = time.monotonic()
    response: dict[str, Any] | None = None
    try:
        result = agent(prompt)
        response = _response_dict(result)
        note = _note_from_result(result)
    except Exception:  # noqa: BLE001 - a model failure is a fallback, not an error
        _record(latency_ms=_elapsed_ms(started), response=None, outcome="ERROR")
        log(
            logger,
            logging.ERROR,
            "the advisory note call failed",
            outcome="error",
            count=1,
        )
        return None
    _record(latency_ms=_elapsed_ms(started), response=response, outcome="SUCCESS")
    return note


def _note_from_result(result: Any) -> str | None:
    """Read the note text off whichever shape the SDK returned."""

    message = getattr(result, "message", None)
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                text = block.get("text") if isinstance(block, dict) else None
                if isinstance(text, str) and text.strip():
                    return _clean(text)
    if isinstance(result, str):
        return _clean(result)
    return None


def _response_dict(result: Any) -> dict[str, Any] | None:
    """The usage and stop reason the trace reads, when the SDK exposes them."""

    metrics = getattr(result, "metrics", None) or {}
    usage = metrics.get("usage") if isinstance(metrics, dict) else None
    return {"usage": usage or {}, "metadata": {"stopReason": metrics.get("stop_reason")}}


def _clean(text: str) -> str:
    return " ".join(text.split())[:MAX_NOTE_CHARS]


def _elapsed_ms(started: float) -> int:
    return max(int((time.monotonic() - started) * 1000), 0)


def _record(*, latency_ms: int, response: dict[str, Any] | None, outcome: str) -> None:
    """Write the sanitized model-call trace. Tracing never fails the call."""

    usage = (response or {}).get("usage") or {}
    blocked = (response or {}).get("metadata", {}).get("stopReason") == "guardrail_intervened"
    metadata = {
        "model_id": model_id(),
        "latency_ms": max(latency_ms, 0),
        "input_tokens": _count(usage, "inputTokens"),
        "output_tokens": _count(usage, "outputTokens"),
        "guardrail_action": "BLOCKED" if blocked else "NONE",
        "outcome": outcome,
    }
    try:
        record_invocation(metadata)
    except TraceRejected:
        log(
            logger,
            logging.WARNING,
            "refusing the model-call trace",
            outcome="refused",
            count=1,
        )


def _count(usage: Any, name: str) -> int:
    if not isinstance(usage, dict):
        return 0
    value = usage.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value