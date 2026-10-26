"""Tenant evidence policy: confidence floor and fault-code catalog.

Rules decide. Vision output is advisory, so it is validated here against the
tenant's own data before any handler stores it:

* the confidence floor is the tenant's ``evidence.min_confidence``;
* a fault code is known only when the tenant has a matching
  ``SK=FAULTCODE#{code}`` row (pattern 5 in ``docs/interfaces.md``).

Neither reader raises on an AWS error. A tenant with no reachable catalog gets
an empty code set, so an unverified code is never presented as verified.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from oerf.platform.observability.logging import get_logger, log
from oerf.platform.tenancy.config_resolver import (
    ConfigError,
    ResolvedConfig,
    deep_merge,
    get_config,
)
from oerf.platform.tenancy.context import TenantContext
from oerf.platform.tenancy.dynamo_config import DynamoConfigRepository

logger = get_logger(__name__)
logger.setLevel(logging.INFO)

#: The domain the cold-chain MVP configures evidence for.
DOMAIN = "cold_chain_excursion"
#: A tenant's catalog entries are keyed under this sort-key prefix, pattern 5.
FAULTCODE_PREFIX = "FAULTCODE#"
#: Used only when a tenant has no readable configuration at all.
FALLBACK_MIN_CONFIDENCE = 0.8

#: The shape the in-memory reader serves. It mirrors the keys ``ResolvedConfig``
#: requires, so a test never reaches the packaged JSON and the suite stays fast.
_MEMORY_DEFAULTS: dict[str, Any] = {
    "domain": DOMAIN,
    "version": 1,
    "status": "ACTIVE",
    "trigger": {"metric": "temperature_c", "condition": "above", "debounce_readings": 2},
    "product_profiles": [
        {
            "id": "PP-2-8-45",
            "name": "2-8C pharma",
            "range_c": [2.0, 8.0],
            "excursion_allowance_min": 45.0,
            "allowance_mode": "cumulative",
        }
    ],
    "assessment": {
        "search_radius_km": 25.0,
        "max_candidates": 3,
        "unload_min": 20.0,
        "rise_rate_c_per_min": 0.5,
        "cooldown_rate_c_per_min": 0.5,
        "min_spare_allowance_min": 10.0,
        "include_continue_option": True,
    },
    "approval": {
        "required_for": ["reroute"],
        "approver_role": "ops_manager",
        "auto_proceed_if_within_limits": False,
        "pre_approved": {"facility_must_be_approved": True, "max_spend": 500.0, "currency": "USD"},
        "escalation_chain": ["ops_manager", "tenant_admin"],
    },
    "verification": {"stable_in_range": "PT10M", "poll_interval": "PT1M", "reopen_on_rise": True},
    "closure": {"closer_role": "qa_manager", "dispositions": ["RELEASED", "REJECTED"]},
    "cadence": {"driver": "PT2M"},
    "callback_roles": {"driver": "ops_user"},
    "callback": {"wait": "PT5M"},
    "communication_profile": "default",
    "calendar": "default",
    "channels": {"priority": ["whatsapp", "sms", "email"], "rcs_for_roles": ["driver"]},
    "evidence": {"min_confidence": FALLBACK_MIN_CONFIDENCE},
    "messaging": {"brand_name": "OERF", "default_language": "en"},
}


class FaultCodeReader(Protocol):
    """The slice of the tenant table the evidence policy reads."""

    def tenant_metadata(self, tenant_id: str) -> dict[str, Any] | None:
        """Return the tenant ``META`` item, or ``None`` when absent."""

    def fault_codes(self, tenant_id: str) -> set[str]:
        """Return the tenant's known fault codes, uppercased."""


class ConfigReader(Protocol):
    """The slice of configuration resolution the policy reads."""

    def read_defaults(self, domain: str) -> dict[str, Any]:
        """Return the published domain defaults."""

    def read_schema(self, domain: str) -> dict[str, Any]:
        """Return the domain schema."""

    def read_tenant_config(self, tenant_id: str, domain: str) -> dict[str, Any]:
        """Return the tenant's active overrides."""


