"""The live corpus scorer stays opt-in and does not keep reply text."""

import json

import pytest

from scripts.eval_driver_replies_live import cascade, main, score_rows


def test_the_command_refuses_without_an_explicit_model_call() -> None:
    assert main([]) == 2


def test_the_report_scores_categories_without_keeping_reply_text() -> None:
    rows = [
        {
            "id": "DR-1",
            "text": "the unit is running and this text must not be reported",
            "lang": "en",
            "expected_intent": "answer",
        },
        {
            "id": "DR-2",
            "text": "please call me back about the secret reply",
            "lang": "hi-Latn",
            "expected_intent": "call-me",
        },
    ]

    def classify(text: str) -> tuple[dict, dict]:
        if "call" in text:
            proposal = {
                "category": "call-me",
                "intent": "call_me",
                "fields": {},
                "confidence": 0.9,
            }
        else:
            proposal = {
                "category": "answer",
                "intent": "unit_running",
                "fields": {"fault_code": "E17"},
                "confidence": 0.9,
            }
        return proposal, {"latency_ms": 12, "input_tokens": 3, "output_tokens": 4}

    report = score_rows(rows, classify, model_id="amazon.nova-lite-v1:0", prompt_version="v")
    rendered = json.dumps(report)
    assert "the unit is running" not in rendered
    assert "secret reply" not in rendered
    assert report["evaluator"] == "live model"
    assert report["invocations"] == 2
    assert report["correct"] == 2
    assert report["by_language"]["en"]["correct"] == 1
    assert report["by_language"]["hi-Latn"]["correct"] == 1
    fault = report["results"][0]
    assert fault["predicted_label"] == "answer"
    assert fault["valid"] is False
    assert fault["asl_route"] == "ResetLadder"
    assert report["results"][1]["asl_route"] == "HandleCallback"


def test_a_driver_decline_is_scored_against_the_clarify_route() -> None:
    """v1.58: a decline proposal on critical_field resets the ladder."""

    rows = [
        {"id": "DR-37", "text": "I will not divert", "lang": "en", "expected_intent": "decline"}
    ]

    def classify(_text: str) -> tuple[dict, dict]:
        proposal = {"category": "decline", "intent": "decline", "fields": {}, "confidence": 0.95}
        return proposal, {"latency_ms": 1, "input_tokens": 1, "output_tokens": 1}

    report = score_rows(rows, classify, model_id="m", prompt_version="v")

    [row] = report["results"]
    assert row["asl_route"] == "ResetLadder"
    assert row["route_correct"] is True


def test_jev_needs_its_own_confirmation_and_a_key(monkeypatch) -> None:
    monkeypatch.delenv("JEV_API_KEY", raising=False)

    assert main(["--backend", "jev", "--confirm-bedrock-call"]) == 2
    assert main(["--backend", "jev", "--confirm-jev-call"]) == 2


def test_the_report_bands_confidence_for_calibration() -> None:
    rows = [
        {"id": f"DR-{index}", "text": "t", "lang": "en", "expected_intent": "answer"}
        for index in range(3)
    ]
    answers = iter([("answer", 0.95), ("concern", 0.92), ("answer", 0.5)])

    def classify(_text: str) -> tuple[dict, dict]:
        category, confidence = next(answers)
        intent = "unit_running" if category == "answer" else "clarify"
        proposal = {
            "category": category,
            "intent": intent,
            "fields": {},
            "confidence": confidence,
        }
        return proposal, {"latency_ms": 1, "input_tokens": 1, "output_tokens": 0}

    report = score_rows(rows, classify, model_id="m", prompt_version="v")

    assert report["by_confidence"][">=0.9"] == {"count": 2, "correct": 1, "accuracy": 0.5}
    assert report["by_confidence"]["<0.8"] == {"count": 1, "correct": 1, "accuracy": 1.0}


def test_the_jev_backend_scores_the_corpus_through_the_same_gate(monkeypatch, capsys) -> None:
    from oerf.platform.agent import jev

    sent: list[dict] = []

    def fake_send(_url, body, _headers, _timeout):
        request = json.loads(body)
        sent.append(request)
        reply = request["state"]["reply"]
        choice = "call_me" if "call" in reply else "unit_running"
        answer = {"type": "choice", "choice": choice, "confidence": 0.97}
        payload = {
            "model": jev.DEFAULT_MODEL,
            "answers": {"reply": answer},
            "usage": {"input_tokens": 50, "output_tokens": 0},
        }
        return 200, json.dumps(payload).encode()

    monkeypatch.setenv("JEV_API_KEY", "test-key")
    monkeypatch.setattr(jev, "_urllib_send", fake_send)

    assert main(["--backend", "jev", "--confirm-jev-call", "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["model_id"] == jev.DEFAULT_MODEL
    assert report["prompt_version"] == "jev-reply-choice-v1"
    assert report["invocations"] == len(sent) == report["count"]
    assert report["results"][0]["input_tokens"] == 50
    assert "test-key" not in json.dumps(report)


def test_the_cascade_needs_both_confirmations(monkeypatch) -> None:
    monkeypatch.setenv("JEV_API_KEY", "test-key")

    assert main(["--backend", "cascade", "--confirm-jev-call"]) == 2
    assert main(["--backend", "cascade", "--confirm-bedrock-call"]) == 2


def _proposer(category: str, confidence: float, calls: list[str], name: str):
    def classify(_text: str) -> tuple[dict, dict]:
        calls.append(name)
        intent = "unit_running" if category == "answer" else "clarify"
        proposal = {"category": category, "intent": intent, "fields": {}, "confidence": confidence}
        return proposal, {"latency_ms": 5, "input_tokens": 10, "output_tokens": 1}

    return classify


def _failing(calls: list[str], name: str):
    def classify(_text: str) -> tuple[dict, dict]:
        calls.append(name)
        raise TimeoutError("timed out")

    return classify


@pytest.mark.parametrize(
    ("first", "calls", "source"),
    [
        pytest.param(("answer", 0.95), ["jev"], "jev", id="confident"),
        pytest.param(("decline", 0.95), ["jev"], "jev", id="confident-clarify"),
        pytest.param(("answer", 0.5), ["jev", "llm"], "llm", id="unsure"),
        pytest.param(None, ["jev", "llm"], "llm", id="failing"),
    ],
)
def test_the_cascade_asks_bedrock_only_when_jev_fails_or_is_unsure(
    first: tuple[str, float] | None, calls: list[str], source: str
) -> None:
    seen: list[str] = []
    jev = _failing(seen, "jev") if first is None else _proposer(*first, seen, "jev")
    classify = cascade(jev, _proposer("answer", 0.9, seen, "llm"))

    _proposal, meta = classify("t")

    assert seen == calls
    assert meta["source"] == source
    answered = [name for name in calls if not (name == "jev" and first is None)]
    assert meta["input_tokens"] == 10 * len(answered)  # a failed call spends nothing


def test_a_cascade_where_both_fail_is_an_unscored_row() -> None:
    seen: list[str] = []
    classify = cascade(_failing(seen, "jev"), _failing(seen, "llm"))
    rows = [{"id": "DR-1", "text": "t", "lang": "en", "expected_intent": "answer"}]

    [row] = score_rows(rows, classify, model_id="m", prompt_version="v")["results"]

    assert seen == ["jev", "llm"]
    assert row["predicted_label"] != "answer" and row["source"] == "none"
