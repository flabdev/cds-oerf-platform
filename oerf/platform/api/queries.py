"""Tenant-scoped incident reads and the dashboard action queue.

Reads never return a task token. The queue send is the only write, and it
goes to SQS. Incident state is not changed here.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from boto3.dynamodb.types import TypeDeserializer

from oerf.platform.incidents.repository import TRANSITIONS
from oerf.platform.rules.allowance import calculate_allowance
from oerf.platform.storage.dynamo import incident_pk, key, s, text
from oerf.platform.tenancy.context import TenantContext

_DESERIALIZER = TypeDeserializer()

_PRIVATE = frozenset({"task_token", "phone", "e164", "email", "recipient"})
_PAGE = 50


class DynamoIncidentQuery:
    """List and detail from the incident table. The client is injected."""

    def __init__(self, client: Any, table_name: str, tenant_table: str = "") -> None:
        self._client = client
        self._table = table_name
        self._tenant_table = tenant_table.strip()

    def list_incidents(
        self,
        tenant_id: str,
        *,
        limit: int = _PAGE,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        context = TenantContext(tenant_id=tenant_id)
        rows = [
            _summary(item)
            for item in self._metas(context.tenant_id)
        ]
        rows.sort(key=lambda item: item["incident_id"])
        start = 0
        if cursor:
            start = next(
                (index + 1 for index, item in enumerate(rows) if item["incident_id"] == cursor),
                len(rows),
            )
        size = min(max(limit, 1), _PAGE)
        page = rows[start : start + size]
        next_cursor = None
        if start + size < len(rows) and page:
            next_cursor = page[-1]["incident_id"]
        return {"incidents": page, "next_cursor": next_cursor}

    def get_incident(self, tenant_id: str, incident_id: str) -> dict[str, Any] | None:
        context = TenantContext(tenant_id=tenant_id)
        incident = incident_id.strip()
        loaded = self._client.get_item(
            TableName=self._table,
            Key=key(incident_pk(context.tenant_id, incident), "META"),
        )
        item = loaded.get("Item")
        if not item or text(item.get("tenant_id", {"S": ""})) != context.tenant_id:
            return None
        pk = incident_pk(context.tenant_id, incident)
        timeline = [_transition(row) for row in self._partition(pk, "EVT#")]
        readings = [_reading(row) for row in self._partition(pk, "READ#")]
        readings = [row for row in readings if row]
        snapshot = _json_field(item, "config_snapshot")
        shipment_id = text(item["shipment_id"])
        profile_id = self._profile_id(item, context.tenant_id, shipment_id)
        profile = _profile(snapshot, profile_id)
        recommendation = self._recommendation(pk)
        engagement_id, engagement_status = self._engagement(pk)
        state = text(item["state"])
        return {
            "incident_id": text(item["incident_id"]),
            "tenant_id": text(item["tenant_id"]),
            "shipment_id": shipment_id,
            "state": state,
            "stage": _optional_text(item, "stage") or state,
            "version": _version(item),
            "reason": _optional_text(item, "reason") or "",
            "opened_at": _optional_text(item, "opened_at") or "",
            "config_version": _optional_number(item, "config_version"),
            "config_snapshot": snapshot,
            "product_profile_id": profile_id,
            "allowance_remaining": _allowance_remaining(profile, readings),
            "range_c": _range_c(profile),
            "evidence": _evidence(item.get("evidence_analysis")),
            "recommendation": recommendation,
            "options": recommendation["options"] if recommendation else [],
            "engagement_id": engagement_id,
            "engagement_status": engagement_status,
            "timeline": timeline,
            "readings": readings,
        }

    def _partition(self, pk: str, prefix: str) -> list[dict[str, Any]]:
        """Every row under one incident, oldest first. Pages until the partition ends."""

        found: list[dict[str, Any]] = []
        start: dict[str, Any] | None = None
        while True:
            args: dict[str, Any] = {
                "TableName": self._table,
                "KeyConditionExpression": "#pk = :pk AND begins_with(#sk, :prefix)",
                "ExpressionAttributeNames": {"#pk": "pk", "#sk": "sk"},
                "ExpressionAttributeValues": {":pk": s(pk), ":prefix": s(prefix)},
            }
            if start is not None:
                args["ExclusiveStartKey"] = start
            response = self._client.query(**args)
            found.extend(response.get("Items", []))
            start = response.get("LastEvaluatedKey")
            if not start:
                found.sort(key=lambda row: text(row.get("sk", {"S": ""})))
                return found

    def _recommendation(self, pk: str) -> dict[str, Any] | None:
        loaded = self._client.get_item(TableName=self._table, Key=key(pk, "STEP#Assess"))
        item = loaded.get("Item")
        if not item:
            return None
        decision = _json_attr(item.get("decision")) or {}
        options = decision.get("ranked_options")
        recommended = decision.get("recommended_option")
        if not isinstance(options, list):
            output = _json_attr(item.get("step_output")) or {}
            workflow = output.get("workflow") if isinstance(output, dict) else {}
            assessment = workflow.get("assessment") if isinstance(workflow, dict) else {}
            options = assessment.get("options") if isinstance(assessment, dict) else []
            recommended = (
                assessment.get("recommended_option") if isinstance(assessment, dict) else None
            )
        public = [_option(option) for option in options if isinstance(option, dict)]
        public = [option for option in public if option]
        if not isinstance(recommended, str) or not recommended.strip():
            recommended = None
        if recommended is None and not public:
            return None
        return {"recommended_option": recommended, "options": public}

    def _engagement(self, pk: str) -> tuple[str | None, str | None]:
        waiting: tuple[str | None, str | None] | None = None
        latest: tuple[str | None, str | None] | None = None
        for row in self._partition(pk, "ENG#"):
            status = _optional_text(row, "status")
            sk = text(row.get("sk", {"S": ""}))
            engagement_id = sk.removeprefix("ENG#").strip()
            if not engagement_id or not status:
                continue
            latest = (engagement_id, status)
            if status == "WAITING":
                waiting = latest
        return waiting or latest or (None, None)

    def _profile_id(self, item: dict[str, Any], tenant_id: str, shipment_id: str) -> str | None:
        """META first. A legacy row uses the shipment's profile. Never the first snapshot entry."""

        stored = _optional_text(item, "product_profile_id")
        if stored and stored.strip():
            return stored.strip()
        return self._shipment_profile(tenant_id, shipment_id)

    def _shipment_profile(self, tenant_id: str, shipment_id: str) -> str | None:
        if not self._tenant_table or not shipment_id:
            return None
        loaded = self._client.get_item(
            TableName=self._tenant_table,
            Key=key(f"TENANT#{tenant_id}", f"SHIPMENT#{shipment_id}"),
        )
        row = loaded.get("Item")
        if not row:
            return None
        return _optional_text(row, "product_profile") or _optional_text(row, "product_profile_id")

    def _metas(self, tenant_id: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for state in sorted(TRANSITIONS):
            response = self._client.query(
                TableName=self._table,
                IndexName="GSI1",
                KeyConditionExpression="#pk = :pk",
                ExpressionAttributeNames={"#pk": "GSI1PK"},
                ExpressionAttributeValues={":pk": s(f"TENANT#{tenant_id}#STATUS#{state}")},
            )
            found.extend(response.get("Items", []))
        return found


class SqsActionQueue:
    """Send one message. A failure raises and the caller does not report success.

    FIFO ids are set only when the queue URL ends in ``.fifo``. A standard
    queue leaves duplicates to the inbound handler's ``INMSG#`` dedupe.
    """

    def __init__(self, client: Any, queue_url: str) -> None:
        self._client = client
        self._url = queue_url

    def send(self, *, body: dict[str, Any], dedup_id: str) -> bool:
        params: dict[str, Any] = {
            "QueueUrl": self._url,
            "MessageBody": json.dumps(body),
        }
        if self._url.endswith(".fifo"):
            params["MessageDeduplicationId"] = dedup_id
            params["MessageGroupId"] = str(body.get("tenant_id") or "dashboard")
        self._client.send_message(**params)
        return True


def _summary(item: dict[str, Any]) -> dict[str, Any]:
    state = text(item["state"])
    return {
        "incident_id": text(item["incident_id"]),
        "state": state,
        "stage": _optional_text(item, "stage") or state,
        "version": _version(item),
    }


def _optional_text(item: dict[str, Any], name: str) -> str | None:
    if name not in item:
        return None
    return text(item[name])


def _optional_number(item: dict[str, Any], name: str) -> int | float | None:
    if name not in item or "N" not in item[name]:
        return None
    raw = item[name]["N"]
    if "." in raw:
        return float(raw)
    return int(raw)


def _json_field(item: dict[str, Any], name: str) -> Any:
    if name not in item:
        return None
    parsed = json.loads(text(item[name]))
    return _public(parsed)


def _version(item: dict[str, Any]) -> int:
    """The same integer the broadcaster sends: ``version``, else ``event_seq``."""

    for name in ("version", "event_seq"):
        number = _optional_number(item, name)
        if isinstance(number, int) and number >= 0:
            return number
        if isinstance(number, float) and number.is_integer() and number >= 0:
            return int(number)
    return 1


def _transition(item: dict[str, Any]) -> dict[str, str]:
    sk = text(item.get("sk", {"S": ""}))
    stamp = sk.removeprefix("EVT#").rsplit("#", 1)[0]
    return {
        "at": stamp,
        "from_state": _optional_text(item, "previous_state") or "",
        "to_state": _optional_text(item, "state") or "",
    }


def _reading(item: dict[str, Any]) -> dict[str, Any] | None:
    ts = _optional_text(item, "ts")
    temperature = _optional_number(item, "temperature_c")
    if not ts or isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        return None
    return {"ts": ts, "temperature_c": temperature}


def _allowance_remaining(
    profile: dict[str, Any] | None,
    readings: list[dict[str, Any]],
) -> float | None:
    """Minutes left from the selected profile and the stored readings.

    No profile, no band, or no reading means the desk has nothing to show.
    """

    if profile is None or not readings:
        return None
    allowance = profile.get("excursion_allowance_min")
    band = profile.get("range_c")
    if isinstance(allowance, bool) or not isinstance(allowance, (int, float)):
        return None
    if not isinstance(band, list) or len(band) != 2:
        return None
    low, high = band
    if isinstance(low, bool) or isinstance(high, bool):
        return None
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return None
    elapsed = _excursion_minutes(readings, float(low), float(high))
    if elapsed is None:
        return None
    remaining = calculate_allowance(float(allowance), elapsed).remaining_minutes
    return round(remaining, 1)


def _profile(snapshot: Any, profile_id: str | None) -> dict[str, Any] | None:
    if not profile_id or not isinstance(snapshot, dict):
        return None
    profiles = snapshot.get("product_profiles")
    if not isinstance(profiles, list):
        return None
    for profile in profiles:
        if isinstance(profile, dict) and str(profile.get("id", "")).strip() == profile_id:
            return profile
    return None


def _range_c(profile: dict[str, Any] | None) -> list[float] | None:
    if profile is None:
        return None
    band = profile.get("range_c")
    if not isinstance(band, list) or len(band) != 2:
        return None
    low, high = band
    if isinstance(low, bool) or isinstance(high, bool):
        return None
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return None
    return [float(low), float(high)]


def _excursion_minutes(readings: list[dict[str, Any]], low: float, high: float) -> float | None:
    points: list[tuple[datetime, float]] = []
    for row in readings:
        stamp = _moment(row.get("ts"))
        temperature = row.get("temperature_c")
        numeric = not isinstance(temperature, bool) and isinstance(temperature, (int, float))
        if stamp is None or not numeric:
            continue
        points.append((stamp, float(temperature)))
    if not points:
        return None
    points.sort()
    elapsed = 0.0
    for (start, temperature), (end, _next) in zip(points, points[1:], strict=False):
        if temperature < low or temperature > high:
            elapsed += max((end - start).total_seconds(), 0) / 60
    return elapsed


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def _evidence(raw: Any) -> dict[str, list[str]]:
    parsed = _decode(raw)
    codes = parsed.get("fault_codes") if isinstance(parsed, dict) else None
    if not isinstance(codes, list):
        return {"fault_codes": []}
    return {
        "fault_codes": [code.strip() for code in codes if isinstance(code, str) and code.strip()]
    }


def _option(option: dict[str, Any]) -> dict[str, Any] | None:
    name = option.get("id")
    if not isinstance(name, str) or not name.strip():
        return None
    return {
        "id": name.strip(),
        "spare_minutes": _plain_number(option.get("spare_minutes")),
        "projected_minutes": _plain_number(option.get("projected_minutes")),
        "feasible": option.get("feasible") is True,
    }


def _json_attr(raw: Any) -> dict[str, Any] | None:
    parsed = _decode(raw)
    return parsed if isinstance(parsed, dict) else None


def _decode(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, dict) and set(raw) <= {"S", "N", "M", "L", "BOOL", "NULL"}:
        try:
            parsed = _DESERIALIZER.deserialize(raw)
        except (TypeError, ValueError):
            return None
    else:
        parsed = raw
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError:
            return None
    if isinstance(parsed, Decimal):
        return float(parsed)
    return _plain(parsed)


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items() if key not in _PRIVATE}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _plain_number(value: Any) -> float | None:
    number = _plain(value)
    if isinstance(number, bool) or not isinstance(number, (int, float)):
        return None
    return float(number)


def _public(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items() if key not in _PRIVATE}
    if isinstance(value, list):
        return [_public(item) for item in value]
    return value
