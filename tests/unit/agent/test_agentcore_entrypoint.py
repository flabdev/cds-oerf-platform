"""The AgentCore runtime is advisory-only, and falls back without a workspace.

The runtime contract is frozen in ``docs/interfaces.md`` section 9.1: it reads
the incident and tenant tables with a read-only role, returns an advisory, and
never echoes the invocation body. Anything else is the documented fallback,
``{"status": "placeholder", "fallback": true}``.
"""

import pytest

from oerf.platform.agent.agentcore import app, decision

_ENVELOPE = {
    "tenant_id": "t1",
    "incident_id": "INC-1",
    "task": "diagnose",
    "inputs": {"note": "do-not-echo"},
    "trace_id": "trace-1",
}

_INCIDENT = {
    "pk": "TENANT#t1#INC#INC-1",
    "sk": "META",
    "tenant_id": "t1",
    "incident_id": "INC-1",
    "shipment_id": "PH-2841",
    "state": "ASSESSING",
    "reason": "EXCURSION",
    "config_version": 3,
    "evidence_analysis": {
        "setpoint_temp_c": 5.0,
        "ambient_temp_c": 8.1,
        "fault_codes": ["E17"],
        "confidence_score": 0.91,
    },
    "evidence_verification": {"requires_confirmation": False},
}


class _Reader:
    def __init__(self, item: dict | None) -> None:
        self._item = item

    def incident(self, tenant_id: str, incident_id: str) -> dict | None:
        return self._item


