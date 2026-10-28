"""One-way WhatsApp send. The channel router is the only caller.

No task token is stored, and a successful send does not write ``OUTMSG#`` or
``ADDR#``. The dedupe row is written only after ``sent`` or ``simulated``, so a
failure can be retried. Provider and address errors return ``failed``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from oerf.platform.channels.address import AddressError, resolve_party_address
from oerf.platform.channels.router import OutboundRequest, route_outbound
from oerf.platform.observability.logging import get_logger, log
from oerf.platform.storage.dynamo import epoch_seconds, is_conditional_failure, key, n, s, text

logger = get_logger(__name__)

RETENTION_SECONDS = 7 * 24 * 60 * 60
SESSION_MESSAGES = frozenset({"driver_ack"})
CONTINUE_STATUS = "continue to your destination"
CONTINUE_NEXT = "we're monitoring the temperature"


class Outbox(Protocol):
    def seen(self, tenant_id: str, dedupe_key: str) -> dict[str, Any] | None:
        """The unexpired row, or None when this key can be sent."""

    def remember(
        self,
        tenant_id: str,
        dedupe_key: str,
        *,
        status: str,
        provider_msg_id: str | None,
    ) -> bool:
        """Store a sent or simulated result. False when an unexpired row exists."""


class FacilityReader(Protocol):
    def get(self, tenant_id: str, facility_id: str) -> dict[str, Any] | None:
        """One facility row for this tenant, or None."""


class MemoryOutbox:
    """In-memory dedupe for tests. Expiry is compared with the injected clock."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    def seen(self, tenant_id: str, dedupe_key: str) -> dict[str, Any] | None:
        row = self.rows.get((tenant_id, dedupe_key))
        if row is None or int(row["expires_at"]) <= epoch_seconds(self._clock()):
            return None
        return row

    def remember(
        self,
        tenant_id: str,
        dedupe_key: str,
        *,
        status: str,
        provider_msg_id: str | None,
    ) -> bool:
        if self.seen(tenant_id, dedupe_key) is not None:
            return False
        self.rows[(tenant_id, dedupe_key)] = {
            "tenant_id": tenant_id,
            "dedupe_key": dedupe_key,
            "status": status,
            "provider_msg_id": provider_msg_id,
            "expires_at": epoch_seconds(self._clock()) + RETENTION_SECONDS,
        }
        return True


