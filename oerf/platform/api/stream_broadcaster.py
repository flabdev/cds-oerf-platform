"""DynamoDB stream broadcaster for live dashboard updates (ADR-010).

Incident META changes go to WebSocket subscriptions for that tenant.
Incident scope receives the sanitized incident event. Tenant-list scope
receives the summary. A row with no scope receives nothing.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import BotoCoreError, ClientError

from oerf.platform.observability.logging import get_logger, log

logger = get_logger(__name__)
logger.setLevel(logging.INFO)

ROUTING_TABLE_NAME = os.environ.get("ROUTING_TABLE_NAME", "")
WEBSOCKET_ENDPOINT = os.environ.get("WEBSOCKET_ENDPOINT", "")

_dynamodb = None
_apigw_client = None
_deserializer = TypeDeserializer()
_INCIDENT_PK = re.compile(r"^TENANT#([^#]+)#INC#(.+)$")
_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
# Incident ``data`` keys the desk accepts. Anything else on the image stays off the socket.
_INCIDENT_DATA = frozenset(
    {
        "incident_id",
        "state",
        "stage",
        "updated_at",
        "version",
    }
)
_SUMMARY_DATA = frozenset({"incident_id", "state", "stage", "version"})
_ENVELOPE = frozenset(
    {"event_type", "tenant_id", "incident_id", "version", "emitted_at", "data"}
)


def _now() -> datetime:
    return datetime.now(UTC)


def get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def get_apigw():
    global _apigw_client
    if _apigw_client is None and WEBSOCKET_ENDPOINT:
        _apigw_client = boto3.client(
            "apigatewaymanagementapi",
            endpoint_url=WEBSOCKET_ENDPOINT,
        )
    return _apigw_client


def deserialize_dynamodb_image(image: dict[str, Any]) -> dict[str, Any]:
    """Convert a DynamoDB Stream image to a plain dict."""
    return {key: _deserializer.deserialize(value) for key, value in image.items()}


def subscription_targets(items: list[dict[str, Any]], incident_id: str) -> list[tuple[str, str]]:
    """Return ``(connection_id, scope)`` for this incident.

    Tenant scope receives the summary. Incident scope receives the update only
    when the stored incident id matches. ``ALL`` and ``*`` receive nothing.
    """

    targets: list[tuple[str, str]] = []
    for item in items:
        connection_id = item.get("connection_id")
        scope = item.get("scope")
        if not isinstance(connection_id, str) or not connection_id:
            continue
        if scope == "tenant" and "incident_id" not in item:
            targets.append((connection_id, "tenant"))
            continue
        subscribed = item.get("incident_id")
        if (
            scope == "incident"
            and isinstance(subscribed, str)
            and subscribed not in {"", "ALL", "*"}
            and subscribed == incident_id
        ):
            targets.append((connection_id, "incident"))
    return targets


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Broadcast one batch. Retryable delivery errors are partial failures."""

    records = event.get("Records")
    if not isinstance(records, list):
        records = []
    log(logger, logging.INFO, "stream_batch", count=len(records), outcome="received")
    failures: list[dict[str, str]] = []
    apigw = get_apigw()
    table = _routing_table()
    if apigw is None or table is None:
        log(logger, logging.WARNING, "stream_batch", outcome="not_configured", count=0)
        return {"batchItemFailures": []}

    for record in records:
        if not isinstance(record, dict):
            log(logger, logging.WARNING, "stream_record", outcome="malformed", count=1)
            continue
        sequence = _sequence(record)
        try:
            if _deliver(record, table, apigw):
                continue
        except (ClientError, BotoCoreError):
            log(logger, logging.ERROR, "stream_record", outcome="query_failed", count=1)
        if not sequence:
            log(logger, logging.ERROR, "stream_record", outcome="malformed", count=1)
            continue
        failures.append({"itemIdentifier": sequence})
    return {"batchItemFailures": failures}


def _routing_table():
    if not ROUTING_TABLE_NAME:
        return None
    return get_dynamodb().Table(ROUTING_TABLE_NAME)


