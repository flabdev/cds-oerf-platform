"""Decide whether a temperature reading should open an operational incident.

Thresholds come from the resolved tenant configuration. This module does not
store incidents.
"""

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

TriggerReason = Literal["TEMPERATURE_ABOVE_MAX", "TEMPERATURE_BELOW_MIN"]


class TemperatureEvent(BaseModel):
    """One normalized temperature reading."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    shipment_id: str
    temperature_c: float
    timestamp: str

    @field_validator("tenant_id", "shipment_id", "timestamp")
    @classmethod
    def required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must be a non-empty string")
        return stripped


class ProductProfileView(Protocol):
    range_c: list[float]


class TriggerView(Protocol):
    condition: str
    debounce_readings: int


class DomainConfigView(Protocol):
    trigger: TriggerView

    def product_profile(self, profile_id: str) -> ProductProfileView:
        """Return the product profile that owns the temperature range."""


class TriggerDecision(BaseModel):
    """Serializable trigger result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    triggered: bool
    reason: TriggerReason | None
    tenant_id: str
    shipment_id: str


def evaluate_trigger(
    event: TemperatureEvent | dict[str, object],
    config: DomainConfigView,
    *,
    profile_id: str,
    consecutive_breaches: int = 1,
) -> TriggerDecision:
    """Compare a reading with the configured range.

    ``triggered`` is true only when the reading is outside the range the
    trigger condition cares about and ``consecutive_breaches`` has reached
    ``debounce_readings``. Exact range endpoints are inside the safe band.
    """

    if isinstance(event, TemperatureEvent):
        reading = event
    else:
        reading = TemperatureEvent.model_validate(event)
    if consecutive_breaches < 1:
        raise ValidationError.from_exception_data(
            title=TriggerDecision.__name__,
            line_errors=[
                {
                    "type": "greater_than_equal",
                    "loc": ("consecutive_breaches",),
                    "input": consecutive_breaches,
                    "ctx": {"ge": 1},
                }
            ],
        )
    reason = breach_reason(reading, config, profile_id=profile_id)
    triggered = reason is not None and consecutive_breaches >= config.trigger.debounce_readings
    return TriggerDecision(
        triggered=triggered,
        reason=reason if triggered else None,
        tenant_id=reading.tenant_id,
        shipment_id=reading.shipment_id,
    )


def breach_reason(
    event: TemperatureEvent | dict[str, object],
    config: DomainConfigView,
    *,
    profile_id: str,
) -> TriggerReason | None:
    """Return the configured breach reason before applying debounce policy."""
    reading = (
        event if isinstance(event, TemperatureEvent) else TemperatureEvent.model_validate(event)
    )
    profile = config.product_profile(profile_id)
    lower, upper = profile.range_c
    return _breach_reason(
        config.trigger.condition,
        reading.temperature_c,
        lower,
        upper,
    )


def _breach_reason(
    condition: str,
    temperature_c: float,
    lower: float,
    upper: float,
) -> TriggerReason | None:
    if condition == "above_upper":
        if temperature_c > upper:
            return "TEMPERATURE_ABOVE_MAX"
        return None
    if condition == "outside_range":
        if temperature_c > upper:
            return "TEMPERATURE_ABOVE_MAX"
        if temperature_c < lower:
            return "TEMPERATURE_BELOW_MIN"
        return None
    raise ValueError(f"unsupported trigger condition {condition!r}")