@dataclass(frozen=True)
class EvidencePolicy:
    """What one tenant's evidence rules allow."""

    tenant_id: str
    min_confidence: float
    known_fault_codes: frozenset[str] = field(default_factory=frozenset)
    source: str = "tenant_config"

    def knows_fault_code(self, code: str) -> bool:
        """True when the tenant catalog contains ``code``, case-insensitively."""

        return code.strip().upper() in self.known_fault_codes

    def confirmed(self, confidence: float | None) -> bool:
        """True when the model's confidence clears the tenant floor."""

        return confidence is not None and confidence >= self.min_confidence


def resolve_policy(
    tenant_id: str,
    *,
    tenant: FaultCodeReader | None = None,
    config: ConfigReader | None = None,
) -> EvidencePolicy:
    """Build the tenant's evidence policy from configuration and catalog.

    A missing tenant row, a missing config, or an AWS read error never raises:
    the floor falls back to :data:`FALLBACK_MIN_CONFIDENCE` and the code set
    stays empty, which reads as "nothing verified".
    """

    context = TenantContext(tenant_id=tenant_id)
    codes = _codes(tenant, context.tenant_id)
    resolved = _config(config, context.tenant_id)
    if resolved is None:
        return EvidencePolicy(
            tenant_id=context.tenant_id,
            min_confidence=FALLBACK_MIN_CONFIDENCE,
            known_fault_codes=frozenset(codes),
            source="fallback",
        )
    return EvidencePolicy(
        tenant_id=context.tenant_id,
        min_confidence=resolved.evidence.min_confidence,
        known_fault_codes=frozenset(codes),
        source="tenant_config",
    )


def _codes(tenant: FaultCodeReader | None, tenant_id: str) -> set[str]:
    if tenant is None:
        return set()
    try:
        return {code.strip().upper() for code in tenant.fault_codes(tenant_id) if code.strip()}
    except Exception:  # noqa: BLE001 - a read failure degrades, it never fails the call
        log(
            logger,
            logging.WARNING,
            f"tenant {tenant_id} fault-code catalog unreadable",
            tenant_id=tenant_id,
            outcome="error",
            count=1,
        )
        return set()


def _config(config: ConfigReader | None, tenant_id: str) -> ResolvedConfig | None:
    try:
        return get_config(tenant_id, DOMAIN, repository=config)
    except ConfigError:
        log(
            logger,
            logging.INFO,
            f"tenant {tenant_id} evidence config unavailable",
            tenant_id=tenant_id,
            outcome="unavailable",
            count=1,
        )
        return None


def dynamo_readers(client: Any, tenant_table: str) -> tuple[FaultCodeReader, ConfigReader]:
    """Build the low-level DynamoDB readers over one injected client.

    The client is the boto3 DynamoDB client the caller already owns, so no
    adapter here constructs a client of its own.
    """

    return (
        _DynamoTenantTable(client, tenant_table),
        DynamoConfigRepository(client, tenant_table),
    )


