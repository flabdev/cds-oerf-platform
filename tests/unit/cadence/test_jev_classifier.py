"""Jev reply classifier and Interpret backend selection. No network, no AWS."""

import json
from typing import Any

import pytest

from oerf.platform.agent.jev import DEFAULT_MODEL
from oerf.platform.cadence import interpret
from oerf.platform.cadence.interpret import interpret_workflow
from oerf.platform.cadence.jev_classifier import (
    CRITERIA,
    PROPOSALS,
    JevReplyClassifier,
    fault_code,
)
from oerf.platform.evidence.policy import EvidencePolicy

CATALOG = frozenset({"E17", "E05", "E22", "E09"})


def _knows(tenant_id: str, code: str) -> bool:
    return tenant_id == "t1" and code in CATALOG


class _Jev:
    """A fake Jev endpoint that answers one choice and records what it was sent."""

    def __init__(self, choice: str = "unit_running", confidence: float = 0.93, **reply: Any):
        self.reply = {
            "model": DEFAULT_MODEL,
            "answers": {"reply": {"type": "choice", "choice": choice, "confidence": confidence}},
            "usage": {"input_tokens": 120, "output_tokens": 0},
            **reply,
        }
        self.status = 200
        self.sent: list[dict[str, Any]] = []

    def __call__(self, url: str, body: bytes, headers: Any, timeout: float) -> tuple[int, bytes]:
        self.sent.append(json.loads(body))
        return self.status, json.dumps(self.reply).encode()


def _classifier(jev: _Jev, traces: list | None = None, **overrides: Any) -> JevReplyClassifier:
    arguments: dict[str, Any] = {
        "api_key": lambda: "k",
        "knows_fault_code": _knows,
        "send": jev,
        "trace": (traces.append if traces is not None else lambda _metadata: None),
    }
    arguments.update(overrides)
    return JevReplyClassifier(**arguments)


def _workflow(text: str, tenant_id: str = "t1") -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "incident_id": "INC-1",
        "policy_id": "critical_field",
        "reply": {"text": text},
    }


def test_every_option_maps_onto_the_adr_042_vocabulary() -> None:
    assert set(CRITERIA) == set(PROPOSALS)
    assert {category for category, _intent in PROPOSALS.values()} == interpret.CATEGORIES
    assert {intent for _category, intent in PROPOSALS.values()} <= interpret.INTENTS


def test_only_the_reply_text_and_the_question_are_sent() -> None:
    jev = _Jev()

    _classifier(jev)("t1", "INC-1", "compressor is on, cooling fine", "critical_field")

    [body] = jev.sent
    assert body["model"] == DEFAULT_MODEL
    assert body["state"]["reply"] == "compressor is on, cooling fine"
    assert "t1" not in json.dumps(body) and "INC-1" not in json.dumps(body)
    assert body["questions"]["reply"]["criteria"] == CRITERIA


@pytest.mark.parametrize(
    ("choice", "intent", "valid"),
    [
        ("unit_running", "unit_running", True),
        ("unit_stopped", "unit_stopped", True),
        ("unknown", "unknown", True),
        ("call_me", "call_me", True),
        ("decline", "clarify", False),
        ("help", "clarify", False),
        ("concern", "clarify", False),
        ("unrelated", "clarify", False),
    ],
)
def test_interpret_validates_each_jev_choice(choice: str, intent: str, valid: bool) -> None:
    response = interpret_workflow(
        _workflow("free text"),
        enabled=True,
        classify=_classifier(_Jev(choice)),
        confidence_floor=0.8,
        knows_fault_code=_knows,
    )["response"]

    assert response["intent"] == intent
    assert response["valid"] is valid
    assert response["category"] == PROPOSALS[choice][0]


