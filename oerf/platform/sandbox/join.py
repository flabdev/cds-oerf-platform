"""Sandbox JOIN. An unknown number can open only an sbx session.

The daily cap and the delivery mode live under PLATFORM#sandbox. A tenant
reset deletes sbx sessions and contacts and leaves those platform rows in place.
"""

from __future__ import annotations

import hashlib
import threading
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from oerf.platform.channels.optout import is_opt_out
from oerf.platform.channels.registry import PhoneRegistry
from oerf.platform.inbound.normalize import NormalizedMessage
from oerf.platform.storage.dynamo import (
    epoch_seconds,
    from_epoch,
    is_conditional_failure,
    key,
    n,
    number,
    required_text,
    s,
    text,
)

SANDBOX_TENANT = "sbx"
DAILY_JOIN_CAP = 20
SESSION_LENGTH = timedelta(hours=24)
PLATFORM_PK = "PLATFORM#sandbox"
INTRODUCTION = (
    "You joined the OERF sandbox. This session lasts 24 hours and stays on the sandbox tenant."
)
DASHBOARD_NOTICE = (
    "Sandbox dashboard-only mode. This notice was not delivered to a phone or email."
)
FULL_NOTICE = "The sandbox is full for today. No session was created."
REJECTED_NOTICE = "This number cannot join the sandbox."
OPTED_OUT_NOTICE = "This number is opted out of the sandbox. No further message will be sent."
IGNORED_NOTICE = "The message was not a sandbox command."

Outcome = Literal[
    "created",
    "duplicate",
    "rejected",
    "full",
    "dashboard_only",
    "opted_out",
    "ignored",
]
Mode = Literal["join_phone", "dashboard_only"]


