import json

import pytest

from oerf.platform.cadence.interpret import (
    MAX_REPLY_TEXT,
    interpret_workflow,
    map_reply,
    validate_proposal,
)

_CASES = (
    ("Running", "unit_running", True, {"unit": "running"}),
    ("running", "unit_running", True, {"unit": "running"}),
    ("Stopped", "unit_stopped", True, {"unit": "stopped"}),
    ("Not sure", "unknown", False, {}),
    ("not_sure", "unknown", False, {}),
    ("Call me", "call_me", False, {}),
    ("call_me", "call_me", False, {}),
)


def test_known_replies_bypass_classifier_and_map_deterministically() -> None:
    def refuse(*_args: object) -> object:
        raise AssertionError("known reply must bypass classifier")

    for text, intent, valid, fields in _CASES:
        assert map_reply(text) == (intent, valid, fields)
        response = interpret_workflow({"reply": {"text": text}}, enabled=True, classify=refuse)[
            "response"
        ]
        assert response["intent"] == intent
        assert response["valid"] is valid
        assert response["fields"] == fields
        assert response["evidence_refs"] == []
        assert "category" not in response


def _proposal(**overrides: object) -> dict:
    value = {
        "category": "answer",
        "intent": "unit_running",
        "fields": {"setpoint_temp_c": 4.5},
        "confidence": 0.93,
    }
    value.update(overrides)
    return value


def test_exact_text_and_tenant_scope_reach_classifier() -> None:
    seen: list[tuple] = []

    def classify(tenant: str, incident: str, text: str, policy: str | None) -> object:
        seen.append((tenant, incident, text, policy))
        return _proposal()

    result = interpret_workflow(
        {
            "tenant_id": "tenant-1",
            "incident_id": "INC-9",
            "policy_id": "critical_field",
            "reply": {"text": "The unit sounds like it is running."},
        },
        enabled=True,
        classify=classify,
        confidence_floor=0.8,
    )
    assert seen == [("tenant-1", "INC-9", "The unit sounds like it is running.", "critical_field")]
    assert result["response"] == {
        "category": "answer",
        "intent": "unit_running",
        "valid": True,
        "fields": {"setpoint_temp_c": 4.5},
        "confidence": 0.93,
        "evidence_refs": [],
    }
    assert result["reply"]["text"] == "The unit sounds like it is running."


def test_no_confidence_floor_or_disabled_classifier_fails_closed() -> None:
    def classify(*_args: object) -> object:
        return _proposal()

    workflow = {
        "tenant_id": "t1",
        "incident_id": "i1",
        "policy_id": "critical_field",
        "reply": {"text": "Sounds on"},
    }
    accepted = interpret_workflow(workflow, enabled=True, classify=classify, confidence_floor=0.8)[
        "response"
    ]
    assert accepted["intent"] == "unit_running" and accepted["valid"] is True
    # An explicit ``None`` floor is still refused outright.
    no_floor = interpret_workflow(
        workflow, enabled=True, classify=classify, confidence_floor=None
    )["response"]
    assert no_floor["category"] == "answer"
    assert no_floor["intent"] == "clarify" and no_floor["valid"] is False
    disabled = interpret_workflow(workflow, enabled=False, classify=classify, confidence_floor=0.8)[
        "response"
    ]
    assert disabled["intent"] == "clarify" and disabled["valid"] is False


def test_empty_nonstring_and_oversized_text_never_reach_classifier() -> None:
    calls: list[str] = []

    def classify(_tenant: str, _incident: str, text: str, _policy: str | None) -> object:
        calls.append(text)
        return _proposal()

    for text in (None, "", "  ", 42, "x" * (MAX_REPLY_TEXT + 1)):
        response = interpret_workflow(
            {
                "tenant_id": "t1",
                "incident_id": "i1",
                "policy_id": "critical_field",
                "reply": {"text": text},
            },
            enabled=True,
            classify=classify,
            confidence_floor=0.8,
        )["response"]
        assert response["intent"] == "clarify" and response["valid"] is False
        assert response["fields"] == {} and response["evidence_refs"] == []
    assert calls == []