def test_the_tenant_floor_applies_to_jev_confidence() -> None:
    def run(confidence: float) -> dict[str, Any]:
        return interpret_workflow(
            _workflow("I think it is running"),
            enabled=True,
            classify=_classifier(_Jev("unit_running", confidence)),
            confidence_floor=0.8,
            knows_fault_code=_knows,
        )["response"]

    assert run(0.79)["intent"] == "clarify"
    accepted = run(0.8)
    assert accepted["intent"] == "unit_running" and accepted["confidence"] == 0.8


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param({"status": 429}, id="rate-limited"),
        pytest.param({"status": 529}, id="overloaded"),
        pytest.param({"model": "jev-1.15.0"}, id="other-version"),
        pytest.param({"choice": "unit_on_fire"}, id="option-not-offered"),
    ],
)
def test_any_jev_failure_clarifies_and_is_traced(failure: dict[str, Any]) -> None:
    jev = _Jev(failure.get("choice", "unit_running"))
    jev.status = failure.get("status", 200)
    if "model" in failure:
        jev.reply["model"] = failure["model"]
    traces: list[dict[str, object]] = []

    response = interpret_workflow(
        _workflow("the unit is running"),
        enabled=True,
        classify=_classifier(jev, traces),
        confidence_floor=0.8,
        knows_fault_code=_knows,
    )["response"]

    assert response == {"intent": "clarify", "valid": False, "fields": {}, "evidence_refs": []}
    [trace] = traces
    assert trace["outcome"] != "OK"


def test_a_successful_call_is_traced_without_the_reply() -> None:
    traces: list[dict[str, object]] = []

    _classifier(_Jev(), traces)("t1", "INC-1", "running, call 98450 00000", "critical_field")

    [trace] = traces
    assert trace == {
        "model_id": DEFAULT_MODEL,
        "latency_ms": trace["latency_ms"],
        "input_tokens": 120,
        "output_tokens": 0,
        "guardrail_action": "NONE",
        "outcome": "OK",
    }
    assert "98450" not in json.dumps(trace)


def test_a_broken_tracer_does_not_fail_the_classification() -> None:
    def broken(_metadata: object) -> None:
        raise OSError("xray down")

    proposal = _classifier(_Jev(), trace=broken)("t1", "INC-1", "running", "critical_field")

    assert proposal["intent"] == "unit_running"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("E17", "E17"),
        ("compressor fault e-17", "E17"),
        ("E 17", "E17"),
        ("E1 7", "E17"),
        ("error e.22 flashing", "E22"),
        ("shows e 0 9", "E09"),
        ("code dikha raha hai E 05", "E05"),
        ("E17 and setpoint shows 5", "E17"),
        ("E99 on the panel", None),
        ("E17 then E22", None),
        ("I 25 km from the depot", None),
        ("reefer is running hot, no code on the display", None),
    ],
)
def test_fault_codes_are_kept_only_when_the_catalog_lists_exactly_one(
    text: str, expected: str | None
) -> None:
    assert fault_code(text, "t1", _knows) == expected


def test_fault_codes_need_a_readable_catalog() -> None:
    def unreadable(_tenant: str, _code: str) -> bool:
        raise OSError("table unavailable")

    assert fault_code("E17", "t1", None) is None
    assert fault_code("E17", "t1", unreadable) is None
    assert fault_code("E17", "t2", _knows) is None


def test_a_known_fault_code_reaches_the_validated_response() -> None:
    response = interpret_workflow(
        _workflow("compressor fault e-17"),
        enabled=True,
        classify=_classifier(_Jev("unknown")),
        confidence_floor=0.8,
        knows_fault_code=_knows,
    )["response"]

    assert response["valid"] is True
    assert response["intent"] == "unknown"
    assert response["fields"] == {"fault_code": "E17"}


# --- Interpret handler: backend selection -----------------------------------------


def _event(text: str = "the compressor seems to be cooling") -> dict[str, Any]:
    return {"workflow": _workflow(text)}


def _tenant_policy(tenant_id: str) -> EvidencePolicy:
    return EvidencePolicy(
        tenant_id=tenant_id,
        min_confidence=0.8,
        known_fault_codes=CATALOG,
        source="tenant_config",
    )


