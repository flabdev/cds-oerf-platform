"""A small client for TypeSafe Jev, a decision model that answers typed questions.

Jev does not generate text. A ``choice`` question names its options in
``criteria`` (option key to description); the answer is one of those keys, a
``confidence``, and a ``probabilities`` map over every option. The answer can
only ever be an option key the caller wrote, which is why the reply classifier
uses it: untrusted reply text cannot steer the output outside that set.

The client is deliberately strict and never retries. Interpret has a 30-second
budget and treats any failure as ``clarify``, so a rate limit (429), an
overload (529), a timeout, a different model version, or a malformed body all
raise ``JevError`` and the caller falls back.

Only the model id, latency, token counts, and an outcome token leave this
module for tracing. The state (the reply text) is never logged.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

#: TypeSafe's own endpoint. Gateways (AIML API, Vercel) use other URLs and ids.
DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
#: Pinned. ``jev-latest`` floats to each release, and an incident's decision
#: must be reproducible against the model version that made it.
DEFAULT_MODEL = "jev-1.14.0"
#: One attempt, well inside the Interpret budget.
DEFAULT_TIMEOUT_SECONDS = 3.0
#: Jev accepts 2 to 255 options for a choice question.
_MIN_OPTIONS, _MAX_OPTIONS = 2, 255

#: ``send(url, body, headers, timeout) -> (status, body)``. Injected in tests.
Sender = Callable[[str, bytes, Mapping[str, str], float], tuple[int, bytes]]


class JevError(RuntimeError):
    """The decision could not be made or could not be trusted.

    ``outcome`` is a short trace token (``HTTP_429``, ``TIMEOUT``, ``MALFORMED``...).
    """

    def __init__(self, message: str, *, outcome: str) -> None:
        super().__init__(message)
        self.outcome = outcome


@dataclass(frozen=True)
class JevChoice:
    """One validated ``choice`` answer."""

    choice: str
    confidence: float
    probabilities: dict[str, float]
    model: str
    latency_ms: int
    input_tokens: int
    output_tokens: int


def decide_choice(
    *,
    api_key: str,
    state: str | Mapping[str, Any],
    name: str,
    instructions: str,
    criteria: Mapping[str, str],
    model: str = DEFAULT_MODEL,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    send: Sender | None = None,
) -> JevChoice:
    """Ask one ``choice`` question and return the validated answer.

    Raises ``JevError`` for any transport, status, version, or shape problem.
    """

    if not api_key.strip():
        raise JevError("no Jev API key", outcome="NO_KEY")
    if not _MIN_OPTIONS <= len(criteria) <= _MAX_OPTIONS:
        raise ValueError(f"a choice question needs {_MIN_OPTIONS}-{_MAX_OPTIONS} options")
    body = json.dumps(
        {
            "model": model,
            "state": state,
            "questions": {
                name: {
                    "type": "choice",
                    "instructions": instructions,
                    "criteria": dict(criteria),
                }
            },
        }
    ).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
    }
    started = time.monotonic()
    status, raw = (send or _urllib_send)(endpoint, body, headers, timeout)
    latency_ms = max(int((time.monotonic() - started) * 1000), 0)
    if status != 200:
        raise JevError(f"Jev returned HTTP {status}", outcome=f"HTTP_{status}")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JevError("Jev returned a body that is not JSON", outcome="MALFORMED") from exc
    return _parse_choice(
        payload, name=name, options=set(criteria), model=model, latency_ms=latency_ms
    )


def _parse_choice(
    payload: object, *, name: str, options: set[str], model: str, latency_ms: int
) -> JevChoice:
    if not isinstance(payload, Mapping):
        raise JevError("Jev returned a body that is not an object", outcome="MALFORMED")
    served = payload.get("model")
    if not isinstance(served, str) or not _same_version(model, served):
        raise JevError(
            f"Jev served {served!r}, not the pinned {model!r}", outcome="MODEL_MISMATCH"
        )
    answers = payload.get("answers")
    answer = answers.get(name) if isinstance(answers, Mapping) else None
    if not isinstance(answer, Mapping):
        raise JevError(f"Jev returned no answer for {name!r}", outcome="MALFORMED")
    choice = answer.get("choice")
    confidence = answer.get("confidence")
    if not isinstance(choice, str) or choice not in options:
        raise JevError("Jev chose an option that was not offered", outcome="MALFORMED")
    if not _probability(confidence):
        raise JevError("Jev returned no usable confidence", outcome="MALFORMED")
    raw_probabilities = answer.get("probabilities")
    probabilities = (
        {
            str(key): float(value)
            for key, value in raw_probabilities.items()
            if key in options and _probability(value)
        }
        if isinstance(raw_probabilities, Mapping)
        else {}
    )
    usage = payload.get("usage")
    return JevChoice(
        choice=choice,
        confidence=float(confidence),
        probabilities=probabilities,
        model=served,
        latency_ms=latency_ms,
        input_tokens=_count(usage, "input_tokens"),
        output_tokens=_count(usage, "output_tokens"),
    )


def _same_version(requested: str, served: str) -> bool:
    """``jev-1.14`` is served as ``jev-1.14.0``; anything else is a different model."""

    return served == requested or served.startswith(f"{requested}.")


def _probability(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and 0 <= value <= 1


def _count(usage: object, key: str) -> int:
    value = usage.get(key) if isinstance(usage, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _urllib_send(
    url: str, body: bytes, headers: Mapping[str, str], timeout: float
) -> tuple[int, bytes]:
    if not url.startswith("https://"):
        raise JevError("the Jev endpoint must use https", outcome="BAD_ENDPOINT")
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except TimeoutError as exc:
        raise JevError("Jev timed out", outcome="TIMEOUT") from exc
    except urllib.error.URLError as exc:
        outcome = "TIMEOUT" if isinstance(exc.reason, TimeoutError) else "UNREACHABLE"
        raise JevError("Jev could not be reached", outcome=outcome) from exc
