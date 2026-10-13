"""Deterministic excursion-allowance calculation.

The caller supplies the configured allowance and how long the shipment has
already been outside the safe range. This module does not read telemetry or
call any external service.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

AllowanceStatus = Literal["WITHIN_ALLOWANCE", "ALLOWANCE_EXHAUSTED", "ALLOWANCE_EXCEEDED"]


class AllowanceResult(BaseModel):
    """Serializable remaining-allowance decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowance_minutes: float = Field(ge=0)
    elapsed_minutes: float = Field(ge=0)
    remaining_minutes: float
    status: AllowanceStatus

    @field_validator("allowance_minutes", "elapsed_minutes", mode="before")
    @classmethod
    def minutes_must_be_numeric(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("must be a number of minutes")
        return value


def calculate_allowance(allowance_minutes: float, elapsed_minutes: float) -> AllowanceResult:
    """Return remaining allowance and whether it is still available.

    Remaining time is ``allowance_minutes - elapsed_minutes``. Status is
    ``WITHIN_ALLOWANCE`` when time remains, ``ALLOWANCE_EXHAUSTED`` at exactly
    zero, and ``ALLOWANCE_EXCEEDED`` when elapsed time is past the allowance.
    Negative inputs raise ``ValidationError``.
    """

    remaining = allowance_minutes - elapsed_minutes
    if remaining > 0:
        status: AllowanceStatus = "WITHIN_ALLOWANCE"
    elif remaining == 0:
        status = "ALLOWANCE_EXHAUSTED"
    else:
        status = "ALLOWANCE_EXCEEDED"
    return AllowanceResult(
        allowance_minutes=allowance_minutes,
        elapsed_minutes=elapsed_minutes,
        remaining_minutes=remaining,
        status=status,
    )