def test_classifier_exception_does_not_crash_or_leak_exception_text() -> None:
    def broken(*_args: object) -> object:
        raise RuntimeError("private reply and model output")

    response = interpret_workflow(
        {
            "tenant_id": "t1",
            "incident_id": "i1",
            "policy_id": "critical_field",
            "reply": {"text": "private reply"},
        },
        enabled=True,
        classify=broken,
        confidence_floor=0.8,
    )["response"]
    assert response["intent"] == "clarify" and response["valid"] is False
    assert "private reply" not in str(response)


def test_invalid_classifier_proposals_fail_closed() -> None:
    invalid = [
        None,
        "prose",
        {**_proposal(), "extra": "do not expose"},
        _proposal(category="unknown"),
        _proposal(intent="arbitrary-route"),
        _proposal(category="answer", intent="decline"),
        _proposal(confidence=1.01),
        _proposal(confidence=-0.1),
        _proposal(confidence=float("nan")),
        _proposal(fields={"untrusted": "secret"}),
        _proposal(fields={"fault_code": "E17"}),
        {**_proposal(), "valid": True},
    ]
    for proposal in invalid:
        response = validate_proposal(proposal, tenant_id="t1", confidence_floor=0.8)
        assert response["intent"] == "clarify" and response["valid"] is False
        assert response["fields"] == {} and response["evidence_refs"] == []
        assert "secret" not in str(response) and "untrusted" not in str(response)


def test_unknown_answer_and_nonfinite_fields_are_rejected_or_safely_routed() -> None:
    proposal = _proposal(intent="unknown", fields={})
    response = validate_proposal(proposal, tenant_id="t1", confidence_floor=0.8)
    assert response["intent"] == "unknown" and response["valid"] is True
    huge = _proposal(fields={"setpoint_temp_c": 10**10000})
    rejected = validate_proposal(huge, tenant_id="t1", confidence_floor=0.8)
    assert rejected["intent"] == "clarify" and rejected["valid"] is False


def test_fault_code_requires_tenant_scoped_catalog_validation() -> None:
    proposal = _proposal(fields={"fault_code": "E17"})
    accepted = validate_proposal(
        proposal,
        tenant_id="t1",
        confidence_floor=0.8,
        knows_fault_code=lambda tenant, code: tenant == "t1" and code == "E17",
    )
    assert accepted["valid"] is True
    denied = validate_proposal(
        proposal,
        tenant_id="t2",
        confidence_floor=0.8,
        knows_fault_code=lambda tenant, code: tenant == "t1" and code == "E17",
    )
    assert denied["intent"] == "clarify" and denied["fields"] == {}


def test_help_concern_and_unrelated_remain_clarifications() -> None:
    for category in ("help", "concern", "unrelated"):
        response = validate_proposal(
            {"category": category, "intent": "clarify", "fields": {}, "confidence": 0.99},
            tenant_id="t1",
            confidence_floor=0.8,
        )
        assert response["category"] == category
        assert response["intent"] == "clarify" and response["valid"] is False


def test_known_replies_map_without_an_advisory() -> None:
    def refuse(*_args: object) -> object:
        raise AssertionError("known reply must bypass classifier")

    for text, intent, valid, fields in _CASES:
        assert map_reply(text) == (intent, valid, fields), text
        response = interpret_workflow({"reply": {"text": text}}, enabled=True, classify=refuse)[
            "response"
        ]
        assert response["intent"] == intent
        assert response["valid"] is valid
        assert response["fields"] == fields
        assert response["evidence_refs"] == []


def test_free_form_text_fails_closed_without_confidence_policy() -> None:
    def classify(*_args: object) -> object:
        return {"category": "answer", "intent": "unit_running", "fields": {}, "confidence": 0.99}

    result = interpret_workflow(
        {
            "tenant_id": "t1",
            "incident_id": "INC-000001",
            "policy_id": "critical_field",
            "reply": {"text": "door is open"},
        },
        enabled=True,
        classify=classify,
        confidence_floor=None,
    )
    assert result["response"]["intent"] == "clarify"
    assert result["response"]["valid"] is False


