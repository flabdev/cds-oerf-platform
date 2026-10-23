"""Auditable incident report assembled only from stored incident data."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ReportOption(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    projected_minutes: float
    spare_minutes: float
    feasible: bool
    drive_min: float


class DecisionTrace(BaseModel):
    """Numeric facts and the rule result that justified the recommendation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    used_minutes: float
    temperature_c: float
    allowance_minutes: float
    upper_c: float
    unload_min: float
    rise_rate_c_per_min: float
    cooldown_rate_c_per_min: float
    min_spare_allowance_min: float
    options: list[ReportOption]
    recommended: str | None = None


class ReportDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    incident_id: str
    shipment_id: str | None = None
    state: str | None = None
    generated_at: str
    temperature_timeline: list[str] = Field(default_factory=list)
    state_timeline: list[str] = Field(default_factory=list)
    diagnosis: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    options: list[ReportOption] = Field(default_factory=list)
    approval: str | None = None
    facility: str | None = None
    actions: list[str] = Field(default_factory=list)
    containment: str | None = None
    qa_disposition: str | None = None
    decision_trace: DecisionTrace | None = None


class ReportReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    report_version: int
    s3_key: str
    sha256: str
    locked: bool
    generated_at: str
    sections: list[str]


SECTIONS = (
    "incident",
    "shipment",
    "temperature_timeline",
    "state_timeline",
    "diagnosis",
    "evidence",
    "options",
    "approval",
    "facility",
    "actions",
    "containment",
    "qa_disposition",
    "decision_trace",
)
