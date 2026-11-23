"""Jev client: request shape, strict parsing, and fail-closed errors. No network."""

import json
from typing import Any

import pytest

from oerf.platform.agent.jev import DEFAULT_MODEL, JevError, decide_choice

CRITERIA = {"billing": "Payments and refunds", "technical": "Bugs", "sales": "Pricing"}


class _Recorder:
    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.status = status
        self.body = body
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, body: bytes, headers: Any, timeout: float) -> tuple[int, bytes]:
        self.calls.append(
            {"url": url, "body": json.loads(body), "headers": dict(headers), "timeout": timeout}
        )
        raw = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        return self.status, raw


def _answer(**overrides: Any) -> dict[str, Any]:
    answer = {
        "type": "choice",
        "choice": "billing",
        "confidence": 0.98,
        "probabilities": {"billing": 0.98, "technical": 0.02, "sales": 0},
    }
    answer.update(overrides)
    return {
        "model": DEFAULT_MODEL,
        "answers": {"topic": answer},
        "usage": {"input_tokens": 42, "output_tokens": 0},
    }


def _decide(send: _Recorder, **overrides: Any):
    arguments: dict[str, Any] = {
        "api_key": "secret-key",
        "state": "My payment failed",
        "name": "topic",
        "instructions": "What is this about?",
        "criteria": CRITERIA,
        "send": send,
    }
    arguments.update(overrides)
    return decide_choice(**arguments)


def test_sends_one_pinned_choice_question_with_bearer_auth() -> None:
    send = _Recorder(body=_answer())

    result = _decide(send)

    [call] = send.calls
    assert call["url"] == "https://api.typesafe.ai/v1/systemone"
    assert call["headers"]["Authorization"] == "Bearer secret-key"
    assert call["body"] == {
        "model": DEFAULT_MODEL,
        "state": "My payment failed",
        "questions": {
            "topic": {
                "type": "choice",
                "instructions": "What is this about?",
                "criteria": CRITERIA,
            }
        },
    }
    assert result.choice == "billing"
    assert result.confidence == 0.98
    assert result.probabilities == {"billing": 0.98, "technical": 0.02, "sales": 0.0}
    assert result.model == DEFAULT_MODEL
    assert (result.input_tokens, result.output_tokens) == (42, 0)


def test_a_short_pin_accepts_the_patch_release_it_resolves_to() -> None:
    send = _Recorder(body=_answer())

    assert _decide(send, model="jev-1.14").model == DEFAULT_MODEL


@pytest.mark.parametrize("served", ["jev-1.15.0", "jev-1.140", "jev-latest", None])
def test_a_different_model_version_is_refused(served: Any) -> None:
    body = _answer()
    body["model"] = served

    with pytest.raises(JevError) as raised:
        _decide(_Recorder(body=body))

    assert raised.value.outcome == "MODEL_MISMATCH"


@pytest.mark.parametrize("status", [401, 422, 429, 500, 529])
def test_a_non_200_status_raises_with_its_code(status: int) -> None:
    with pytest.raises(JevError) as raised:
        _decide(_Recorder(status=status, body={}))

    assert raised.value.outcome == f"HTTP_{status}"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"not json", id="not-json"),
        pytest.param([], id="not-an-object"),
        pytest.param({"model": DEFAULT_MODEL, "answers": {}}, id="no-answer"),
        pytest.param(_answer(choice="refund"), id="option-not-offered"),
        pytest.param(_answer(choice=None), id="no-choice"),
        pytest.param(_answer(confidence=1.5), id="confidence-out-of-range"),
        pytest.param(_answer(confidence=True), id="confidence-bool"),
        pytest.param(_answer(confidence=None), id="no-confidence"),
    ],
)
def test_a_malformed_answer_is_refused(body: Any) -> None:
    with pytest.raises(JevError) as raised:
        _decide(_Recorder(body=body))

    assert raised.value.outcome == "MALFORMED"


def test_probabilities_keep_only_offered_options_with_valid_values() -> None:
    body = _answer(probabilities={"billing": 0.9, "refund": 0.1, "technical": "x", "sales": 2})

    assert _decide(_Recorder(body=body)).probabilities == {"billing": 0.9}


def test_a_missing_key_never_calls_jev() -> None:
    send = _Recorder(body=_answer())

    with pytest.raises(JevError) as raised:
        _decide(send, api_key="  ")

    assert raised.value.outcome == "NO_KEY"
    assert send.calls == []


def test_a_question_needs_at_least_two_options() -> None:
    with pytest.raises(ValueError, match="options"):
        _decide(_Recorder(body=_answer()), criteria={"only": "one"})


def test_the_default_sender_refuses_a_plain_http_endpoint() -> None:
    with pytest.raises(JevError) as raised:
        decide_choice(
            api_key="k",
            state="s",
            name="topic",
            instructions="i",
            criteria=CRITERIA,
            endpoint="http://api.typesafe.ai/v1/systemone",
        )

    assert raised.value.outcome == "BAD_ENDPOINT"