class SandboxSession(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    session_id: str
    contact_id: str
    shipment_id: str
    e164: str
    role: str
    started_at: datetime
    expires_at: datetime
    consent_at: datetime
    consent_source: str
    opt_in: bool


class JoinResult(BaseModel):
    """Reply for one inbound sandbox message. Production identifiers are never set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Outcome
    message: str
    simulated: bool
    delivered: bool
    channel: str | None = None
    session: SandboxSession | None = None


class SandboxGate(Protocol):
    def channel_send_allowed(self) -> bool:
        """False in dashboard-only mode, before any provider call."""


class SandboxStore(SandboxGate, Protocol):
    def mode(self) -> Mode:
        """Return the platform delivery mode."""

    def set_mode(self, mode: Mode) -> None:
        """Store the platform delivery mode."""

    def recall(self, message_id: str) -> JoinResult | None:
        """Return the stored result for this inbound id, if one was recorded."""

    def remember(self, message_id: str, result: JoinResult) -> None:
        """Record the first result for this inbound id."""

    def get_session(self, e164: str) -> SandboxSession | None:
        """Return the sbx session for this number, including an expired one."""

    def sessions(self) -> list[SandboxSession]:
        """Return every sbx session row."""

    def claim_join(self, session: SandboxSession, message_id: str, now: datetime) -> JoinResult:
        """Reserve a daily slot and store the session, or report why that did not happen."""

    def mark_opt_out(self, e164: str) -> SandboxSession | None:
        """Set the sbx session opt-in to false."""

    def clear_tenant_rows(self) -> None:
        """Delete sbx session and shipment rows. Platform cap and mode stay."""

    def join_count(self, day: str) -> int:
        """Return how many JOINs have been reserved for this UTC day."""


def handle_sandbox_message(
    message: NormalizedMessage,
    *,
    registry: PhoneRegistry,
    store: SandboxStore,
    now: datetime,
) -> JoinResult:
    """Accept JOIN for an unregistered number, or stop an sbx contact.

    The caller has already normalized the provider payload. This function does
    not read another tenant's contact and does not call a provider.
    """

    _aware(now)
    prior = store.recall(message.provider_msg_id)
    if prior is not None:
        _ensure_contact(registry, prior)
        return prior
    if is_opt_out(message.text):
        return _stop(message, registry=registry, store=store)
    if not _is_join(message.text):
        return _remember(store, message.provider_msg_id, _plain("ignored", IGNORED_NOTICE))
    e164 = required_text(message.sender, "sender")
    if _production_number(registry, e164):
        return _remember(
            store,
            message.provider_msg_id,
            _plain("rejected", REJECTED_NOTICE),
        )
    existing = store.get_session(e164)
    if existing is not None and not existing.opt_in:
        return _remember(
            store,
            message.provider_msg_id,
            _plain("opted_out", OPTED_OUT_NOTICE, session=existing),
        )
    if not store.channel_send_allowed():
        return _remember(store, message.provider_msg_id, _dashboard_result())
    result = store.claim_join(_session(message, now), message.provider_msg_id, now)
    _ensure_contact(registry, result)
    return result


def reset_sandbox_tenant(registry: PhoneRegistry, store: SandboxStore) -> None:
    """Drop sbx sessions and their contacts. The daily cap and mode remain."""

    contact_ids = [session.contact_id for session in store.sessions()]
    store.clear_tenant_rows()
    for contact_id in contact_ids:
        registry.remove_contact(SANDBOX_TENANT, contact_id)


class MemorySandboxStore:
    """Process-local sandbox rows. The cap update holds one lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mode: Mode = "join_phone"
        self._counts: dict[str, int] = {}
        self._sessions: dict[str, SandboxSession] = {}
        self._shipments: set[str] = set()
        self._receipts: dict[str, JoinResult] = {}

    def mode(self) -> Mode:
        return self._mode

    def set_mode(self, mode: Mode) -> None:
        if mode not in ("join_phone", "dashboard_only"):
            raise ValueError("mode must be join_phone or dashboard_only")
        self._mode = mode

    def channel_send_allowed(self) -> bool:
        return self._mode == "join_phone"

    def recall(self, message_id: str) -> JoinResult | None:
        with self._lock:
            return self._receipts.get(message_id)

    def remember(self, message_id: str, result: JoinResult) -> None:
        with self._lock:
            self._receipts.setdefault(message_id, result)

    def get_session(self, e164: str) -> SandboxSession | None:
        with self._lock:
            return self._sessions.get(e164)

    def sessions(self) -> list[SandboxSession]:
        with self._lock:
            return list(self._sessions.values())

    def claim_join(self, session: SandboxSession, message_id: str, now: datetime) -> JoinResult:
        with self._lock:
            prior = self._receipts.get(message_id)
            if prior is not None:
                return prior
            current = self._sessions.get(session.e164)
            if current is not None and not current.opt_in:
                result = _plain("opted_out", OPTED_OUT_NOTICE, session=current)
            elif current is not None and current.expires_at > now:
                result = _plain(
                    "duplicate",
                    INTRODUCTION,
                    session=current,
                    channel=session.consent_source,
                )
            elif self._counts.get(_day(now), 0) >= DAILY_JOIN_CAP:
                result = _plain("full", FULL_NOTICE)
            else:
                self._counts[_day(now)] = self._counts.get(_day(now), 0) + 1
                self._sessions[session.e164] = session
                self._shipments.add(session.shipment_id)
                result = _plain(
                    "created",
                    INTRODUCTION,
                    session=session,
                    channel=session.consent_source,
                )
            self._receipts[message_id] = result
            return result

    def mark_opt_out(self, e164: str) -> SandboxSession | None:
        with self._lock:
            current = self._sessions.get(e164)
            if current is None:
                return None
            updated = current.model_copy(update={"opt_in": False})
            self._sessions[e164] = updated
            return updated

    def clear_tenant_rows(self) -> None:
        with self._lock:
            self._sessions.clear()
            self._shipments.clear()

    def join_count(self, day: str) -> int:
        with self._lock:
            return self._counts.get(day, 0)


class DynamoSandboxStore:
    """Session rows on the tenant table. Cap and mode rows on the routing table."""

    def __init__(self, client: Any, routing_table: str, tenant_table: str) -> None:
        self._client = client
        self._routing = routing_table
        self._tenant = tenant_table

    def mode(self) -> Mode:
        item = self._platform_item("SETTINGS")
        if not item:
            return "join_phone"
        value = text(item["mode"])
        if value not in ("join_phone", "dashboard_only"):
            raise ValueError("mode must be join_phone or dashboard_only")
        return value

    def set_mode(self, mode: Mode) -> None:
        if mode not in ("join_phone", "dashboard_only"):
            raise ValueError("mode must be join_phone or dashboard_only")
        self._client.put_item(
            TableName=self._routing,
            Item={**key(PLATFORM_PK, "SETTINGS"), "mode": s(mode)},
        )

    def channel_send_allowed(self) -> bool:
        return self.mode() == "join_phone"

    def recall(self, message_id: str) -> JoinResult | None:
        item = self._platform_item(f"RECEIPT#{message_id}")
        if not item:
            return None
        e164 = text(item["e164"]) if "e164" in item else ""
        session = self.get_session(e164) if e164 else None
        channel = text(item["channel"]) if "channel" in item else ""
        return JoinResult.model_validate(
            {
                "outcome": text(item["outcome"]),
                "message": text(item["message"]),
                "simulated": text(item["simulated"]) == "true",
                "delivered": text(item["delivered"]) == "true",
                "channel": channel or None,
                "session": session,
            }
        )

    def remember(self, message_id: str, result: JoinResult) -> None:
        try:
            self._client.put_item(
                TableName=self._routing,
                Item=_receipt_item(message_id, result),
                ConditionExpression="attribute_not_exists(#pk)",
                ExpressionAttributeNames={"#pk": "pk"},
            )
        except Exception as exc:
            if not is_conditional_failure(exc):
                raise

    def get_session(self, e164: str) -> SandboxSession | None:
        loaded = self._client.get_item(
            TableName=self._tenant,
            Key=key(f"TENANT#{SANDBOX_TENANT}", f"SBXSESSION#{e164}"),
        )
        item = loaded.get("Item")
        if not item:
            return None
        return _session_from_item(item)

    def sessions(self) -> list[SandboxSession]:
        response = self._client.query(
            TableName=self._tenant,
            KeyConditionExpression="#pk = :pk AND begins_with(#sk, :prefix)",
            ExpressionAttributeNames={"#pk": "pk", "#sk": "sk"},
            ExpressionAttributeValues={
                ":pk": s(f"TENANT#{SANDBOX_TENANT}"),
                ":prefix": s("SBXSESSION#"),
            },
        )
        return [_session_from_item(item) for item in response.get("Items", [])]

    def claim_join(self, session: SandboxSession, message_id: str, now: datetime) -> JoinResult:
        prior = self.recall(message_id)
        if prior is not None:
            return prior
        result = _plain("created", INTRODUCTION, session=session, channel=session.consent_source)
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {"Update": _counter_update(self._routing, now)},
                    {"Put": _session_put(self._tenant, session, now)},
                    {"Put": _shipment_put(self._tenant, session)},
                    {
                        "Put": {
                            "TableName": self._routing,
                            "Item": _receipt_item(message_id, result),
                            "ConditionExpression": "attribute_not_exists(#pk)",
                            "ExpressionAttributeNames": {"#pk": "pk"},
                        }
                    },
                ]
            )
        except Exception as exc:
            if not is_conditional_failure(exc):
                raise
            stored = self.recall(message_id)
            if stored is not None:
                return stored
            decided = _decide(self, session.e164, now)
            self.remember(message_id, decided)
            return self.recall(message_id) or decided
        return result

    def mark_opt_out(self, e164: str) -> SandboxSession | None:
        current = self.get_session(e164)
        if current is None:
            return None
        updated = current.model_copy(update={"opt_in": False})
        self._client.put_item(TableName=self._tenant, Item=_session_item(updated))
        return updated

    def clear_tenant_rows(self) -> None:
        for prefix in ("SBXSESSION#", "SBXSHIP#"):
            response = self._client.query(
                TableName=self._tenant,
                KeyConditionExpression="#pk = :pk AND begins_with(#sk, :prefix)",
                ExpressionAttributeNames={"#pk": "pk", "#sk": "sk"},
                ExpressionAttributeValues={
                    ":pk": s(f"TENANT#{SANDBOX_TENANT}"),
                    ":prefix": s(prefix),
                },
            )
            for item in response.get("Items", []):
                self._client.delete_item(
                    TableName=self._tenant,
                    Key=key(text(item["pk"]), text(item["sk"])),
                )

    def join_count(self, day: str) -> int:
        item = self._platform_item(f"JOIN#{day}")
        if not item or "count" not in item:
            return 0
        return number(item["count"])

    def _platform_item(self, sort_key: str) -> dict[str, dict[str, str]] | None:
        loaded = self._client.get_item(TableName=self._routing, Key=key(PLATFORM_PK, sort_key))
        return loaded.get("Item")


