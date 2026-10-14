"""Deterministic ranking of cold-chain recovery options.

All rates, limits, and candidate data come from the caller. This module does
not name facilities or read configuration files.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OptionKind = Literal["facility", "continue"]


class AssessmentContext(BaseModel):
    """Numeric inputs for one option projection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    upper_c: float
    allowance_minutes: float = Field(ge=0)
    used_minutes: float = Field(ge=0)
    temperature_c: float
    unload_min: float = Field(ge=0)
    rise_rate_c_per_min: float = Field(gt=0)
    cooldown_rate_c_per_min: float = Field(gt=0)
    min_spare_allowance_min: float = Field(ge=0)


class RecoveryOption(BaseModel):
    """One candidate destination supplied by the caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    drive_min: float = Field(ge=0)
    capacity_available: bool = True
    kind: OptionKind = "facility"


class RankedOption(BaseModel):
    """Projection and feasibility for one candidate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    drive_min: float
    kind: OptionKind
    projected_minutes: float
    spare_minutes: float
    feasible: bool
    rank: int | None = None


class RankingResult(BaseModel):
    """Ranked recovery options. ``recommended_option`` is set only when one is feasible."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    options: list[RankedOption]
    recommended_option: str | None


def rank_options(context: AssessmentContext, options: list[RecoveryOption]) -> RankingResult:
    """Project, judge, and rank recovery options.

    A facility is feasible when it has capacity and its spare allowance is at
    least the configured minimum. Feasible options are ordered by spare
    descending, then shorter drive time, then id. Continue options do not
    receive cooldown credit.
    """

    projected = [_project(context, option) for option in options]
    feasible = [item for item in projected if item.feasible]
    infeasible = [item for item in projected if not item.feasible]
    ordered = sorted(feasible, key=lambda item: (-item.spare_minutes, item.drive_min, item.id))
    ranked = [
        item.model_copy(update={"rank": index})
        for index, item in enumerate(ordered, start=1)
    ]
    recommended = ranked[0].id if ranked else None
    return RankingResult(options=[*ranked, *infeasible], recommended_option=recommended)


def _project(context: AssessmentContext, option: RecoveryOption) -> RankedOption:
    if option.kind == "continue":
        projected = context.used_minutes + option.drive_min + context.unload_min
    else:
        travel_min = option.drive_min + context.unload_min
        peak = context.temperature_c + context.rise_rate_c_per_min * travel_min
        above = max(0.0, peak - context.upper_c)
        cooldown = above / context.cooldown_rate_c_per_min
        projected = context.used_minutes + travel_min + cooldown
    spare = context.allowance_minutes - projected
    feasible = option.capacity_available and spare >= context.min_spare_allowance_min
    return RankedOption(
        id=option.id,
        drive_min=option.drive_min,
        kind=option.kind,
        projected_minutes=projected,
        spare_minutes=spare,
        feasible=feasible,
    )