def test_the_documented_confidence_floor_is_point_eight() -> None:
    """OERF-211 chooses the numeric floor the frozen contract left open."""

    from oerf.platform.cadence import interpret

    assert interpret.CLASSIFIER_CONFIDENCE_FLOOR == 0.8


def test_a_proposal_below_the_default_floor_fails_closed() -> None:
    def classify(*_args: object) -> object:
        return {"category": "answer", "intent": "unit_running", "fields": {}, "confidence": 0.79}

    workflow = {
        "tenant_id": "t1",
        "incident_id": "INC-000001",
        "policy_id": "critical_field",
        "reply": {"text": "maybe on"},
    }

    # No explicit floor: the module default (0.8) applies.
    below = interpret_workflow(workflow, enabled=True, classify=classify)["response"]
    assert below["intent"] == "clarify" and below["valid"] is False

    # An explicit lower floor accepts the same proposal.
    above = interpret_workflow(
        workflow, enabled=True, classify=classify, confidence_floor=0.75
    )["response"]
    assert above["intent"] == "unit_running" and above["valid"] is True


def test_known_keyword_does_not_invoke_classifier() -> None:
    def refuse(*_args: object) -> object:
        raise AssertionError("known reply must bypass classifier")

    result = interpret_workflow(
        {"reply": {"text": "Running"}, "tenant_id": "t1", "incident_id": "INC-000001"},
        enabled=True,
        classify=refuse,
    )
    assert result["response"]["intent"] == "unit_running"


