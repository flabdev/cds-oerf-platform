"""Vision output is advisory: the schema is checked and codes are tenant-scoped."""

import pytest

from oerf.platform.evidence import analysis


def _reading(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "setpoint_temp_c": 5.0,
        "ambient_temp_c": 8.1,
        "fault_codes": ["E17"],
        "alarm_status": "ACTIVE",
        "door_alarm_status": "CLOSED",
        "power_status": "RUNNING",
        "confidence_score": 0.91,
        "display_readout_summary": "E17 on the panel",
    }
    base.update(overrides)
    return base


def test_a_verdict_reads_the_tenant_catalog_case_insensitively() -> None:
    result = analysis.validate(
        _reading(fault_codes=["e17", "ZZ9"]),
        known_fault_codes=frozenset({"E17"}),
        min_confidence=0.8,
    )

    assert result.verified_fault_codes == ["E17"]
    assert result.unverified_fault_codes == ["ZZ9"]
    assert result.observed_fault_codes == ["E17", "ZZ9"]
    assert result.requires_confirmation is True


def test_an_unknown_code_alone_asks_for_a_human() -> None:
    result = analysis.validate(
        _reading(fault_codes=["ZZ9"]),
        known_fault_codes=frozenset({"E17"}),
        min_confidence=0.8,
    )

    assert result.verified_fault_codes == []
    assert result.unverified_fault_codes == ["ZZ9"]
    assert result.verification["requires_confirmation"] is True


def test_confidence_below_the_tenant_floor_asks_for_a_human() -> None:
    result = analysis.validate(
        _reading(confidence_score=0.4),
        known_fault_codes=frozenset({"E17"}),
        min_confidence=0.8,
    )

    assert result.requires_confirmation is True
    assert result.confidence == 0.4


def test_a_confident_known_reading_is_stored_as_is() -> None:
    result = analysis.validate(
        _reading(),
        known_fault_codes=frozenset({"E17"}),
        min_confidence=0.8,
    )

    assert result.requires_confirmation is False
    assert result.readable is True
    assert result.analysis["fault_codes"] == ["E17"]
    assert result.analysis["setpoint_temp_c"] == 5.0


def test_an_unreadable_reading_is_marked_unreadable() -> None:
    result = analysis.validate(
        _reading(setpoint_temp_c=None, ambient_temp_c=None, fault_codes=[], confidence_score=0.2),
        known_fault_codes=frozenset({"E17"}),
        min_confidence=0.8,
    )

    assert result.readable is False
    assert result.requires_confirmation is True


@pytest.mark.parametrize(
    "candidate",
    [
        "not a dict",
        [],
        {"setpoint_temp_c": 5.0},
        _reading(alarm_status="maybe"),
        _reading(power_status="FLYING"),
        _reading(door_alarm_status="AJAR"),
        _reading(fault_codes="E17"),
        _reading(fault_codes=[17]),
        _reading(confidence_score=1.5),
        _reading(setpoint_temp_c="cold"),
        _reading(display_readout_summary=5),
    ],
)
def test_a_shape_the_prompt_did_not_ask_for_is_rejected(candidate: object) -> None:
    with pytest.raises(analysis.AnalysisRejected):
        analysis.validate(candidate)


def test_the_prompt_schema_and_the_validator_agree_on_the_field_set() -> None:
    from oerf.platform.evidence import vision

    for name in analysis.FIELDS:
        assert name in vision.VISION_SYSTEM_PROMPT