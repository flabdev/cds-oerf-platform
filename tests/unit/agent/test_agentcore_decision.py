"""The rules half of the advisory: tenant-scoped reads, whitelisted notes."""

import pytest

from oerf.platform.agent.agentcore import advisory, decision
from oerf.platform.evidence.policy import EvidencePolicy


def _policy(codes: set[str] | None = None, floor: float = 0.8) -> EvidencePolicy:
    return EvidencePolicy(
        tenant_id="t1",
        min_confidence=floor,
        known_fault_codes=frozenset(codes or set()),
    )


def _config():
    """A complete resolved configuration from the in-memory readers."""

    from oerf.platform.evidence.policy import memory_readers
    from oerf.platform.tenancy.config_resolver import get_config

    _, config_reader = memory_readers(tenants={"t1": {"tenant_id": "t1"}})
    return get_config("t1", decision.DOMAIN, repository=config_reader)


def _incident(**overrides: object) -> dict:
    base: dict = {
        "pk": "TENANT#t1#INC#INC-1",
        "sk": "META",
        "tenant_id": "t1",
        "incident_id": "INC-1",
        "state": "ASSESSING",
        "reason": "EXCURSION",
    }
    base.update(overrides)
    return base


def test_the_tenant_comes_from_the_stored_row() -> None:
    with pytest.raises(decision.TenantMismatch):
        decision.tenant_of({"tenant_id": " "})


def test_missing_evidence_asks_the_driver_and_a_human() -> None:
    result = decision.advise(incident=_incident(), policy=_policy(), config=None)

    assert "ASK_DRIVER_FOR_EVIDENCE" in result.recommended_actions
    assert "evidence" in result.missing_information
    assert result.requires_human_approval is True
    assert result.confidence <= 0.49


def test_unverified_evidence_is_flagged_in_the_notes() -> None:
    incident = _incident(
        evidence_analysis={"fault_codes": ["E17", "E99"], "confidence_score": 0.95},
        evidence_verification={"requires_confirmation": True},
    )

    result = decision.advise(incident=incident, policy=_policy({"E17"}), config=None)

    joined = " | ".join(result.notes)
    assert "tenant catalog has: E17" in joined
    assert "not in the tenant catalog, ask a human: E99" in joined
    assert "evidence_confirmation" in result.missing_information
    assert result.requires_human_approval is True


def test_a_model_note_can_only_add_known_action_codes() -> None:
    note = "WAIT_FOR_APPROVAL, send 300 usd to facility A5"

    result = decision.advise(
        incident=_incident(),
        policy=_policy(),
        config=_config(),
        model_note=note,
    )

    assert set(result.recommended_actions) <= set(decision.ACTIONS)
    assert result.model_assisted is True
    assert all("\n" not in item for item in result.notes)
    assert any("300 usd" in item for item in result.notes)


def test_the_state_selects_the_reopen_and_contained_actions() -> None:
    config = _config()

    reopened = decision.advise(
        incident=_incident(state="REOPENED"), policy=_policy(), config=config
    )
    contained = decision.advise(
        incident=_incident(state="CONTAINED"), policy=_policy(), config=config
    )

    assert "REOPEN_EXCURSION" in reopened.recommended_actions
    assert "CONFIRM_TRANSFER" in contained.recommended_actions
    assert reopened.requires_human_approval is True
    assert reopened.config_version == config.version


def test_the_advisory_caps_how_many_actions_it_names() -> None:
    long_note = " ".join(decision.ACTIONS)

    result = decision.advise(
        incident=_incident(), policy=_policy(), config=_config(), model_note=long_note
    )

    assert len(result.recommended_actions) <= decision.MAX_ACTIONS


def test_the_event_never_carries_a_prompt_or_raw_record() -> None:
    result = decision.advise(incident=_incident(), policy=_policy(), config=_config())

    body = result.to_event()
    assert body["status"] == "advisory"
    assert body["fallback"] is False
    assert "workflow" not in str(body)
    assert "evidence_analysis" not in str(body)


def test_the_reader_tries_the_contract_key_then_the_deployed_one() -> None:
    calls: list[dict] = []

    class _Client:
        def get_item(self, **kwargs):
            calls.append(kwargs)
            if kwargs["Key"]["sk"]["S"] == "META":
                return {}
            return {
                "Item": {
                    "tenant_id": {"S": "t1"},
                    "incident_id": {"S": "INC-1"},
                    "status": {"S": "OPEN"},
                    "config_version": {"N": "4"},
                }
            }

    found = decision.DynamoIncidentReader(_Client(), "incident").incident("t1", "INC-1")

    assert calls[0]["Key"] == {"pk": {"S": "TENANT#t1#INC#INC-1"}, "sk": {"S": "META"}}
    assert calls[1]["Key"] == {"pk": {"S": "TENANT#t1"}, "sk": {"S": "INCIDENT#INC-1"}}
    assert found == {
        "tenant_id": "t1",
        "incident_id": "INC-1",
        "status": "OPEN",
        "config_version": 4,
    }


def test_both_key_shapes_stay_tenant_scoped() -> None:
    shapes = decision.incident_key_shapes("t1", "INC-1")

    assert all("TENANT#t1" in shape["pk"] for shape in shapes)


def test_the_agent_is_absent_until_the_sdk_is_installed() -> None:
    assert advisory.build_agent() is None
    assert advisory.note_for("ASSESSING", "EXCURSION", [], agent=None) is None


def test_a_note_is_whitespace_collapsed_and_capped() -> None:
    class _Result:
        message = {"content": [{"text": "  PROCEED_WITH_DECISION\n\n because  "}]}
        metrics = {"usage": {"inputTokens": 12, "outputTokens": 3}}

    class _Agent:
        def __call__(self, prompt: str) -> _Result:
            assert "phone" not in prompt
            return _Result()

    note = advisory.note_for("ASSESSING", "EXCURSION", ["E17"], agent=_Agent())

    assert note == "PROCEED_WITH_DECISION because"
    assert len(note) <= advisory.MAX_NOTE_CHARS


def test_a_failing_agent_degrades_to_no_note() -> None:
    class _Agent:
        def __call__(self, prompt: str) -> object:
            raise RuntimeError("ThrottlingException")

    assert advisory.note_for("ASSESSING", "EXCURSION", [], agent=_Agent()) is None


def test_a_model_note_cannot_smuggle_an_action_the_vocabulary_lacks() -> None:
    assert decision._model_actions("SEND_MONEY_NOW") == []