@pytest.fixture
def jev_backend(monkeypatch: pytest.MonkeyPatch) -> _Jev:
    jev = _Jev("unit_running", 0.91)
    monkeypatch.setenv("INTERPRET_CLASSIFIER", "jev")
    monkeypatch.setenv("JEV_API_KEY_PARAM", "/oerf/main/jev/api-key")
    monkeypatch.setattr(interpret, "_tenant_policy", _tenant_policy)
    monkeypatch.setattr(interpret, "_jev_api_key", lambda: "k")

    def refuse_agentcore(*_args: object) -> object:
        raise AssertionError("the jev backend must not call AgentCore")

    monkeypatch.setattr(interpret, "_runtime_classifier", refuse_agentcore)

    original = interpret._jev_classifier

    def with_fake_sender(knows):
        classifier = original(knows)
        classifier._send = jev
        classifier._trace = lambda _metadata: None
        return classifier

    monkeypatch.setattr(interpret, "_jev_classifier", with_fake_sender)
    return jev


def test_the_handler_classifies_with_jev_when_selected(jev_backend: _Jev) -> None:
    response = interpret.handler(_event("compressor e-17, still cooling"), None)["response"]

    assert response["intent"] == "unit_running"
    assert response["valid"] is True
    assert response["confidence"] == 0.91
    assert response["fields"] == {"fault_code": "E17"}
    assert len(jev_backend.sent) == 1


def test_the_handler_keeps_buttons_and_other_policies_off_jev(jev_backend: _Jev) -> None:
    assert interpret.handler(_event("Running"), None)["response"]["intent"] == "unit_running"
    facility = _event("we can take it at dock 3")
    facility["workflow"]["policy_id"] = "partner_request"
    assert interpret.handler(facility, None)["response"]["intent"] == "clarify"

    assert jev_backend.sent == []


def test_jev_is_off_without_a_key_parameter(
    jev_backend: _Jev, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JEV_API_KEY_PARAM")

    assert interpret.handler(_event(), None)["response"]["intent"] == "clarify"
    assert jev_backend.sent == []


def test_an_unknown_backend_disables_free_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERPRET_CLASSIFIER", "gpt")
    monkeypatch.setenv("AGENTCORE_ENABLED", "true")

    def refuse(*_args: object) -> object:
        raise AssertionError("no classifier may run for an unknown backend")

    monkeypatch.setattr(interpret, "_runtime_classifier", refuse)
    monkeypatch.setattr(interpret, "_tenant_policy", refuse)

    assert interpret.handler(_event(), None)["response"]["intent"] == "clarify"


def test_agentcore_stays_the_default_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INTERPRET_CLASSIFIER", raising=False)

    assert interpret.classifier_backend() == "agentcore"


class _Ssm:
    def __init__(self, value: str | None) -> None:
        self.value = value
        self.calls: list[dict[str, Any]] = []

    def get_parameter(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"Parameter": {"Value": self.value}} if self.value is not None else {}


def test_the_key_is_read_decrypted_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    ssm = _Ssm(" secret ")
    monkeypatch.setenv("JEV_API_KEY_PARAM", "/oerf/main/jev/api-key")
    monkeypatch.setattr(interpret, "_jev_key_cache", {})
    monkeypatch.setattr(boto3, "client", lambda *_args, **_kwargs: ssm)

    assert interpret._jev_api_key() == "secret"
    assert interpret._jev_api_key() == "secret"
    assert ssm.calls == [{"Name": "/oerf/main/jev/api-key", "WithDecryption": True}]


def test_an_empty_key_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    ssm = _Ssm("")
    monkeypatch.setenv("JEV_API_KEY_PARAM", "/oerf/main/jev/api-key")
    monkeypatch.setattr(interpret, "_jev_key_cache", {})
    monkeypatch.setattr(boto3, "client", lambda *_args, **_kwargs: ssm)

    with pytest.raises(RuntimeError, match="empty"):
        interpret._jev_api_key()
    ssm.value = "now-set"
    assert interpret._jev_api_key() == "now-set"
