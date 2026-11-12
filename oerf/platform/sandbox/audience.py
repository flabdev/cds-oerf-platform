"""Dev-only audience drivers (ADR-040).

``JOIN DRIVER`` registers the sender as a t1 driver for 24 hours, assigns one
seeded shipment, and never logs the number. The three team contacts stay
seeded. Main leaves this path off.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from oerf.platform.channels.registry import PhoneRegistry
from oerf.platform.inbound.normalize import NormalizedMessage
from oerf.platform.storage.dynamo import (
    canonical_e164,
    contact_sk,
    epoch_seconds,
    from_epoch,
    key,
    n,
    number,
    phone_pk,
    required_text,
    s,
    text,
)

AUDIENCE_TENANT = "t1"
AUDIENCE_KEYWORD = "join driver"
DAILY_CAP = 20
SESSION_LENGTH = timedelta(hours=24)
DISPLAY_PREFIX = "Audience"


class AudienceSession(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    contact_id: str
    display_name: str
    shipment_id: str
    e164: str
    started_at: datetime
    expires_at: datetime
    consent_at: datetime
    seed_driver: str = ""


class AudienceResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: str
    message: str
    session: AudienceSession | None = None


class AudienceStore(Protocol):
    def recall(self, message_id: str) -> AudienceResult | None:
        """Return the stored result for this inbound id."""

    def claim(
        self,
        message: NormalizedMessage,
        now: datetime,
        registry: PhoneRegistry,
    ) -> AudienceResult:
        """Register one audience driver, or report why that did not happen."""

    def sessions(self) -> list[AudienceSession]:
        """Return every audience session, including expired ones."""

    def remove(self, contact_id: str, registry: PhoneRegistry) -> None:
        """Delete one audience driver and its phone row."""

    def assign(
        self,
        contact_id: str,
        shipment_id: str,
        registry: PhoneRegistry,
    ) -> AudienceSession | None:
        """Point one live audience driver at a free shipment."""


def is_audience_join(body: str | None) -> bool:
    """True for the audience keyword, ignoring case and extra spaces."""

    if body is None:
        return False
    return " ".join(body.strip().casefold().split()) == AUDIENCE_KEYWORD


def mask_number(e164: str) -> str:
    """A desk label. The number itself is not returned."""

    digits = "".join(ch for ch in e164 if ch.isdigit())
    if len(digits) < 2:
        return "•••"
    return f"•••{digits[-2:]}"


def public_driver_name(label: str | None) -> str:
    """A display name. A value that contains a phone-length digit run is blank."""

    if not isinstance(label, str):
        return ""
    name = " ".join(label.split())
    digits = "".join(ch for ch in name if ch.isdigit())
    if not name or len(digits) >= 6:
        return ""
    return name


def audience_join_link(e164: str) -> str:
    """WhatsApp click-to-chat with the audience keyword. The caller supplies the number."""

    number = canonical_e164(required_text(e164, "e164"))
    return f"https://wa.me/{number.removeprefix('+')}?text=JOIN%20DRIVER"


def shipment_pool(tenant_id: str = AUDIENCE_TENANT) -> tuple[str, ...]:
    """Audience-pool shipment ids for this tenant, in seed order."""

    from sim.seed.load import load_seed

    rows = load_seed(tenant_id).get("shipments") or []
    found: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("audience_pool") is not True:
            continue
        shipment_id = str(row.get("shipment_id") or "").strip()
        if shipment_id:
            found.append(shipment_id)
    return tuple(found)


def handle_audience_message(
    message: NormalizedMessage,
    *,
    registry: PhoneRegistry,
    store: AudienceStore,
    now: datetime,
) -> AudienceResult:
    """Accept JOIN DRIVER, or leave every other text for the normal inbound path."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    prior = store.recall(message.provider_msg_id)
    if prior is not None:
        return prior
    if not is_audience_join(message.text):
        return AudienceResult(outcome="ignored", message="")
    _drop_expired(store, registry, now)
    return store.claim(message, now, registry)


def list_drivers(store: AudienceStore, now: datetime) -> list[dict[str, str]]:
    """Live audience drivers. Numbers are masked."""

    rows: list[dict[str, str]] = []
    for session in store.sessions():
        if session.expires_at <= now:
            continue
        rows.append(
            {
                "name": session.display_name,
                "number": mask_number(session.e164),
                "shipment_id": session.shipment_id,
            }
        )
    return rows


