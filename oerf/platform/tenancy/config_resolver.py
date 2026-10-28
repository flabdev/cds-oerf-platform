"""Resolve a tenant's effective domain configuration from files.

Domain numbers live in JSON. This module merges tenant overrides onto domain
defaults and validates the result. It does not snapshot the config onto an
incident, and it does not call AWS.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from oerf.platform.tenancy.context import TenantContext


class ConfigError(Exception):
    """Base error for configuration resolution."""


class TenantNotFound(ConfigError):
    """The requested tenant has no seed or stored record."""


class DomainNotFound(ConfigError):
    """The domain is unknown, or the tenant is not subscribed to it."""


class ConfigInvalid(ConfigError):
    """The merged configuration does not match the domain schema."""


class ProductProfile(BaseModel):
    """Temperature range and excursion allowance for one product."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    range_c: list[float]
    excursion_allowance_min: float
    allowance_mode: str

    @field_validator("range_c")
    @classmethod
    def range_must_be_ordered(cls, value: list[float]) -> list[float]:
        if len(value) != 2 or value[0] >= value[1]:
            raise ValueError("range_c must be [min, max] with min < max")
        return value


class TriggerConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: str
    condition: str
    debounce_readings: int


class AssessmentConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    search_radius_km: float
    max_candidates: int
    unload_min: float
    rise_rate_c_per_min: float
    stopped_rise_rate_c_per_min: float | None = None
    cooldown_rate_c_per_min: float
    min_spare_allowance_min: float
    include_continue_option: bool


class PreApprovedLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    facility_must_be_approved: bool
    max_spend: float
    currency: str


class ApprovalConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    required_for: list[str]
    approver_role: str
    auto_proceed_if_within_limits: bool
    pre_approved: PreApprovedLimits
    escalation_chain: list[str]


class VerificationConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stable_in_range: str
    poll_interval: str
    reopen_on_rise: bool


class ClosureConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    closer_role: str
    dispositions: list[str]


class CallbackConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    wait: str


class ChannelConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    priority: list[str]
    rcs_for_roles: list[str]


class EvidenceConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    min_confidence: float


class MessagingConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    brand_name: str
    default_language: str