def memory_readers(
    *,
    tenants: Mapping[str, Mapping[str, Any]] | None = None,
    codes: Mapping[str, set[str]] | None = None,
    configs: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[FaultCodeReader, ConfigReader]:
    """Build the readers over in-memory documents, for tests and local runs.

    ``tenants`` are the tenant ``META`` rows and the domains they subscribe to.
    A tenant key with no ``domains`` list subscribes to :data:`DOMAIN`.
    ``configs`` are the per-tenant overrides merged onto the packaged defaults.
    """

    rows = {tenant_id: dict(row) for tenant_id, row in (tenants or {}).items()}
    return (
        _MemoryTenantTable(rows, dict(codes or {})),
        _MemoryConfig(rows, dict(configs or {})),
    )


class _DynamoTenantTable:
    """Tenant ``META`` and ``FAULTCODE#`` reads over the tenant table."""

    def __init__(self, client: Any, table_name: str) -> None:
        self._client = client
        self._table = table_name

    def tenant_metadata(self, tenant_id: str) -> dict[str, Any] | None:
        context = TenantContext(tenant_id=tenant_id)
        response = self._client.get_item(
            TableName=self._table,
            Key=_key(f"TENANT#{context.tenant_id}", "META"),
        )
        return response.get("Item")

    def fault_codes(self, tenant_id: str) -> set[str]:
        context = TenantContext(tenant_id=tenant_id)
        response = self._client.query(
            TableName=self._table,
            KeyConditionExpression="#pk = :pk AND begins_with(#sk, :prefix)",
            ExpressionAttributeNames={"#pk": "pk", "#sk": "sk"},
            ExpressionAttributeValues={
                ":pk": _s(f"TENANT#{context.tenant_id}"),
                ":prefix": _s(FAULTCODE_PREFIX),
            },
        )
        codes: set[str] = set()
        for item in response.get("Items", []):
            codes.update(codes_in(item))
        return codes


class _MemoryTenantTable:
    def __init__(self, tenants: dict[str, Any], codes: dict[str, set[str]]) -> None:
        self._tenants = tenants
        self._codes = codes

    def tenant_metadata(self, tenant_id: str) -> dict[str, Any] | None:
        return self._tenants.get(tenant_id)

    def fault_codes(self, tenant_id: str) -> set[str]:
        return set(self._codes.get(tenant_id, set()))


class _MemoryConfig:
    """A complete in-memory configuration, for tests and local runs.

    ``get_config`` reads through the whole repository protocol: the tenant list,
    the subscribed domains, the defaults, the schema, and the overrides. This
    reader serves one small complete document so a test never depends on the
    packaged JSON.
    """

    def __init__(self, tenants: dict[str, Any], configs: dict[str, Any]) -> None:
        self._tenants = tenants
        self._configs = configs

    def list_tenant_ids(self) -> set[str]:
        return set(self._tenants)

    def subscribed_domains(self, tenant_id: str) -> set[str]:
        row = self._tenants.get(tenant_id, {})
        domains = row.get("domains")
        if isinstance(domains, list) and domains:
            return {str(domain) for domain in domains}
        return {DOMAIN}

    def read_defaults(self, domain: str) -> dict[str, Any]:
        return dict(_MEMORY_DEFAULTS)

    def read_schema(self, domain: str) -> dict[str, Any]:
        return {}

    def read_tenant_config(self, tenant_id: str, domain: str) -> dict[str, Any]:
        return deep_merge(_MEMORY_DEFAULTS, self._configs.get(tenant_id, {}))

    def read_version(self, tenant_id: str, domain: str, version: int) -> dict[str, Any] | None:
        return self.read_tenant_config(tenant_id, domain)


def codes_in(item: Mapping[str, Any]) -> set[str]:
    """Read a fault code from whichever shape a catalog row stores."""

    sk = _attr_text(item.get("sk")) or ""
    found: set[str] = set()
    if sk.startswith(FAULTCODE_PREFIX) and len(sk) > len(FAULTCODE_PREFIX):
        found.add(sk[len(FAULTCODE_PREFIX) :])
    for name in ("code", "fault_code", "id"):
        value = _attr_text(item.get(name))
        if value:
            found.add(value)
    return {code.strip().upper() for code in found if code.strip()}


def _attr_text(attribute: Any) -> str | None:
    if isinstance(attribute, dict):
        value = attribute.get("S")
        return value if isinstance(value, str) else None
    return attribute if isinstance(attribute, str) else None


def _key(pk: str, sk: str) -> dict[str, dict[str, str]]:
    return {"pk": {"S": pk}, "sk": {"S": sk}}


def _s(value: str) -> dict[str, str]:
    return {"S": value}