def _deliver(record: dict[str, Any], table, apigw) -> bool:
    """Return True when the record must not be retried."""

    event_name = record.get("eventName")
    if event_name == "REMOVE":
        identity = _identity(_image(record, "OldImage"))
        if identity is None:
            log(logger, logging.WARNING, "stream_record", outcome="malformed", count=1)
        else:
            tenant_id, incident_id = identity
            log(
                logger,
                logging.INFO,
                "stream_remove_skipped",
                tenant_id=tenant_id,
                incident_id=incident_id,
                outcome="skipped",
                count=1,
            )
        return True

    item = _image(record, "NewImage")
    identity = _identity(item)
    if item is None or identity is None:
        log(logger, logging.WARNING, "stream_record", outcome="malformed", count=1)
        return True
    tenant_id, incident_id = identity
    sk = str(item.get("sk") or "")
    if sk.startswith("EVT#") or sk.startswith("READ#"):
        return True
    state = _text(item.get("state"))
    if state is None:
        log(
            logger,
            logging.WARNING,
            "stream_record",
            tenant_id=tenant_id,
            incident_id=incident_id,
            outcome="malformed",
            count=1,
        )
        return True

    version = _version(item)
    stage = _stage(item, state)
    emitted_at = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
    payloads = {
        "incident": _envelope(
            "INCIDENT_UPDATE",
            tenant_id,
            incident_id,
            version,
            emitted_at,
            _incident_data(item, incident_id, state, stage, version),
        ),
        "tenant": _envelope(
            "INCIDENT_SUMMARY",
            tenant_id,
            incident_id,
            version,
            emitted_at,
            _summary_data(incident_id, state, stage, version),
        ),
    }
    retry = False
    delivered = 0
    for subscription in _subscriptions(table, tenant_id):
        audience = _audience(subscription, tenant_id, incident_id)
        if audience is None:
            continue
        connection_id = _text(subscription.get("connection_id"))
        if connection_id is None:
            continue
        posted = _post(
            apigw,
            table,
            tenant_id,
            incident_id,
            connection_id,
            subscription,
            payloads[audience],
        )
        if posted:
            delivered += 1
        else:
            retry = True
    log(
        logger,
        logging.INFO,
        "stream_broadcast",
        tenant_id=tenant_id,
        incident_id=incident_id,
        outcome="sent",
        count=delivered,
    )
    return not retry


def _post(
    apigw,
    table,
    tenant_id: str,
    incident_id: str,
    connection_id: str,
    subscription: dict,
    payload: dict,
) -> bool:
    """Return False when the post should be retried. Gone is not a failure."""

    try:
        apigw.post_to_connection(ConnectionId=connection_id, Data=_dumps(payload).encode("utf-8"))
    except ClientError as exc:
        if _gone(exc):
            _forget(table, tenant_id, connection_id, subscription)
            log(
                logger,
                logging.INFO,
                "connection_gone",
                tenant_id=tenant_id,
                incident_id=incident_id,
                outcome="gone",
                count=1,
            )
            return True
        log(
            logger,
            logging.ERROR,
            "stream_delivery",
            tenant_id=tenant_id,
            incident_id=incident_id,
            outcome="retry",
            count=1,
        )
        return False
    except BotoCoreError:
        log(
            logger,
            logging.ERROR,
            "stream_delivery",
            tenant_id=tenant_id,
            incident_id=incident_id,
            outcome="retry",
            count=1,
        )
        return False
    return True


def _forget(table, tenant_id: str, connection_id: str, subscription: dict) -> None:
    """Drop the subscription row and the connection binding."""

    sk = _text(subscription.get("sk")) or f"CONN#{connection_id}"
    keys = (
        {"pk": f"WS_SUB#{tenant_id}", "sk": sk},
        {"pk": f"WS#{connection_id}", "sk": "METADATA"},
    )
    for key in keys:
        try:
            table.delete_item(Key=key)
        except (ClientError, BotoCoreError):
            log(
                logger,
                logging.ERROR,
                "connection_gone",
                tenant_id=tenant_id,
                outcome="gone",
                count=1,
            )


def _subscriptions(table, tenant_id: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": "pk = :pk",
        "ExpressionAttributeValues": {":pk": f"WS_SUB#{tenant_id}"},
    }
    while True:
        response = table.query(**kwargs)
        for item in response.get("Items", []):
            if isinstance(item, dict):
                found.append(item)
        token = response.get("LastEvaluatedKey")
        if not token:
            return found
        kwargs["ExclusiveStartKey"] = token