class MemoryAudienceStore:
    """Process-local audience rows for tests."""

    def __init__(self, pool: tuple[str, ...] = ()) -> None:
        self._pool = pool
        self._sessions: dict[str, AudienceSession] = {}
        self._by_message: dict[str, AudienceResult] = {}
        self._counts: dict[str, int] = {}

    def recall(self, message_id: str) -> AudienceResult | None:
        return self._by_message.get(message_id)

    def claim(
        self,
        message: NormalizedMessage,
        now: datetime,
        registry: PhoneRegistry,
    ) -> AudienceResult:
        e164 = canonical_e164(required_text(message.sender, "sender"))
        if _team_number(registry, e164):
            return self._remember(message.provider_msg_id, "rejected", "This number cannot join.")
        existing = self._by_phone(e164)
        if existing is not None and existing.expires_at > now:
            return self._remember(
                message.provider_msg_id,
                "duplicate",
                "You are already registered for this demo.",
                existing,
            )
        day = now.astimezone(UTC).strftime("%Y-%m-%d")
        if self._counts.get(day, 0) >= DAILY_CAP:
            return self._remember(message.provider_msg_id, "full", "The demo is full for today.")
        shipment_id = _free_shipment(self._pool, self._sessions.values(), now)
        if shipment_id is None:
            return self._remember(message.provider_msg_id, "full", "No demo shipment is free.")
        contact_id = _contact_id(e164)
        session = AudienceSession(
            tenant_id=AUDIENCE_TENANT,
            contact_id=contact_id,
            display_name=f"{DISPLAY_PREFIX} {contact_id[-4:]}",
            shipment_id=shipment_id,
            e164=e164,
            started_at=now,
            expires_at=now + SESSION_LENGTH,
            consent_at=now,
        )
        self._sessions[contact_id] = session
        self._counts[day] = self._counts.get(day, 0) + 1
        _register(registry, session)
        return self._remember(
            message.provider_msg_id,
            "created",
            "You joined as a driver for 24 hours.",
            session,
        )

    def sessions(self) -> list[AudienceSession]:
        return list(self._sessions.values())

    def remove(self, contact_id: str, registry: PhoneRegistry) -> None:
        session = self._sessions.pop(contact_id, None)
        if session is not None:
            registry.remove_contact(session.tenant_id, session.contact_id)

    def assign(
        self,
        contact_id: str,
        shipment_id: str,
        registry: PhoneRegistry,
    ) -> AudienceSession | None:
        del registry
        current = self._sessions.get(contact_id)
        if current is None or shipment_id not in self._pool:
            return None
        taken = {
            session.shipment_id
            for session in self._sessions.values()
            if session.contact_id != contact_id
        }
        if shipment_id in taken:
            return None
        updated = current.model_copy(update={"shipment_id": shipment_id})
        self._sessions[contact_id] = updated
        return updated

    def _by_phone(self, e164: str) -> AudienceSession | None:
        for session in self._sessions.values():
            if session.e164 == e164:
                return session
        return None

    def _remember(
        self,
        message_id: str,
        outcome: str,
        message: str,
        session: AudienceSession | None = None,
    ) -> AudienceResult:
        result = AudienceResult(outcome=outcome, message=message, session=session)
        self._by_message[message_id] = result
        return result