class ResolvedConfig(BaseModel):
    """Effective domain configuration for one tenant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    domain: str
    version: int
    status: str
    trigger: TriggerConfig
    product_profiles: list[ProductProfile]
    assessment: AssessmentConfig
    approval: ApprovalConfig
    verification: VerificationConfig
    closure: ClosureConfig
    cadence: dict[str, str]
    callback_roles: dict[str, str]
    callback: CallbackConfig
    communication_profile: str
    calendar: str
    channels: ChannelConfig
    evidence: EvidenceConfig
    messaging: MessagingConfig

    def product_profile(self, profile_id: str) -> ProductProfile:
        for profile in self.product_profiles:
            if profile.id == profile_id:
                return profile
        raise ConfigInvalid(f"product profile {profile_id!r} is not in the resolved configuration")


class ConfigRepository(Protocol):
    """Storage behind configuration resolution.

    The file repository is the current implementation. A later DynamoDB
    adapter can satisfy the same methods.
    """

    def list_tenant_ids(self) -> set[str]:
        """Return the tenants this store knows about."""

    def subscribed_domains(self, tenant_id: str) -> set[str]:
        """Return the domains a tenant is subscribed to."""

    def read_defaults(self, domain: str) -> dict[str, Any]:
        """Return the domain template defaults."""

    def read_schema(self, domain: str) -> dict[str, Any]:
        """Return the JSON Schema for a domain configuration."""

    def read_tenant_config(self, tenant_id: str, domain: str) -> dict[str, Any]:
        """Return tenant overrides. Missing overrides are an empty mapping."""


class FileConfigRepository:
    """Read domain templates and tenant overrides shipped with the package.

    Pass ``root`` only in tests that use a temporary checkout layout
    (``root/domains`` and ``root/sim/seed``). The default reads the installed
    ``domains`` and ``sim`` packages.
    """

    def __init__(self, root: Path | None = None) -> None:
        if root is None:
            self.domains_dir = _package_path("domains")
            self.seed_dir = _package_path("sim") / "seed"
        else:
            self.domains_dir = root / "domains"
            self.seed_dir = root / "sim" / "seed"

    def list_tenant_ids(self) -> set[str]:
        if not self.seed_dir.is_dir():
            return set()
        return {path.name for path in self.seed_dir.iterdir() if (path / "tenant.json").is_file()}

    def subscribed_domains(self, tenant_id: str) -> set[str]:
        document = _read_json(self.seed_dir / tenant_id / "tenant.json")
        domains = document.get("domains", [])
        if not isinstance(domains, list):
            raise ConfigInvalid(f"tenant {tenant_id!r} has an invalid domains list")
        return {str(domain) for domain in domains}

    def read_defaults(self, domain: str) -> dict[str, Any]:
        path = self.domains_dir / domain / "defaults.json"
        if not path.is_file():
            raise DomainNotFound(f"domain {domain!r} does not exist")
        return _read_object(path, kind="defaults")

    def read_schema(self, domain: str) -> dict[str, Any]:
        path = self.domains_dir / domain / "schema.json"
        if not path.is_file():
            raise ConfigInvalid(f"domain {domain!r} has no schema")
        return _read_object(path, kind="schema")

    def read_tenant_config(self, tenant_id: str, domain: str) -> dict[str, Any]:
        path = self.seed_dir / tenant_id / "domains" / f"{domain}.json"
        if not path.is_file():
            return {}
        document = _read_object(path, kind="tenant config")
        for key in ("tenant_id", "PK", "SK"):
            document.pop(key, None)
        return document


def get_config(
    tenant_id: str,
    domain: str,
    *,
    repository: ConfigRepository | None = None,
) -> ResolvedConfig:
    """Return the validated configuration for a tenant and domain.

    Tenant overrides replace domain defaults. Nested objects merge; lists and
    scalars from the tenant replace the default value.
    """

    context = TenantContext(tenant_id=tenant_id)
    domain_name = domain.strip()
    if not domain_name:
        raise DomainNotFound("domain must be a non-empty string")

    store = repository or FileConfigRepository()
    if context.tenant_id not in store.list_tenant_ids():
        raise TenantNotFound(f"tenant {context.tenant_id!r} does not exist")
    if domain_name not in store.subscribed_domains(context.tenant_id):
        raise DomainNotFound(
            f"tenant {context.tenant_id!r} is not subscribed to domain {domain_name!r}"
        )

    merged = deep_merge(
        store.read_defaults(domain_name),
        store.read_tenant_config(context.tenant_id, domain_name),
    )
    _validate_schema(merged, store.read_schema(domain_name), domain_name)
    try:
        return ResolvedConfig.model_validate({**merged, "tenant_id": context.tenant_id})
    except ValidationError as exc:
        raise ConfigInvalid(str(exc)) from exc


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Merge mappings. Nested mappings combine; other values are replaced."""

    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _package_path(package: str) -> Path:
    """Return the installed directory for a data package."""

    return Path(str(files(package)))


def find_repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise ConfigError("repository root not found")


def _validate_schema(document: Mapping[str, Any], schema: Mapping[str, Any], domain: str) -> None:
    try:
        validator = Draft202012Validator(schema)
    except SchemaError as exc:
        raise ConfigInvalid(f"domain {domain!r} schema is invalid: {exc.message}") from exc
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(error.message for error in errors)
        raise ConfigInvalid(f"domain {domain!r} configuration is invalid: {details}")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigInvalid(f"missing configuration file {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigInvalid(f"configuration file {path} is not valid JSON") from exc


def _read_object(path: Path, *, kind: str) -> dict[str, Any]:
    document = _read_json(path)
    if not isinstance(document, dict):
        raise ConfigInvalid(f"{kind} file {path} must contain a JSON object")
    return document
