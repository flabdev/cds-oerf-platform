"""Tests for remaining excursion allowance."""

import pytest
from pydantic import ValidationError

from oerf.platform.rules.allowance import calculate_allowance


def test_remaining_allowance_within_limit() -> None:
    result = calculate_allowance(allowance_minutes=45, elapsed_minutes=18)

    assert result.model_dump() == {
        "allowance_minutes": 45,
        "elapsed_minutes": 18,
        "remaining_minutes": 27,
        "status": "WITHIN_ALLOWANCE",
    }


def test_same_inputs_are_deterministic() -> None:
    first = calculate_allowance(allowance_minutes=45, elapsed_minutes=18)
    second = calculate_allowance(allowance_minutes=45, elapsed_minutes=18)

    assert first == second


def test_exact_boundary_is_exhausted() -> None:
    result = calculate_allowance(allowance_minutes=45, elapsed_minutes=45)

    assert result.remaining_minutes == 0
    assert result.status == "ALLOWANCE_EXHAUSTED"


def test_elapsed_past_allowance_is_exceeded() -> None:
    result = calculate_allowance(allowance_minutes=45, elapsed_minutes=46)

    assert result.remaining_minutes == -1
    assert result.status == "ALLOWANCE_EXCEEDED"


def test_zero_elapsed_keeps_the_full_allowance() -> None:
    result = calculate_allowance(allowance_minutes=45, elapsed_minutes=0)

    assert result.remaining_minutes == 45
    assert result.status == "WITHIN_ALLOWANCE"


def test_fractional_minutes_are_preserved() -> None:
    result = calculate_allowance(allowance_minutes=45, elapsed_minutes=20.8)

    assert result.remaining_minutes == pytest.approx(24.2)
    assert result.status == "WITHIN_ALLOWANCE"


def test_negative_allowance_is_rejected() -> None:
    with pytest.raises(ValidationError, match="allowance_minutes"):
        calculate_allowance(allowance_minutes=-1, elapsed_minutes=0)


def test_negative_elapsed_is_rejected() -> None:
    with pytest.raises(ValidationError, match="elapsed_minutes"):
        calculate_allowance(allowance_minutes=45, elapsed_minutes=-0.1)


def test_result_round_trips_through_json() -> None:
    result = calculate_allowance(allowance_minutes=45, elapsed_minutes=18)

    restored = result.model_validate_json(result.model_dump_json())

    assert restored == result
