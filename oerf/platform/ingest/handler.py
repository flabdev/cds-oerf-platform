"""EventBridge temperature ingestion for cold-chain incidents.

The device registry is authoritative for tenant/shipment identity. The
EventBridge ``detail`` is validated against it, then tenant configuration,
debounce, trigger evaluation, incident locking, and execution start run through
injected ports. No AWS client is created at import time.

This module is the deployed ``oerf-ingest`` entry point
(``oerf.platform.ingest.handler.handler``). The compute stack already wires the
EventBridge rule for ``sim.reefer`` / ``TemperatureReading``, the dead-letter
queue, ``states:StartExecution`` on the cold-chain state machine, and the
environment below. Ingest state is kept on the incident table because that is
the only table this role may write: routing and tenant access is read-only.
While the shipment lock is present, each accepted reading is one ``READ#``
row, written by ``IncidentStore.record_reading``.

``TODO(OERF-301)``: no DynamoDB TTL is configured on any table, so the
``expires_at`` attribute on dedupe and debounce items does not delete them yet.
Device rows are written by ``oerf.platform.channels.devices.register_device``.
Ingest only reads them and still drops an unknown device.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import boto3
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from oerf.platform.incidents.debounce import DebounceStore, observe
from oerf.platform.incidents.dynamo import DynamoIncidentStore
from oerf.platform.incidents.repository import IncidentStore
from oerf.platform.observability.logging import get_logger, log
from oerf.platform.rules.trigger import TemperatureEvent, breach_reason, evaluate_trigger
from oerf.platform.storage.dynamo import is_conditional_failure, iso_z, key, n, text
from oerf.platform.tenancy.config_resolver import ResolvedConfig, get_config
from oerf.platform.tenancy.dynamo_config import DynamoConfigRepository

logger = get_logger(__name__)
logger.setLevel(logging.INFO)

SOURCE = "sim.reefer"
DETAIL_TYPE = "TemperatureReading"
DOMAIN = "cold_chain_excursion"
EVENT_RETENTION_SECONDS = 7 * 24 * 60 * 60
_EXECUTION_NAME = re.compile(r"[^A-Za-z0-9_-]")


class Reading(BaseModel):
    """Validated EventBridge Detail; tenant and shipment are untrusted claims."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str | None = None
    shipment_id: str | None = None
    device_id: str = Field(min_length=1)
    ts: datetime
    temperature_c: float
    lat: float | None = None
    lon: float | None = None
    seq: int = Field(ge=0)

    @field_validator("device_id")
    @classmethod
    def _required_device_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("device_id must be non-empty")
        return normalized

    @field_validator("temperature_c", "lat", "lon")
    @classmethod
    def _finite_number(cls, value: float | None) -> float | None:
        if value is not None and not (-1e9 < value < 1e9):
            raise ValueError("numeric reading fields must be finite")
        return value


class DeviceResolver(Protocol):
    def resolve(self, device_id: str) -> tuple[str, str] | None:
        """Return trusted ``(tenant_id, shipment_id)`` for one registered device."""


class ShipmentReader(Protocol):
    def get(self, tenant_id: str, shipment_id: str) -> dict[str, Any] | None:
        """Load one tenant-scoped shipment record."""


class ConfigReader(Protocol):
    def get(self, tenant_id: str, domain: str) -> ResolvedConfig:
        """Resolve current tenant configuration."""


class ReadingDedupe(Protocol):
    def register(self, event_id: str, *, now: datetime) -> bool:
        """Return true for a new event, false for a still-retained duplicate."""

    def forget(self, event_id: str) -> None:
        """Release a newly inserted event id when processing fails."""


class ExecutionStarter(Protocol):
    def start(self, *, name: str, input: dict[str, Any]) -> str:
        """Start one named execution; an already-running name is idempotent."""


@dataclass(frozen=True)
class IngestDependencies:
    devices: DeviceResolver
    shipments: ShipmentReader
    configs: ConfigReader
    debounce: DebounceStore
    incidents: IncidentStore
    dedupe: ReadingDedupe
    executions: ExecutionStarter
    state_machine_arn: str
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


