"""Golden tests for recovery-option projection."""

from pathlib import Path

import pytest

from oerf.platform.rules.options import (
    AssessmentContext,
    RecoveryOption,
    rank_options,
)


def test_ph2841_ranks_facility_a_first() -> None:
    result = rank_options(_ph2841(), _ph2841_options())

    by_id = {option.id: option for option in result.options}
    assert by_id["FACILITY_A"].projected_minutes == pytest.approx(20.8, abs=0.1)
    assert by_id["FACILITY_B"].projected_minutes == pytest.approx(35.8, abs=0.1)
    assert by_id["DESTINATION"].projected_minutes == pytest.approx(50.0, abs=0.1)
    assert by_id["FACILITY_A"].feasible is True
    assert by_id["FACILITY_A"].rank == 1
    assert by_id["FACILITY_B"].feasible is True
    assert by_id["DESTINATION"].feasible is False
    assert by_id["DESTINATION"].rank is None
    assert result.recommended_option == "FACILITY_A"


def test_facility_without_capacity_is_skipped() -> None:
    options = _ph2841_options()
    options[0] = options[0].model_copy(update={"capacity_available": False})

    result = rank_options(_ph2841(), options)

    assert result.recommended_option == "FACILITY_B"
    facility_a = next(option for option in result.options if option.id == "FACILITY_A")
    assert facility_a.feasible is False


def test_no_feasible_option_has_no_recommendation() -> None:
    context = _ph2841().model_copy(update={"allowance_minutes": 10})

    result = rank_options(context, _ph2841_options())

    assert result.recommended_option is None
    assert all(option.feasible is False for option in result.options)


def test_equal_spare_prefers_the_shorter_drive() -> None:
    drive = 9
    peak = 8.19 + 0.03 * (drive + 4)
    continue_eta = drive + (peak - 8) / 0.12
    context = _ph2841().model_copy(update={"min_spare_allowance_min": 0, "allowance_minutes": 100})
    options = [
        RecoveryOption(id="CONTINUE", drive_min=continue_eta, kind="continue"),
        RecoveryOption(id="NEAR", drive_min=drive),
    ]

    result = rank_options(context, options)

    assert result.options[0].id == "NEAR"
    assert result.options[0].spare_minutes == pytest.approx(result.options[1].spare_minutes)
    assert result.recommended_option == "NEAR"


def test_rules_module_has_no_facility_names() -> None:
    source = Path("oerf/platform/rules/options.py").read_text(encoding="utf-8")

    assert "FACILITY_A" not in source
    assert "PH-2841" not in source


def _ph2841() -> AssessmentContext:
    return AssessmentContext(
        upper_c=8,
        allowance_minutes=45,
        used_minutes=3,
        temperature_c=8.19,
        unload_min=4,
        rise_rate_c_per_min=0.03,
        cooldown_rate_c_per_min=0.12,
        min_spare_allowance_min=5,
    )


def _ph2841_options() -> list[RecoveryOption]:
    return [
        RecoveryOption(id="FACILITY_A", drive_min=9),
        RecoveryOption(id="FACILITY_B", drive_min=21),
        RecoveryOption(id="DESTINATION", drive_min=43, kind="continue"),
    ]