class DynamoOutbox:
    """Conditional put on ``TENANT#{tid}#OUTBOX#{dedupe_key}`` / ``META``."""

    def __init__(
        self,
        client: Any,
        table_name: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._table = table_name
        self._clock = clock or (lambda: datetime.now(UTC))

    def seen(self, tenant_id: str, dedupe_key: str) -> dict[str, Any] | None:
        loaded = self._client.get_item(
            TableName=self._table,
            Key=key(_outbox_pk(tenant_id, dedupe_key), "META"),
        )
        item = loaded.get("Item")
        if not item:
            return None
        expires = _attr_int(item, "expires_at")
        if expires is None or expires <= epoch_seconds(self._clock()):
            return None
        message_id = item.get("provider_msg_id")
        return {
            "tenant_id": text(item["tenant_id"]) if item.get("tenant_id") else tenant_id,
            "dedupe_key": dedupe_key,
            "status": text(item["status"]) if item.get("status") else "",
            "provider_msg_id": text(message_id) if message_id else None,
            "expires_at": expires,
        }

    def remember(
        self,
        tenant_id: str,
        dedupe_key: str,
        *,
        status: str,
        provider_msg_id: str | None,
    ) -> bool:
        now = epoch_seconds(self._clock())
        item: dict[str, Any] = {
            **key(_outbox_pk(tenant_id, dedupe_key), "META"),
            "tenant_id": s(tenant_id),
            "dedupe_key": s(dedupe_key),
            "status": s(status),
            "expires_at": n(now + RETENTION_SECONDS),
        }
        if provider_msg_id:
            item["provider_msg_id"] = s(provider_msg_id)
        try:
            self._client.put_item(
                TableName=self._table,
                Item=item,
                ConditionExpression="attribute_not_exists(#pk) OR #expires_at <= :now",
                ExpressionAttributeNames={"#pk": "pk", "#expires_at": "expires_at"},
                ExpressionAttributeValues={":now": n(now)},
            )
        except Exception as exc:
            if is_conditional_failure(exc):
                return False
            raise
        return True


class DynamoFacilityReader:
    """``GetItem`` on ``TENANT#{tid}`` / ``FACILITY#{id}``. The router may already do this."""

    def __init__(self, client: Any, table_name: str) -> None:
        self._client = client
        self._table = table_name

    def get(self, tenant_id: str, facility_id: str) -> dict[str, Any] | None:
        loaded = self._client.get_item(
            TableName=self._table,
            Key=key(f"TENANT#{tenant_id}", f"FACILITY#{facility_id}"),
        )
        item = loaded.get("Item")
        if not item:
            return None
        row: dict[str, Any] = {"facility_id": facility_id}
        name = _attr_text(item, "name")
        if name:
            row["name"] = name
        for field_name in ("lat", "lon", "drive_min"):
            number = _attr_number(item, field_name)
            if number is not None:
                row[field_name] = number
        return row


def send_one_way(event: dict[str, Any], *, ports: Any | None = None) -> dict[str, Any]:
    """Send one message, or return failed. This function does not raise for a send error."""

    built = ports if ports is not None else _ports()
    try:
        return _send(event, built)
    except Exception as exc:
        _log_failure(event, exc)
        return _outcome("failed", None)


def _send(event: dict[str, Any], ports: Any) -> dict[str, Any]:
    tenant_id = _text(event, "tenant_id")
    incident_id = _text(event, "incident_id")
    role = _text(event, "role")
    message = _text(event, "message")
    dedupe_key = _text(event, "dedupe_key")
    if not all((tenant_id, incident_id, role, message, dedupe_key)):
        return _outcome("failed", None)
    channel = _text(event, "channel") or "whatsapp"
    if channel == "voice":
        log(
            logger,
            logging.INFO,
            "one-way voice skipped",
            tenant_id=tenant_id,
            incident_id=incident_id,
            channel="voice",
            outcome="skipped",
        )
        return _outcome("skipped", None)
    facts = event.get("facts") if isinstance(event.get("facts"), dict) else {}
    existing = ports.outbox.seen(tenant_id, dedupe_key)
    if existing is not None:
        stored = existing.get("provider_msg_id")
        return _outcome("skipped", stored if isinstance(stored, str) else None)
    contact_id = _contact_id(facts, role)
    if contact_id is None:
        return _outcome("failed", None)
    try:
        recipient = resolve_party_address(
            tenant_id,
            role,
            contact_id,
            registry=ports.registry,
            parameters=ports.parameters,
            live=not ports.mock_whatsapp,
        )
    except AddressError as exc:
        _log_failure(event, exc)
        return _outcome("failed", None)
    template_facts = _template_facts(message, facts, tenant_id, ports)
    if template_facts is None:
        return _outcome("failed", None)
    result = route_outbound(
        OutboundRequest(
            tenant_id=tenant_id,
            incident_id=incident_id,
            engagement_id=dedupe_key,
            role=role,
            template_id=message,
            facts=template_facts,
            contact_id=contact_id,
            recipient=recipient,
            channels=(channel,),
            session_text=message in SESSION_MESSAGES,
        ),
        registry=ports.registry,
        engagements=getattr(ports, "engagements", None),
        providers=ports.providers,
        messages=ports.messages,
        correlate=False,
    )
    if result.simulated:
        status = "simulated"
    elif result.delivered:
        status = "sent"
    else:
        return _outcome("failed", None)
    try:
        ports.outbox.remember(
            tenant_id,
            dedupe_key,
            status=status,
            provider_msg_id=result.provider_msg_id,
        )
    except Exception as exc:
        _log_failure(event, exc)
    return _outcome(status, result.provider_msg_id)


def _template_facts(
    message: str,
    facts: dict[str, Any],
    tenant_id: str,
    ports: Any,
) -> dict[str, Any] | None:
    if message == "driver_ack":
        answer = facts.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            return None
        return {"answer": answer.strip()}
    shipment_id = _shipment_id(facts)
    if message == "status_update":
        if shipment_id is None:
            return None
        return {
            "shipment_id": shipment_id,
            "status": CONTINUE_STATUS,
            "next_step": CONTINUE_NEXT,
        }
    if message == "driver_reroute":
        return _reroute_facts(facts, tenant_id, ports, shipment_id)
    rendered = dict(facts)
    shipment = facts.get("shipment") if isinstance(facts.get("shipment"), dict) else {}
    trigger = facts.get("trigger") if isinstance(facts.get("trigger"), dict) else {}
    if "shipment_id" in shipment:
        rendered["shipment_id"] = shipment["shipment_id"]
    if "temperature_c" in trigger:
        rendered["temperature_c"] = trigger["temperature_c"]
    return rendered


def _reroute_facts(
    facts: dict[str, Any],
    tenant_id: str,
    ports: Any,
    shipment_id: str | None,
) -> dict[str, Any] | None:
    if shipment_id is None:
        return None
    row = _facility_row(facts, tenant_id, ports)
    name = row.get("name")
    if not isinstance(name, str) or not name.strip():
        fallback = row.get("facility_id")
        name = fallback.strip() if isinstance(fallback, str) and fallback.strip() else None
    if name is None:
        return None
    minutes = _minutes(row.get("drive_min"))
    action = facts.get("action") if isinstance(facts.get("action"), dict) else {}
    if minutes is None:
        minutes = _minutes(action.get("drive_min"))
    if minutes is None:
        minutes = _minutes(facts.get("drive_min"))
    if minutes is None:
        return None
    return {
        "shipment_id": shipment_id,
        "facility_name": name.strip() if isinstance(name, str) else name,
        "minutes": minutes,
        "directions": _directions(row.get("lat"), row.get("lon")),
    }


def _facility_row(facts: dict[str, Any], tenant_id: str, ports: Any) -> dict[str, Any]:
    action = facts.get("action") if isinstance(facts.get("action"), dict) else {}
    facility_id = _plain(action.get("facility_id")) or _plain(facts.get("facility_id"))
    row: dict[str, Any] = {}
    facilities = facts.get("facilities")
    if isinstance(facilities, list) and facility_id:
        for item in facilities:
            if not isinstance(item, dict):
                continue
            named = _plain(item.get("facility_id")) or _plain(item.get("id"))
            if named == facility_id:
                row = dict(item)
                break
    reader = getattr(ports, "facilities", None)
    if reader is not None and facility_id:
        loaded = reader.get(tenant_id, facility_id)
        if isinstance(loaded, dict):
            merged = dict(loaded)
            merged.update({name: value for name, value in row.items() if value is not None})
            row = merged
    if facility_id:
        row.setdefault("facility_id", facility_id)
    if "drive_min" not in row and action.get("drive_min") is not None:
        row["drive_min"] = action.get("drive_min")
    return row


def _directions(lat: object, lon: object) -> str:
    latitude = _coord(lat)
    longitude = _coord(lon)
    if latitude is None or longitude is None:
        return "n/a"
    return f"https://maps.google.com/?q={latitude},{longitude}"


def _contact_id(facts: dict[str, Any], role: str) -> str | None:
    shipment = facts.get("shipment") if isinstance(facts.get("shipment"), dict) else None
    if shipment is None:
        context = facts.get("context") if isinstance(facts.get("context"), dict) else {}
        nested = context.get("shipment") if isinstance(context.get("shipment"), dict) else {}
        shipment = nested
    value = shipment.get(role)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _shipment_id(facts: dict[str, Any]) -> str | None:
    direct = facts.get("shipment_id")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    shipment = facts.get("shipment") if isinstance(facts.get("shipment"), dict) else {}
    value = shipment.get("shipment_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _coord(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    rendered = format(float(value), "f").rstrip("0").rstrip(".")
    return rendered or "0"


def _minutes(value: object) -> str | None:
    return _coord(value)


def _plain(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _text(event: dict[str, Any], name: str) -> str | None:
    value = event.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _outcome(status: str, provider_msg_id: str | None) -> dict[str, Any]:
    return {"status": status, "provider_msg_id": provider_msg_id}


def _log_failure(event: dict[str, Any], exc: BaseException) -> None:
    log(
        logger,
        logging.ERROR,
        f"one-way send failed error={type(exc).__name__}",
        tenant_id=_text(event, "tenant_id"),
        incident_id=_text(event, "incident_id"),
        outcome="failed",
    )


def _outbox_pk(tenant_id: str, dedupe_key: str) -> str:
    return f"TENANT#{tenant_id}#OUTBOX#{dedupe_key}"


def _attr_text(item: dict[str, Any], name: str) -> str:
    attribute = item.get(name)
    if not isinstance(attribute, dict):
        return ""
    value = attribute.get("S")
    return value.strip() if isinstance(value, str) else ""


def _attr_int(item: dict[str, Any], name: str) -> int | None:
    number = _attr_number(item, name)
    if number is None:
        return None
    return int(number)


def _attr_number(item: dict[str, Any], name: str) -> float | None:
    attribute = item.get(name)
    if not isinstance(attribute, dict) or "N" not in attribute:
        return None
    return float(attribute["N"])


def _ports() -> Any:
    import boto3

    from oerf.platform.channels.engagement_send import _ports as engagement_ports

    built = engagement_ports()
    client = boto3.client("dynamodb")
    routing = os.environ["ROUTING_TABLE_NAME"]
    tenant = os.environ.get("TENANT_TABLE_NAME", "").strip()
    built.outbox = DynamoOutbox(client, routing)
    built.facilities = DynamoFacilityReader(client, tenant) if tenant else None
    return built