class DynamoAudienceStore:
    """Audience rows on the tenant table. The phone stays off the desk."""

    def __init__(
        self,
        client: object,
        tenant_table: str,
        routing_table: str,
        pool: tuple[str, ...],
    ) -> None:
        self._client = client
        self._tenant = tenant_table
        self._routing = routing_table
        self._pool = pool

    def recall(self, message_id: str) -> AudienceResult | None:
        item = self._get(self._tenant, f"TENANT#{AUDIENCE_TENANT}", f"AUDIENCEMSG#{message_id}")
        if not item:
            return None
        contact_id = text(item.get("contact_id", {"S": ""}))
        session = self._session(contact_id) if contact_id else None
        return AudienceResult(
            outcome=text(item["outcome"]),
            message=text(item.get("notice", {"S": ""})),
            session=session,
        )

    def claim(
        self,
        message: NormalizedMessage,
        now: datetime,
        registry: PhoneRegistry,
    ) -> AudienceResult:
        e164 = canonical_e164(required_text(message.sender, "sender"))
        if _team_number(registry, e164):
            return self._remember(message.provider_msg_id, "rejected", "This number cannot join.")
        existing = self._session_for_phone(e164)
        if existing is not None and existing.expires_at > now:
            return self._remember(
                message.provider_msg_id,
                "duplicate",
                "You are already registered for this demo.",
                existing,
            )
        day = now.astimezone(UTC).strftime("%Y-%m-%d")
        if self._count(day) >= DAILY_CAP:
            return self._remember(message.provider_msg_id, "full", "The demo is full for today.")
        shipment_id = _free_shipment(self._pool, self.sessions(), now)
        if shipment_id is None:
            return self._remember(message.provider_msg_id, "full", "No demo shipment is free.")
        contact_id = _contact_id(e164)
        session = AudienceSession(
            tenant_id=AUDIENCE_TENANT,
            contact_id=contact_id,
            display_name=f"{DISPLAY_PREFIX} {contact_id[-4:]}",
            shipment_id=shipment_id,
            e164=e164,
            started_at=now,
            expires_at=now + SESSION_LENGTH,
            consent_at=now,
            seed_driver=self._seed_driver(shipment_id),
        )
        _register(registry, session)
        self._stamp_contact(session)
        self._put_session(session)
        self._point_shipment(session)
        self._add_count(day)
        return self._remember(
            message.provider_msg_id,
            "created",
            "You joined as a driver for 24 hours.",
            session,
        )

    def sessions(self) -> list[AudienceSession]:
        response = self._client.scan(TableName=self._tenant)  # type: ignore[attr-defined]
        found: list[AudienceSession] = []
        for item in response.get("Items", []):
            if text(item.get("pk", {"S": ""})) != f"TENANT#{AUDIENCE_TENANT}":
                continue
            if not text(item.get("sk", {"S": ""})).startswith("AUDIENCE#"):
                continue
            if text(item.get("sk", {"S": ""})).startswith("AUDIENCEMSG#"):
                continue
            found.append(_session_from_item(item))
        return found

    def remove(self, contact_id: str, registry: PhoneRegistry) -> None:
        session = self._session(contact_id)
        if session is not None:
            self._restore_driver(session)
            registry.remove_contact(session.tenant_id, session.contact_id)
        self._client.delete_item(  # type: ignore[attr-defined]
            TableName=self._tenant,
            Key=key(f"TENANT#{AUDIENCE_TENANT}", f"AUDIENCE#{contact_id}"),
        )

    def assign(
        self,
        contact_id: str,
        shipment_id: str,
        registry: PhoneRegistry,
    ) -> AudienceSession | None:
        del registry
        current = self._session(contact_id)
        if current is None or shipment_id not in self._pool:
            return None
        taken = {
            session.shipment_id
            for session in self.sessions()
            if session.contact_id != contact_id and session.expires_at > datetime.now(UTC)
        }
        if shipment_id in taken:
            return None
        if shipment_id != current.shipment_id:
            self._restore_driver(current)
            seed = self._seed_driver(shipment_id)
        else:
            seed = current.seed_driver
        updated = current.model_copy(update={"shipment_id": shipment_id, "seed_driver": seed})
        self._put_session(updated)
        self._point_shipment(updated)
        return updated

    def _session(self, contact_id: str) -> AudienceSession | None:
        item = self._get(self._tenant, f"TENANT#{AUDIENCE_TENANT}", f"AUDIENCE#{contact_id}")
        if not item:
            return None
        return _session_from_item(item)

    def _session_for_phone(self, e164: str) -> AudienceSession | None:
        for session in self.sessions():
            if session.e164 == e164:
                return session
        return None

    def _count(self, day: str) -> int:
        item = self._get(self._routing, f"AUDIENCE#JOINS#{day}", "META")
        if not item or "count" not in item:
            return 0
        return int(number(item["count"]))

    def _add_count(self, day: str) -> None:
        current = self._count(day)
        self._client.put_item(  # type: ignore[attr-defined]
            TableName=self._routing,
            Item={
                **key(f"AUDIENCE#JOINS#{day}", "META"),
                "count": n(current + 1),
            },
        )

    def _put_session(self, session: AudienceSession) -> None:
        self._client.put_item(  # type: ignore[attr-defined]
            TableName=self._tenant,
            Item={
                **key(f"TENANT#{session.tenant_id}", f"AUDIENCE#{session.contact_id}"),
                "tenant_id": s(session.tenant_id),
                "contact_id": s(session.contact_id),
                "display_name": s(session.display_name),
                "shipment_id": s(session.shipment_id),
                "e164": s(session.e164),
                "audience": s("true"),
                "expires_at": n(epoch_seconds(session.expires_at)),
                "started_at": n(epoch_seconds(session.started_at)),
                "consent_at": n(epoch_seconds(session.consent_at)),
                "seed_driver": s(session.seed_driver),
            },
        )

    def _stamp_contact(self, session: AudienceSession) -> None:
        loaded = self._get(
            self._tenant,
            f"TENANT#{session.tenant_id}",
            f"CONTACT#{session.contact_id}",
        )
        if not loaded:
            return
        loaded["label"] = s(session.display_name)
        loaded["audience"] = s("true")
        self._client.put_item(TableName=self._tenant, Item=loaded)  # type: ignore[attr-defined]
        phone = self._get(
            self._routing,
            phone_pk(session.e164),
            contact_sk(session.tenant_id, session.contact_id),
        )
        if phone:
            phone["audience"] = s("true")
            self._client.put_item(TableName=self._routing, Item=phone)  # type: ignore[attr-defined]

    def _seed_driver(self, shipment_id: str) -> str:
        """The driver to put back. An audience id is not a seed."""

        item = self._get(
            self._tenant,
            f"TENANT#{AUDIENCE_TENANT}",
            f"SHIPMENT#{shipment_id}",
        )
        if not item or "driver" not in item:
            return ""
        driver = text(item["driver"])
        if driver.startswith("AUD-"):
            return ""
        return driver

    def _restore_driver(self, session: AudienceSession) -> None:
        item = self._get(
            self._tenant,
            f"TENANT#{session.tenant_id}",
            f"SHIPMENT#{session.shipment_id}",
        )
        if not item or "driver" not in item:
            return
        if text(item["driver"]) != session.contact_id:
            return
        if session.seed_driver:
            item["driver"] = s(session.seed_driver)
        else:
            item.pop("driver", None)
        self._client.put_item(TableName=self._tenant, Item=item)  # type: ignore[attr-defined]

    def _point_shipment(self, session: AudienceSession) -> None:
        item = self._get(
            self._tenant,
            f"TENANT#{session.tenant_id}",
            f"SHIPMENT#{session.shipment_id}",
        )
        if not item:
            return
        item["driver"] = s(session.contact_id)
        self._client.put_item(TableName=self._tenant, Item=item)  # type: ignore[attr-defined]

    def _remember(
        self,
        message_id: str,
        outcome: str,
        notice: str,
        session: AudienceSession | None = None,
    ) -> AudienceResult:
        item = {
            **key(f"TENANT#{AUDIENCE_TENANT}", f"AUDIENCEMSG#{message_id}"),
            "outcome": s(outcome),
            "notice": s(notice),
        }
        if session is not None:
            item["contact_id"] = s(session.contact_id)
        self._client.put_item(TableName=self._tenant, Item=item)  # type: ignore[attr-defined]
        return AudienceResult(outcome=outcome, message=notice, session=session)

    def _get(self, table: str, pk: str, sk: str) -> dict | None:
        loaded = self._client.get_item(TableName=table, Key=key(pk, sk))  # type: ignore[attr-defined]
        item = loaded.get("Item")
        return item if isinstance(item, dict) else None