def test_the_runtime_envelope_is_unwrapped_and_a_fallback_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The AgentCore runtime wraps a proposal; the caller unwraps it.

    A documented fallback (or a missing proposal) raises, so the deterministic
    caller maps the whole call to ``clarify``.
    """

    from oerf.platform.cadence import interpret

    monkeypatch.setenv("AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x")

    class _Client:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def invoke_agent_runtime(self, **_kwargs: object) -> dict:
            return {"response": self._body}

    def _with(body: dict) -> object:
        monkeypatch.setattr(
            "boto3.client", lambda _service, **_kwargs: _Client(json.dumps(body).encode())
        )
        return interpret._runtime_classifier("t1", "INC-1", "the unit is running", None)

    proposal = _with(
        {
            "status": "proposal",
            "fallback": False,
            "proposal": {
                "category": "answer",
                "intent": "unit_running",
                "fields": {},
                "confidence": 0.9,
            },
        }
    )
    assert proposal["intent"] == "unit_running"

    for body in (
        {"status": "placeholder", "fallback": True},
        {"status": "proposal", "fallback": False},
        {"status": "proposal", "fallback": False, "proposal": "prose"},
    ):
        with pytest.raises(ValueError):
            _with(body)


def test_reply_words_follow_the_engagement_policy() -> None:
    """A word outside the policy is clarify, the same as any other unknown reply."""

    driver_accept = interpret_workflow(
        {"policy_id": "critical_field", "reply": {"text": "accept"}},
        enabled=False,
    )
    assert driver_accept["response"]["intent"] == "clarify"
    assert driver_accept["response"]["valid"] is False

    for text, intent in (
        ("accept", "accept"),
        ("accepted", "accept"),
        ("decline", "decline"),
        ("declined", "decline"),
    ):
        facility = interpret_workflow(
            {"policy_id": "partner_request", "reply": {"text": text}},
            enabled=False,
        )
        assert facility["response"]["intent"] == intent, text
        assert facility["response"]["valid"] is True

    for text, intent, fields in (
        ("running", "unit_running", {"unit": "running"}),
        ("stopped", "unit_stopped", {"unit": "stopped"}),
    ):
        driver = interpret_workflow(
            {"policy_id": "critical_field", "reply": {"text": text}},
            enabled=False,
        )
        assert driver["response"]["intent"] == intent
        assert driver["response"]["valid"] is True
        assert driver["response"]["fields"] == fields

    # Policies other than the two scoped ladders keep the combined word list.
    for policy_id in (
        "approval_fast",
        "driver_ack",
        "transfer_confirm",
        "review_normal",
        "notify_only",
    ):
        kept = interpret_workflow(
            {"policy_id": policy_id, "reply": {"text": "accept"}},
            enabled=False,
        )
        assert kept["response"]["intent"] == "accept", policy_id
        assert kept["response"]["valid"] is True


def test_advisory_stays_off_unless_agentcore_is_enabled() -> None:
    result = interpret_workflow(
        {"tenant_id": "t1", "incident_id": "INC-000001", "reply": {"text": "hello"}},
        enabled=False,
    )
    assert result["response"]["intent"] == "clarify"
    assert "advisory" not in result["response"]


def _free_text_event() -> dict:
    return {
        "workflow": {
            "tenant_id": "t1",
            "incident_id": "INC-1",
            "policy_id": "critical_field",
            "reply": {"text": "the unit is running"},
        }
    }


def _running_proposal(**overrides: object) -> dict:
    proposal = {
        "category": "answer",
        "intent": "unit_running",
        "fields": {},
        "confidence": 0.9,
    }
    proposal.update(overrides)
    return proposal


def test_handler_uses_the_tenant_confidence_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    from oerf.platform.cadence import interpret
    from oerf.platform.evidence.policy import EvidencePolicy

    monkeypatch.setenv("AGENTCORE_ENABLED", "true")
    monkeypatch.setattr(interpret, "_runtime_classifier", lambda *_args: _running_proposal())

    def configured(floor: float):
        def policy(tenant_id: str) -> EvidencePolicy:
            return EvidencePolicy(
                tenant_id=tenant_id,
                min_confidence=floor,
                source="tenant_config",
            )

        return policy

    monkeypatch.setattr(interpret, "_tenant_policy", configured(0.95))
    tight = interpret.handler(_free_text_event(), None)["response"]
    assert tight["intent"] == "clarify" and tight["valid"] is False

    monkeypatch.setattr(interpret, "_tenant_policy", configured(0.5))
    loose = interpret.handler(_free_text_event(), None)["response"]
    assert loose["intent"] == "unit_running" and loose["valid"] is True

    monkeypatch.setattr(interpret, "_tenant_policy", lambda _tenant: None)
    missing = interpret.handler(_free_text_event(), None)["response"]
    assert missing["intent"] == "clarify" and missing["valid"] is False

    monkeypatch.setattr(
        interpret,
        "_tenant_policy",
        lambda tenant_id: EvidencePolicy(
            tenant_id=tenant_id, min_confidence=0.8, source="fallback"
        ),
    )
    fallback = interpret.handler(_free_text_event(), None)["response"]
    assert fallback["intent"] == "clarify" and fallback["valid"] is False


def test_handler_accepts_only_the_loaded_tenants_fault_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from oerf.platform.cadence import interpret
    from oerf.platform.evidence.policy import EvidencePolicy

    monkeypatch.setenv("AGENTCORE_ENABLED", "true")
    monkeypatch.setattr(
        interpret,
        "_runtime_classifier",
        lambda *_args: _running_proposal(fields={"fault_code": "E17"}),
    )

    def policy(tenant_id: str) -> EvidencePolicy:
        codes = frozenset({"E17"}) if tenant_id == "t1" else frozenset()
        return EvidencePolicy(
            tenant_id=tenant_id,
            min_confidence=0.8,
            known_fault_codes=codes,
            source="tenant_config",
        )

    monkeypatch.setattr(interpret, "_tenant_policy", policy)
    known = interpret.handler(_free_text_event(), None)["response"]
    assert known["valid"] is True and known["fields"] == {"fault_code": "E17"}

    other = _free_text_event()
    other["workflow"]["tenant_id"] = "t2"
    denied = interpret.handler(other, None)["response"]
    assert denied["intent"] == "clarify" and denied["fields"] == {}

    monkeypatch.setattr(
        interpret,
        "_tenant_policy",
        lambda tenant_id: EvidencePolicy(
            tenant_id=tenant_id,
            min_confidence=0.8,
            known_fault_codes=frozenset(),
            source="tenant_config",
        ),
    )
    absent = interpret.handler(_free_text_event(), None)["response"]
    assert absent["intent"] == "clarify" and absent["fields"] == {}

    class _Unreadable:
        tenant_id = "t1"
        source = "tenant_config"
        min_confidence = 0.8

        def knows_fault_code(self, _code: str) -> bool:
            raise RuntimeError("catalog unreadable")

    monkeypatch.setattr(interpret, "_tenant_policy", lambda _tenant: _Unreadable())
    unreadable = interpret.handler(_free_text_event(), None)["response"]
    assert unreadable["intent"] == "clarify" and unreadable["fields"] == {}


def test_a_classifier_timeout_clarifies_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from oerf.platform.cadence import interpret
    from oerf.platform.evidence.policy import EvidencePolicy

    monkeypatch.setenv("AGENTCORE_ENABLED", "true")

    def timed_out(*_args: object) -> object:
        raise TimeoutError("read timeout")

    monkeypatch.setattr(interpret, "_runtime_classifier", timed_out)
    monkeypatch.setattr(
        interpret,
        "_tenant_policy",
        lambda tenant_id: EvidencePolicy(
            tenant_id=tenant_id, min_confidence=0.8, source="tenant_config"
        ),
    )
    response = interpret.handler(_free_text_event(), None)["response"]
    assert response["intent"] == "clarify" and response["valid"] is False
    assert "timeout" not in str(response)


def test_agentcore_client_timeouts_stay_inside_the_lambda_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from oerf.platform.cadence import interpret

    config = interpret.agentcore_client_config()
    tenant = interpret.tenant_client_config()
    assert config.connect_timeout == 1
    assert config.read_timeout == 10
    assert config.retries["max_attempts"] == 2
    agentcore_budget = 2 * (config.connect_timeout + config.read_timeout)
    tenant_budget = tenant.retries["max_attempts"] * (tenant.connect_timeout + tenant.read_timeout)
    assert agentcore_budget + tenant_budget < 30

    monkeypatch.setenv("AGENTCORE_READ_TIMEOUT", "60")
    monkeypatch.setenv("AGENTCORE_MAX_ATTEMPTS", "9")
    bounded = interpret.agentcore_client_config()
    assert bounded.read_timeout == 10
    assert bounded.retries["max_attempts"] == 2


def test_only_critical_field_free_text_calls_the_classifier() -> None:
    """Facility free text, and a reply with no policy, never reach the model."""

    calls: list[str | None] = []

    def classify(_tenant: str, _incident: str, _text: str, policy: str | None) -> object:
        calls.append(policy)
        return _proposal()

    facility = interpret_workflow(
        {
            "tenant_id": "t1",
            "incident_id": "INC-1",
            "policy_id": "partner_request",
            "reply": {"text": "we can take the truck this afternoon"},
        },
        enabled=True,
        classify=classify,
        confidence_floor=0.8,
    )
    missing = interpret_workflow(
        {
            "tenant_id": "t1",
            "incident_id": "INC-1",
            "reply": {"text": "we can take the truck this afternoon"},
        },
        enabled=True,
        classify=classify,
        confidence_floor=0.8,
    )

    assert calls == []
    assert facility["response"]["intent"] == "clarify" and facility["response"]["valid"] is False
    assert missing["response"]["intent"] == "clarify" and missing["response"]["valid"] is False


def test_critical_field_accepts_an_answer_and_a_callback_but_not_a_decline() -> None:
    def propose(category: str, intent: str):
        def classify(*_args: object) -> object:
            return {
                "category": category,
                "intent": intent,
                "fields": {},
                "confidence": 0.95,
            }

        return classify

    workflow = {
        "tenant_id": "t1",
        "incident_id": "INC-1",
        "policy_id": "critical_field",
        "reply": {"text": "the panel is hard to read"},
    }
    answer = interpret_workflow(
        workflow, enabled=True, classify=propose("answer", "unit_stopped"), confidence_floor=0.8
    )["response"]
    callback = interpret_workflow(
        workflow, enabled=True, classify=propose("call-me", "call_me"), confidence_floor=0.8
    )["response"]
    declined = interpret_workflow(
        workflow, enabled=True, classify=propose("decline", "decline"), confidence_floor=0.8
    )["response"]

    assert answer["intent"] == "unit_stopped" and answer["valid"] is True
    assert callback["intent"] == "call_me" and callback["valid"] is True
    assert declined["category"] == "decline"
    assert declined["intent"] == "clarify" and declined["valid"] is False
    assert declined["fields"] == {}


def test_handler_skips_the_tenant_table_unless_the_policy_is_critical_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from oerf.platform.cadence import interpret

    monkeypatch.setenv("AGENTCORE_ENABLED", "true")

    def refuse_policy(_tenant: str) -> object:
        raise AssertionError("tenant table must stay unread")

    def refuse_classifier(*_args: object) -> object:
        raise AssertionError("classifier must stay uncalled")

    monkeypatch.setattr(interpret, "_tenant_policy", refuse_policy)
    monkeypatch.setattr(interpret, "_runtime_classifier", refuse_classifier)

    for policy_id in (None, "partner_request", "approval_fast"):
        event = _free_text_event()
        if policy_id is None:
            event["workflow"].pop("policy_id")
        else:
            event["workflow"]["policy_id"] = policy_id
        response = interpret.handler(event, None)["response"]
        assert response["intent"] == "clarify" and response["valid"] is False


def test_seeded_t1_fault_codes_are_visible_to_both_readers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgentCore ``policy_for`` and Interpret's tenant read see the same t1 rows."""

    import boto3

    from oerf.platform.agent.agentcore.decision import policy_for
    from oerf.platform.cadence import interpret
    from oerf.platform.evidence.policy import dynamo_readers
    from scripts.seed_runtime import seed_runtime
    from tests.unit.support.fake_dynamo import FakeDynamoClient

    client = FakeDynamoClient()
    seed_runtime(client, "tenant", "t1", apply=True)
    seed_runtime(client, "tenant", "t2", apply=True)
    tenant_reader, config_reader = dynamo_readers(client, "tenant")
    t1_policy = policy_for("t1", tenant=tenant_reader, config=config_reader)
    t2_policy = policy_for("t2", tenant=tenant_reader, config=config_reader)
    assert t1_policy.known_fault_codes == frozenset({"E17", "E05", "E22", "E09"})
    assert t1_policy.knows_fault_code("e17") is True
    assert t1_policy.knows_fault_code("E99") is False
    assert t2_policy.known_fault_codes == frozenset()

    monkeypatch.setenv("TENANT_TABLE_NAME", "tenant")
    monkeypatch.setenv("AGENTCORE_ENABLED", "true")
    monkeypatch.setattr(boto3, "client", lambda *_args, **_kwargs: client)
    loaded = interpret._tenant_policy("t1")
    assert interpret._policy_knows(loaded, "t1", "E17") is True
    assert interpret._policy_knows(loaded, "t1", "E99") is False
    assert interpret._policy_knows(interpret._tenant_policy("t2"), "t2", "E17") is False

    def proposal(code: str) -> dict:
        return {
            "category": "answer",
            "intent": "unit_running",
            "fields": {"fault_code": code},
            "confidence": 0.93,
        }

    monkeypatch.setattr(interpret, "_runtime_classifier", lambda *_args: proposal("E17"))
    accepted = interpret.handler(_free_text_event(), None)["response"]
    assert accepted["valid"] is True
    assert accepted["intent"] == "unit_running"
    assert accepted["fields"] == {"fault_code": "E17"}

    monkeypatch.setattr(interpret, "_runtime_classifier", lambda *_args: proposal("E99"))
    unknown = interpret.handler(_free_text_event(), None)["response"]
    assert unknown["intent"] == "clarify" and unknown["fields"] == {}

    other = _free_text_event()
    other["workflow"]["tenant_id"] = "t2"
    monkeypatch.setattr(interpret, "_runtime_classifier", lambda *_args: proposal("E17"))
    denied = interpret.handler(other, None)["response"]
    assert denied["intent"] == "clarify" and denied["fields"] == {}