class DynamoDeviceResolver:
    """Resolve a device from the trusted ``DEVICE#{id}`` routing row."""

    def __init__(self, client: Any, routing_table: str) -> None:
        self._client = client
        self._table = routing_table

    def resolve(self, device_id: str) -> tuple[str, str] | None:
        loaded = self._client.get_item(
            TableName=self._table,
            Key=key(f"DEVICE#{device_id}", "META"),
        )
        item = loaded.get("Item")
        if not item:
            return None
        tenant = text(item["tenant_id"])
        shipment = text(item["shipment_id"])
        return tenant, shipment


class DynamoShipmentReader:
    """Tenant-scoped shipment lookup in the tenant table."""

    def __init__(self, client: Any, tenant_table: str) -> None:
        self._client = client
        self._table = tenant_table

    def get(self, tenant_id: str, shipment_id: str) -> dict[str, Any] | None:
        loaded = self._client.get_item(
            TableName=self._table,
            Key=key(f"TENANT#{tenant_id}", f"SHIPMENT#{shipment_id}"),
        )
        item = loaded.get("Item")
        if not item:
            return None
        return {name: _attribute(value) for name, value in item.items() if name not in {"pk", "sk"}}


class DynamoReadingDedupe:
    """Conditional event dedupe rows on the platform table."""

    def __init__(self, client: Any, table_name: str) -> None:
        self._client = client
        self._table = table_name

    def register(self, event_id: str, *, now: datetime) -> bool:
        expires_at = int(now.timestamp()) + EVENT_RETENTION_SECONDS
        try:
            self._client.put_item(
                TableName=self._table,
                Item={
                    **key(f"INGEST#{event_id}", "META"),
                    "expires_at": n(expires_at),
                },
                ConditionExpression="attribute_not_exists(#pk) OR #expires_at <= :now",
                ExpressionAttributeNames={"#pk": "pk", "#expires_at": "expires_at"},
                ExpressionAttributeValues={":now": n(int(now.timestamp()))},
            )
        except Exception as exc:
            if is_conditional_failure(exc):
                return False
            raise
        return True

    def forget(self, event_id: str) -> None:
        self._client.delete_item(
            TableName=self._table,
            Key=key(f"INGEST#{event_id}", "META"),
        )


class StepFunctionsStarter:
    """Named Standard execution adapter; handles already-started retries."""

    def __init__(self, client: Any, state_machine_arn: str) -> None:
        self._client = client
        self._arn = state_machine_arn

    def start(self, *, name: str, input: dict[str, Any]) -> str:
        try:
            response = self._client.start_execution(
                stateMachineArn=self._arn,
                name=name,
                input=json.dumps(input, separators=(",", ":"), sort_keys=True),
            )
            return str(response["executionArn"])
        except Exception as exc:
            if not _execution_conflict(exc):
                raise
            return ""