class _Dynamo:
    """Only the two reads the runtime is granted."""

    def __init__(self, codes: list[str] | None = None) -> None:
        self._codes = codes or []

    def get_item(self, **kwargs):
        return {"Item": {"tenant_id": {"S": "t1"}, "domains": {"S": '["cold_chain_excursion"]'}}}

    def query(self, **kwargs):
        return {"Items": [{"sk": {"S": f"FAULTCODE#{code}"}} for code in self._codes]}

    def scan(self, **kwargs):
        return {
            "Items": [
                {
                    "sk": {"S": "META"},
                    "tenant_id": {"S": "t1"},
                    "domains": {"S": '["cold_chain_excursion"]'},
                }
            ]
        }


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No client and no reader by default, so each test opts in explicitly."""

    monkeypatch.setattr(app, "_client", None)
    monkeypatch.setattr(app, "_incidents", None)
    monkeypatch.delenv("INCIDENT_TABLE_NAME", raising=False)
    monkeypatch.delenv("TENANT_TABLE_NAME", raising=False)


def _serve(
    monkeypatch: pytest.MonkeyPatch,
    item: dict | None,
    *,
    client: object | None = None,
    **env: str,
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(app, "_incidents", _Reader(item))
    monkeypatch.setattr(app, "_client", client)


def test_a_body_that_is_not_the_envelope_falls_back() -> None:
    assert app.handle({"task": "diagnose"}) == {"status": "placeholder", "fallback": True}
    assert app.handle("nope") == {"status": "placeholder", "fallback": True}
    assert app.handle(None) == {"status": "placeholder", "fallback": True}
    assert app.handle({**_ENVELOPE, "tenant_id": "  "}) == {
        "status": "placeholder",
        "fallback": True,
    }


def test_without_a_table_or_reader_the_answer_is_the_fallback() -> None:
    assert app.handle(_ENVELOPE) == {"status": "placeholder", "fallback": True}


def test_an_incident_this_tenant_does_not_own_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, {**_INCIDENT, "tenant_id": "t2"}, INCIDENT_TABLE_NAME="incident")

    result = app.handle(_ENVELOPE)

    assert result == {"status": "placeholder", "fallback": True}
    assert "t2" not in str(result)


def test_a_missing_incident_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, None, INCIDENT_TABLE_NAME="incident")

    assert app.handle(_ENVELOPE) == {"status": "placeholder", "fallback": True}


def test_the_advisory_names_actions_and_never_echoes_the_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, _INCIDENT, INCIDENT_TABLE_NAME="incident")

    result = app.handle(_ENVELOPE)
    rendered = str(result)

    assert result["status"] == "advisory"
    assert result["fallback"] is False
    advisory = result["advisory"]
    assert advisory["state"] == "ASSESSING"
    assert advisory["recommended_actions"]
    assert set(advisory["recommended_actions"]) <= set(decision.ACTIONS)
    assert advisory["model_assisted"] is False
    assert advisory["config_version"] == 3
    assert "do-not-echo" not in rendered
    assert "trace-1" not in rendered


def test_a_code_outside_the_tenant_catalog_is_flagged_not_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The tenant catalog knows E99, not the E17 the evidence claims.
    _serve(
        monkeypatch,
        _INCIDENT,
        client=_Dynamo(["E99"]),
        INCIDENT_TABLE_NAME="incident",
        TENANT_TABLE_NAME="",
    )

    advisory = app.handle(_ENVELOPE)["advisory"]

    assert any("not in the tenant catalog" in note for note in advisory["notes"])
    assert advisory["confidence"] <= 0.49


_INTERPRET = {
    "tenant_id": "t1",
    "incident_id": "INC-1",
    "task": "interpret",
    "inputs": {"reply_text": "the unit is running"},
    "trace_id": "trace-1",
}


def test_interpret_without_a_reply_text_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _INCIDENT, INCIDENT_TABLE_NAME="incident")

    for inputs in ({}, {"reply_text": ""}, {"reply_text": 42}, {"reply_text": None}):
        result = app.handle({**_INTERPRET, "inputs": inputs})
        assert result == {"status": "placeholder", "fallback": True}


def test_interpret_returns_the_bare_proposal_and_never_the_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proposal has no ``valid`` flag; Interpret owns that decision."""

    from oerf.platform.agent.agentcore import classifier

    class _Agent:
        def __call__(self, _prompt: str, *, structured_output_model: object) -> object:
            return type(
                "_Result",
                (),
                {
                    "structured_output": structured_output_model(
                        category="answer",
                        intent="unit_running",
                        fields={},
                        confidence=0.9,
                    ),
                    "metrics": {"usage": {"inputTokens": 3, "outputTokens": 4}},
                },
            )()

    _serve(monkeypatch, _INCIDENT, client=_Dynamo([]), INCIDENT_TABLE_NAME="incident")
    monkeypatch.setenv("TENANT_TABLE_NAME", "tenant")
    monkeypatch.setattr(classifier, "build_agent", lambda **_kwargs: _Agent())

    result = app.handle(_INTERPRET)

    assert result["status"] == "proposal"
    assert result["fallback"] is False
    assert result["proposal"] == {
        "category": "answer",
        "intent": "unit_running",
        "fields": {},
        "confidence": 0.9,
    }
    assert "the unit is running" not in str(result)


def test_interpret_falls_back_when_the_classifier_returns_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from oerf.platform.agent.agentcore import classifier

    _serve(monkeypatch, _INCIDENT, client=_Dynamo([]), INCIDENT_TABLE_NAME="incident")
    monkeypatch.setenv("TENANT_TABLE_NAME", "tenant")
    monkeypatch.setattr(classifier, "build_agent", lambda **_kwargs: None)

    assert app.handle(_INTERPRET) == {"status": "placeholder", "fallback": True}


def test_interpret_drops_a_fault_code_the_tenant_catalog_lacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tenant catalog knows E99; the model's E17 must not survive."""

    from oerf.platform.agent.agentcore import classifier

    class _Agent:
        def __call__(self, _prompt: str, *, structured_output_model: object) -> object:
            return type(
                "_Result",
                (),
                {
                    "structured_output": structured_output_model(
                        category="answer",
                        intent="unit_stopped",
                        fields={"fault_code": "E17"},
                        confidence=0.9,
                    ),
                    "metrics": {},
                },
            )()

    _serve(
        monkeypatch,
        _INCIDENT,
        client=_Dynamo(["E99"]),
        INCIDENT_TABLE_NAME="incident",
        TENANT_TABLE_NAME="tenant",
    )
    monkeypatch.setattr(classifier, "build_agent", lambda **_kwargs: _Agent())

    assert app.handle(_INTERPRET) == {"status": "placeholder", "fallback": True}