def _stop(
    message: NormalizedMessage,
    *,
    registry: PhoneRegistry,
    store: SandboxStore,
) -> JoinResult:
    e164 = required_text(message.sender, "sender")
    contact_id = _contact_id(e164)
    if registry.get_contact(SANDBOX_TENANT, contact_id) is not None:
        registry.set_opt_in(SANDBOX_TENANT, contact_id, False)
    session = store.mark_opt_out(e164)
    if session is None and registry.get_contact(SANDBOX_TENANT, contact_id) is None:
        return _remember(store, message.provider_msg_id, _plain("ignored", IGNORED_NOTICE))
    return _remember(
        store,
        message.provider_msg_id,
        _plain("opted_out", OPTED_OUT_NOTICE, session=session),
    )


def _ensure_contact(registry: PhoneRegistry, result: JoinResult) -> None:
    session = result.session
    if result.outcome != "created" or session is None:
        return
    registry.register_contact(
        SANDBOX_TENANT,
        session.contact_id,
        session.role,
        session.e164,
        opt_in=True,
    )


def _production_number(registry: PhoneRegistry, e164: str) -> bool:
    """True when the phone index has a row for a tenant other than sbx.

    Those rows are not copied into the reply.
    """

    resolution = registry.resolve_sender(e164)
    return any(match.tenant_id != SANDBOX_TENANT for match in resolution.matches)