def handler(
    event: dict[str, Any],
    context: Any,
    *,
    dependencies: IngestDependencies | None = None,
) -> dict[str, Any]:
    """Process one EventBridge reading; malformed/unknown input is safely ignored."""
    del context
    event_id = _event_identity(event)
    if event.get("source") != SOURCE or event.get("detail-type") != DETAIL_TYPE:
        return {"status": "ignored", "reason": "unsupported_event"}
    try:
        reading = Reading.model_validate(event.get("detail"))
    except (ValidationError, TypeError):
        log(
            logger,
            logging.INFO,
            f"Ignoring malformed temperature reading event_id={event_id}",
            outcome="ignored",
            count=1,
        )
        return {"status": "ignored", "reason": "invalid_reading"}

    ports = dependencies or build_dependencies()
    now = ports.clock()
    if not ports.dedupe.register(event_id, now=now):
        return {"status": "duplicate", "event_id": event_id}

    try:
        trusted = ports.devices.resolve(reading.device_id)
        if trusted is None:
            return {"status": "ignored", "reason": "unknown_device", "event_id": event_id}
        tenant_id, shipment_id = trusted
        if not tenant_id.strip() or not shipment_id.strip():
            return {"status": "ignored", "reason": "invalid_device_registry", "event_id": event_id}
        if reading.tenant_id is not None and reading.tenant_id != tenant_id:
            return {"status": "ignored", "reason": "tenant_mismatch", "event_id": event_id}
        if reading.shipment_id is not None and reading.shipment_id != shipment_id:
            return {"status": "ignored", "reason": "shipment_mismatch", "event_id": event_id}

        shipment = ports.shipments.get(tenant_id, shipment_id)
        if shipment is None:
            return {"status": "ignored", "reason": "unknown_shipment", "event_id": event_id}
        profile_id = str(
            shipment.get("product_profile") or shipment.get("product_profile_id") or ""
        )
        if not profile_id:
            return {"status": "ignored", "reason": "shipment_missing_profile", "event_id": event_id}

        config = ports.configs.get(tenant_id, DOMAIN)
        reading_event = TemperatureEvent(
            tenant_id=tenant_id,
            shipment_id=shipment_id,
            temperature_c=reading.temperature_c,
            timestamp=reading.ts.isoformat(),
        )
        reason = breach_reason(reading_event, config, profile_id=profile_id)
        streak = observe(
            ports.debounce,
            tenant_id=tenant_id,
            shipment_id=shipment_id,
            device_id=reading.device_id,
            in_breach=reason is not None,
        )
        decision = evaluate_trigger(
            reading_event,
            config,
            profile_id=profile_id,
            consecutive_breaches=max(1, streak),
        )
        if not decision.triggered:
            _record_reading(ports, tenant_id, shipment_id, reading, event_id)
            return {
                "status": "accepted",
                "reason": "debouncing" if streak else "within_range",
                "consecutive_breaches": streak,
                "event_id": event_id,
            }

        incident = ports.incidents.create(
            tenant_id=tenant_id,
            shipment_id=shipment_id,
            reason=decision.reason or "TEMPERATURE_EXCURSION",
            config_version=config.version,
            config_snapshot=config.model_dump(mode="json"),
        )
        _record_reading(ports, tenant_id, shipment_id, reading, event_id)
        execution_name = _execution_name(incident.incident_id)
        if ports.incidents.get(tenant_id, incident.incident_id).state != "OPEN":
            return {
                "status": "already_open",
                "event_id": event_id,
                "tenant_id": tenant_id,
                "shipment_id": shipment_id,
                "incident_id": incident.incident_id,
            }
        execution_arn = ports.executions.start(
            name=execution_name,
            input={
                "step": "OPEN",
                "tenant_id": tenant_id,
                "incident_id": incident.incident_id,
                "shipment_id": shipment_id,
                "context": {},
                "workflow": {
                    "domain": DOMAIN,
                    "trigger": {
                        "device_id": reading.device_id,
                        "timestamp": reading.ts.isoformat(),
                        "temperature_c": reading.temperature_c,
                        "latitude": reading.lat,
                        "longitude": reading.lon,
                        "sequence": reading.seq,
                        "reason": decision.reason,
                    },
                    "shipment": shipment,
                    "config_version": config.version,
                    "config_snapshot": config.model_dump(mode="json"),
                },
            },
        )
        log(
            logger,
            logging.INFO,
            (
                "Started cold-chain execution "
                f"tenant={tenant_id} shipment={shipment_id} incident={incident.incident_id}"
            ),
            tenant_id=tenant_id,
            incident_id=incident.incident_id,
            outcome="started",
        )
        return {
            "status": "triggered",
            "event_id": event_id,
            "tenant_id": tenant_id,
            "shipment_id": shipment_id,
            "incident_id": incident.incident_id,
            "execution_arn": execution_arn or None,
        }
    except Exception:
        ports.dedupe.forget(event_id)
        raise


