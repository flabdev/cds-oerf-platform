"""Tests for the temperature trigger evaluator."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from oerf.platform.rules.trigger import evaluate_trigger
from oerf.platform.tenancy.config_resolver import get_config


def test_two_breaches_above_the_configured_maximum_trigger() -> None:
    config = get_config(tenant_id="t1", domain="cold_chain_excursion")
    upper = config.product_profile("PP-2-8-45").range_c[1]
    event = _event(upper + 0.1)

    decision = evaluate_trigger(
        event,
        config,
        profile_id="PP-2-8-45",
        consecutive_breaches=config.trigger.debounce_readings,
    )

    assert decision.triggered is True
    assert decision.reason == "TEMPERATURE_ABOVE_MAX"
    assert decision.model_dump()["shipment_id"] == "PH-2841"


def test_one_breach_does_not_meet_debounce() -> None:
    config = get_config(tenant_id="t1", domain="cold_chain_excursion")
    upper = config.product_profile("PP-2-8-45").range_c[1]

    decision = evaluate_trigger(
        _event(upper + 0.1),
        config,
        profile_id="PP-2-8-45",
        consecutive_breaches=1,
    )

    assert decision.triggered is False
    assert decision.reason is None


def test_reading_inside_the_range_does_not_trigger() -> None:
    config = get_config(tenant_id="t1", domain="cold_chain_excursion")
    lower, upper = config.product_profile("PP-2-8-45").range_c

    for temperature in (lower, (lower + upper) / 2, upper):
        decision = evaluate_trigger(
            _event(temperature),
            config,
            profile_id="PP-2-8-45",
            consecutive_breaches=config.trigger.debounce_readings,
        )
        assert decision.triggered is False


def test_above_upper_ignores_a_low_reading() -> None:
    config = get_config(tenant_id="t1", domain="cold_chain_excursion")
    lower = config.product_profile("PP-2-8-45").range_c[0]

    decision = evaluate_trigger(
        _event(lower - 1),
        config,
        profile_id="PP-2-8-45",
        consecutive_breaches=config.trigger.debounce_readings,
    )

    assert decision.triggered is False


def test_outside_range_triggers_below_the_minimum() -> None:
    config = _Config(condition="outside_range", debounce=1)

    decision = evaluate_trigger(
        _event(config.profile.range_c[0] - 1),
        config,
        profile_id="PP-2-8-45",
        consecutive_breaches=1,
    )

    assert decision.triggered is True
    assert decision.reason == "TEMPERATURE_BELOW_MIN"


def test_invalid_event_is_rejected() -> None:
    config = get_config(tenant_id="t1", domain="cold_chain_excursion")

    with pytest.raises(ValidationError):
        evaluate_trigger(
            {"tenant_id": "t1", "temperature_c": 5, "timestamp": "2026-10-20T10:16:00Z"},
            config,
            profile_id="PP-2-8-45",
        )


def test_evaluator_does_not_hard_code_the_threshold() -> None:
    source = Path("oerf/platform/rules/trigger.py").read_text(encoding="utf-8")

    assert "8" not in source


class _Profile:
    def __init__(self) -> None:
        self.range_c = [2.0, 8.0]


class _Trigger:
    def __init__(self, condition: str, debounce: int) -> None:
        self.condition = condition
        self.debounce_readings = debounce


class _Config:
    def __init__(self, condition: str, debounce: int) -> None:
        self.trigger = _Trigger(condition, debounce)
        self.profile = _Profile()

    def product_profile(self, profile_id: str) -> _Profile:
        return self.profile


def _event(temperature_c: float) -> dict[str, object]:
    return {
        "tenant_id": "t1",
        "shipment_id": "PH-2841",
        "temperature_c": temperature_c,
        "timestamp": "2026-10-20T10:16:00Z",
    }