def _remember(store: SandboxStore, message_id: str, result: JoinResult) -> JoinResult:
    store.remember(message_id, result)
    return store.recall(message_id) or result


def _plain(
    outcome: Outcome,
    message: str,
    *,
    session: SandboxSession | None = None,
    channel: str | None = None,
    simulated: bool = False,
) -> JoinResult:
    return JoinResult(
        outcome=outcome,
        message=message,
        simulated=simulated,
        delivered=False,
        channel=channel,
        session=session,
    )


def _dashboard_result() -> JoinResult:
    return _plain(
        "dashboard_only",
        DASHBOARD_NOTICE,
        channel="dashboard",
        simulated=True,
    )


def _session(message: NormalizedMessage, now: datetime) -> SandboxSession:
    e164 = required_text(message.sender, "sender")
    token = _token(e164)
    started = epoch_seconds(now)
    return SandboxSession(
        tenant_id=SANDBOX_TENANT,
        session_id=f"SES-{token}-{started}",
        contact_id=_contact_id(e164),
        shipment_id=f"SBX-{token}-{started}",
        e164=e164,
        role="driver",
        started_at=now,
        expires_at=now + SESSION_LENGTH,
        consent_at=now,
        consent_source=message.channel,
        opt_in=True,
    )


def _contact_id(e164: str) -> str:
    return f"C-SBX-{_token(e164)}"


def _token(e164: str) -> str:
    return hashlib.sha256(e164.encode()).hexdigest()[:8].upper()