def build_dependencies() -> IngestDependencies:
    """Build lazy AWS ports from the deployed ``oerf-ingest`` environment.

    The compute stack sets ``INCIDENT_TABLE_NAME``, ``TENANT_TABLE_NAME``,
    ``ROUTING_TABLE_NAME``, ``EVENT_BUS_NAME``, ``STATE_MACHINE_ARN`` and
    ``LOG_LEVEL``. Event identity, debounce streaks and the shipment lock all
    live on the incident table, which is the only table this role writes;
    ``EVENT_BUS_NAME`` is read by publishers, not by this handler.
    """
    incident_table = os.environ.get("INCIDENT_TABLE_NAME", "")
    tenant_table = os.environ.get("TENANT_TABLE_NAME", "")
    routing_table = os.environ.get("ROUTING_TABLE_NAME", "")
    state_machine_arn = os.environ.get("STATE_MACHINE_ARN", "")
    missing = [
        name
        for name, value in (
            ("INCIDENT_TABLE_NAME", incident_table),
            ("TENANT_TABLE_NAME", tenant_table),
            ("ROUTING_TABLE_NAME", routing_table),
            ("STATE_MACHINE_ARN", state_machine_arn),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"missing ingest environment variables: {', '.join(missing)}")
    dynamodb = boto3.client("dynamodb")
    return IngestDependencies(
        devices=DynamoDeviceResolver(dynamodb, routing_table),
        shipments=DynamoShipmentReader(dynamodb, tenant_table),
        configs=_DynamoConfigReader(DynamoConfigRepository(dynamodb, tenant_table)),
        debounce=_configured_debounce(dynamodb, incident_table),
        incidents=DynamoIncidentStore(dynamodb, incident_table),
        dedupe=DynamoReadingDedupe(dynamodb, incident_table),
        executions=StepFunctionsStarter(boto3.client("stepfunctions"), state_machine_arn),
        state_machine_arn=state_machine_arn,
    )


def _configured_debounce(client: Any, table_name: str) -> DebounceStore:
    from oerf.platform.incidents.debounce import DynamoDebounce

    return DynamoDebounce(client, table_name)


class _DynamoConfigReader:
    def __init__(self, repository: DynamoConfigRepository) -> None:
        self._repository = repository

    def get(self, tenant_id: str, domain: str) -> ResolvedConfig:
        return get_config(tenant_id, domain, repository=self._repository)


def _record_reading(
    ports: IngestDependencies,
    tenant_id: str,
    shipment_id: str,
    reading: Reading,
    event_id: str,
) -> None:
    """Store the reading on the open incident. ``record_reading`` is the only writer."""

    ports.incidents.record_reading(
        tenant_id,
        shipment_id,
        event_id=event_id,
        timestamp=iso_z(reading.ts),
        temperature_c=reading.temperature_c,
        sequence=reading.seq,
    )


def _event_identity(event: Mapping[str, Any]) -> str:
    provider_id = event.get("id")
    if isinstance(provider_id, str) and provider_id.strip():
        raw = provider_id.strip()
    else:
        detail = event.get("detail")
        detail = detail if isinstance(detail, Mapping) else {}
        raw = f"{detail.get('device_id', '')}#{detail.get('seq', '')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _execution_name(incident_id: str) -> str:
    cleaned = _EXECUTION_NAME.sub("-", incident_id)
    return cleaned[:70] or "cold-chain"


def _execution_conflict(error: BaseException) -> bool:
    response = getattr(error, "response", None)
    return (
        isinstance(response, dict)
        and response.get("Error", {}).get("Code") == "ExecutionAlreadyExists"
    )


def _attribute(value: dict[str, Any]) -> Any:
    if "S" in value:
        return value["S"]
    if "N" in value:
        raw = value["N"]
        return float(raw) if "." in raw else int(raw)
    if "BOOL" in value:
        return value["BOOL"]
    if "M" in value:
        return {name: _attribute(item) for name, item in value["M"].items()}
    if "L" in value:
        return [_attribute(item) for item in value["L"]]
    return None
