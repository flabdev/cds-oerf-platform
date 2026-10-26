"""Rules decide the intent. A model note cannot change it or the incident."""

from oerf.platform.cadence.interpret import interpret_workflow, map_reply

_CASES = (
    ("Running", "unit_running", True, {"unit": "running"}),
    ("running", "unit_running", True, {"unit": "running"}),
    ("Stopped", "unit_stopped", True, {"unit": "stopped"}),
    ("Not sure", "unknown", False, {}),
    ("not_sure", "unknown", False, {}),
    ("Call me", "call_me", False, {}),
    ("call_me", "call_me", False, {}),
)


def test_known_replies_map_without_an_advisory() -> None:
    for text, intent, valid, fields in _CASES:
        mapped = map_reply(text)
        assert mapped == (intent, valid, fields), text
        result = interpret_workflow({"reply": {"text": text}}, enabled=True, consult=_refuse)
        assert result["response"]["intent"] == intent
        assert result["response"]["valid"] is valid
        assert result["response"]["fields"] == fields
        assert result["response"]["evidence_refs"] == []
        assert "advisory" not in result["response"]


def test_free_form_text_stays_clarify_and_keeps_only_status() -> None:
    def consult(tenant_id: str, incident_id: str) -> dict[str, str | bool]:
        assert tenant_id == "t1"
        assert incident_id == "INC-000001"
        return {
            "status": "placeholder",
            "fallback": True,
            "recommended_actions": ["divert"],
            "intent": "unit_running",
        }

    result = interpret_workflow(
        {"tenant_id": "t1", "incident_id": "INC-000001", "reply": {"text": "the door is open"}},
        enabled=True,
        consult=consult,
    )

    assert result["response"]["intent"] == "clarify"
    assert result["response"]["valid"] is False
    assert result["response"]["advisory"] == {"status": "placeholder", "fallback": True}
    assert "recommended_actions" not in result["response"]["advisory"]


def test_a_keyword_does_not_consult_and_a_failure_stays_on_the_rule() -> None:
    skipped = interpret_workflow(
        {"reply": {"text": "Running"}, "tenant_id": "t1", "incident_id": "INC-000001"},
        enabled=True,
        consult=_refuse,
    )
    assert skipped["response"]["intent"] == "unit_running"
    assert "advisory" not in skipped["response"]

    def broken(tenant_id: str, incident_id: str) -> dict[str, str]:
        del tenant_id, incident_id
        raise RuntimeError("runtime unavailable")

    failed = interpret_workflow(
        {"tenant_id": "t1", "incident_id": "INC-000001", "reply": {"text": ""}},
        enabled=True,
        consult=broken,
    )
    assert failed["response"]["intent"] == "clarify"
    assert "advisory" not in failed["response"]


def test_advisory_stays_off_unless_agentcore_is_enabled() -> None:
    result = interpret_workflow(
        {"tenant_id": "t1", "incident_id": "INC-000001", "reply": {"text": "hello"}},
        enabled=False,
        consult=_refuse,
    )
    assert result["response"]["intent"] == "clarify"
    assert "advisory" not in result["response"]


def _refuse(tenant_id: str, incident_id: str) -> dict[str, str]:
    del tenant_id, incident_id
    raise AssertionError("a keyword match must not consult the model")