def _drop_expired(store: AudienceStore, registry: PhoneRegistry, now: datetime) -> None:
    for session in list(store.sessions()):
        if session.expires_at <= now:
            store.remove(session.contact_id, registry)


def _team_number(registry: PhoneRegistry, e164: str) -> bool:
    resolution = registry.resolve_sender(e164)
    return any(not match.contact_id.startswith("AUD-") for match in resolution.matches)


def _free_shipment(
    pool: tuple[str, ...],
    sessions: object,
    now: datetime,
) -> str | None:
    taken = {
        session.shipment_id
        for session in sessions
        if isinstance(session, AudienceSession) and session.expires_at > now
    }
    for shipment_id in pool:
        if shipment_id not in taken:
            return shipment_id
    return None


def _contact_id(e164: str) -> str:
    digest = hashlib.sha256(e164.encode()).hexdigest()[:8].upper()
    return f"AUD-{digest}"


def _register(registry: PhoneRegistry, session: AudienceSession) -> None:
    registry.register_contact(
        session.tenant_id,
        session.contact_id,
        "driver",
        session.e164,
        opt_in=True,
    )


def _session_from_item(item: dict) -> AudienceSession:
    return AudienceSession(
        tenant_id=text(item["tenant_id"]),
        contact_id=text(item["contact_id"]),
        display_name=text(item["display_name"]),
        shipment_id=text(item["shipment_id"]),
        e164=text(item["e164"]),
        started_at=from_epoch(number(item["started_at"])),
        expires_at=from_epoch(number(item["expires_at"])),
        consent_at=from_epoch(number(item["consent_at"])),
        seed_driver=text(item["seed_driver"]) if "seed_driver" in item else "",
    )