def _audience(subscription: dict[str, Any], tenant_id: str, incident_id: str) -> str | None:
    """Incident, tenant list, or nothing. Missing scope and ALL receive nothing."""

    row_tenant = _text(subscription.get("tenant_id"))
    if row_tenant is not None and row_tenant != tenant_id:
        return None
    scope = subscription.get("scope")
    if scope == "incident" and _text(subscription.get("incident_id")) == incident_id:
        return "incident"
    if scope == "tenant":
        return "tenant"
    return None


def _identity(item: dict[str, Any] | None) -> tuple[str, str] | None:
    if not isinstance(item, dict):
        return None
    pk = _text(item.get("pk"))
    if pk is None:
        return None
    match = _INCIDENT_PK.fullmatch(pk)
    if match is None:
        return None
    tenant_id, incident_id = match.group(1), match.group(2)
    if not tenant_id or not incident_id:
        return None
    declared_tenant = _text(item.get("tenant_id"))
    declared_incident = _text(item.get("incident_id"))
    if declared_tenant is not None and declared_tenant != tenant_id:
        return None
    if declared_incident is not None and declared_incident != incident_id:
        return None
    return tenant_id, incident_id


def _image(record: dict[str, Any], name: str) -> dict[str, Any] | None:
    dynamodb = record.get("dynamodb")
    if not isinstance(dynamodb, dict):
        return None
    raw = dynamodb.get(name)
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        item = deserialize_dynamodb_image(raw)
    except (TypeError, ValueError, AttributeError):
        return None
    keys = dynamodb.get("Keys")
    if isinstance(keys, dict):
        try:
            parsed = deserialize_dynamodb_image(keys)
        except (TypeError, ValueError, AttributeError):
            parsed = {}
        for field in ("pk", "sk"):
            if field not in item and field in parsed:
                item[field] = parsed[field]
    return item


def _incident_data(
    item: dict[str, Any],
    incident_id: str,
    state: str,
    stage: str,
    version: int,
) -> dict[str, Any]:
    """Incident-scope ``data``. Keys match the desk allowlist in ``deskEvents.js``."""

    data: dict[str, Any] = {
        "incident_id": incident_id,
        "state": state,
        "stage": stage,
        "version": version,
    }
    stamp = _timestamp(item.get("updated_at"))
    if stamp is not None:
        data["updated_at"] = stamp
    return {key: value for key, value in data.items() if key in _INCIDENT_DATA}


def _summary_data(incident_id: str, state: str, stage: str, version: int) -> dict[str, Any]:
    data = {
        "incident_id": incident_id,
        "state": state,
        "stage": stage,
        "version": version,
    }
    return {key: value for key, value in data.items() if key in _SUMMARY_DATA}


def _envelope(
    event_type: str,
    tenant_id: str,
    incident_id: str,
    version: int,
    emitted_at: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "event_type": event_type,
        "tenant_id": tenant_id,
        "incident_id": incident_id,
        "version": version,
        "emitted_at": emitted_at,
        "data": data,
    }
    return {key: value for key, value in payload.items() if key in _ENVELOPE}


def _stage(item: dict[str, Any], state: str) -> str:
    for name in ("stage", "current_stage"):
        value = _text(item.get(name))
        if value is not None:
            return value
    return state


def _version(item: dict[str, Any]) -> int:
    for name in ("version", "event_seq"):
        number = _whole(item.get(name))
        if number is not None and number >= 0:
            return number
    return 1


def _timestamp(value: Any) -> str | int | None:
    if isinstance(value, str) and _ISO_Z.fullmatch(value):
        return value
    number = _whole(value)
    if number is not None and number >= 0:
        return number
    return None


def _whole(value: Any) -> int | None:
    if isinstance(value, bool) or isinstance(value, float):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    return None


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _sequence(record: dict[str, Any]) -> str | None:
    dynamodb = record.get("dynamodb")
    if not isinstance(dynamodb, dict):
        return None
    return _text(dynamodb.get("SequenceNumber"))


def _gone(exc: ClientError) -> bool:
    error = exc.response.get("Error", {})
    if error.get("Code") == "GoneException":
        return True
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status == 410


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"))