def _day(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%d")


def _aware(now: datetime) -> None:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")


def _is_join(body: str | None) -> bool:
    if body is None:
        return False
    return " ".join(body.strip().casefold().split()) == "join"


def _decide(store: SandboxStore, e164: str, now: datetime) -> JoinResult:
    session = store.get_session(e164)
    if session is not None and not session.opt_in:
        return _plain("opted_out", OPTED_OUT_NOTICE, session=session)
    if session is not None and session.expires_at > now:
        return _plain("duplicate", INTRODUCTION, session=session, channel=session.consent_source)
    if store.join_count(_day(now)) >= DAILY_JOIN_CAP:
        return _plain("full", FULL_NOTICE)
    return _plain("full", FULL_NOTICE)


def _counter_update(table: str, now: datetime) -> dict[str, Any]:
    return {
        "TableName": table,
        "Key": key(PLATFORM_PK, f"JOIN#{_day(now)}"),
        "UpdateExpression": "SET #count = if_not_exists(#count, :zero) + :one",
        "ConditionExpression": "attribute_not_exists(#count) OR #count < :cap",
        "ExpressionAttributeNames": {"#count": "count"},
        "ExpressionAttributeValues": {
            ":zero": n(0),
            ":one": n(1),
            ":cap": n(DAILY_JOIN_CAP),
        },
    }


def _session_put(table: str, session: SandboxSession, now: datetime) -> dict[str, Any]:
    return {
        "TableName": table,
        "Item": _session_item(session),
            "ConditionExpression": (
                "attribute_not_exists(#pk) OR #expires_at <= :now AND #opt_in = :yes"
            ),
        "ExpressionAttributeNames": {"#pk": "pk", "#expires_at": "expires_at", "#opt_in": "opt_in"},
        "ExpressionAttributeValues": {":now": n(epoch_seconds(now)), ":yes": s("true")},
    }


def _shipment_put(table: str, session: SandboxSession) -> dict[str, Any]:
    return {
        "TableName": table,
        "Item": {
            **key(f"TENANT#{SANDBOX_TENANT}", f"SBXSHIP#{session.shipment_id}"),
            "tenant_id": s(SANDBOX_TENANT),
            "shipment_id": s(session.shipment_id),
            "session_id": s(session.session_id),
            "e164": s(session.e164),
        },
        "ConditionExpression": "attribute_not_exists(#pk)",
        "ExpressionAttributeNames": {"#pk": "pk"},
    }


def _receipt_item(message_id: str, result: JoinResult) -> dict[str, dict[str, str]]:
    item = {
        **key(PLATFORM_PK, f"RECEIPT#{message_id}"),
        "outcome": s(result.outcome),
        "message": s(result.message),
        "simulated": s("true" if result.simulated else "false"),
        "delivered": s("true" if result.delivered else "false"),
    }
    if result.channel:
        item["channel"] = s(result.channel)
    if result.session is not None:
        item["e164"] = s(result.session.e164)
    return item


def _session_item(session: SandboxSession) -> dict[str, dict[str, str]]:
    return {
        **key(f"TENANT#{SANDBOX_TENANT}", f"SBXSESSION#{session.e164}"),
        "tenant_id": s(session.tenant_id),
        "session_id": s(session.session_id),
        "contact_id": s(session.contact_id),
        "shipment_id": s(session.shipment_id),
        "e164": s(session.e164),
        "role": s(session.role),
        "started_at": n(epoch_seconds(session.started_at)),
        "expires_at": n(epoch_seconds(session.expires_at)),
        "consent_at": n(epoch_seconds(session.consent_at)),
        "consent_source": s(session.consent_source),
        "opt_in": s("true" if session.opt_in else "false"),
    }


def _session_from_item(item: dict[str, dict[str, str]]) -> SandboxSession:
    return SandboxSession(
        tenant_id=text(item["tenant_id"]),
        session_id=text(item["session_id"]),
        contact_id=text(item["contact_id"]),
        shipment_id=text(item["shipment_id"]),
        e164=text(item["e164"]),
        role=text(item["role"]),
        started_at=from_epoch(number(item["started_at"])),
        expires_at=from_epoch(number(item["expires_at"])),
        consent_at=from_epoch(number(item["consent_at"])),
        consent_source=text(item["consent_source"]),
        opt_in=text(item["opt_in"]) == "true",
    )
