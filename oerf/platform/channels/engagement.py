"""Waiting engagements and the outbound message that a reply quotes.

The incident item is ``TENANT#{tenant}#INC#{incident}`` / ``ENG#{engagement}``.
A reply lookup is one query on ``OUTMSG#{provider_msg_id}``. That row expires
90 days after it is registered. A non-WAITING row does not resume.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from oerf.platform.storage.dynamo import (
    addr_pk,
    incident_pk,
    is_conditional_failure,
    iso_z,
    key,
    n,
    number,
    outmsg_pk,
    required_text,
    s,
    text,
)
from oerf.platform.tenancy.context import TenantContext

EngagementStatus = Literal["WAITING", "ANSWERED", "EXPIRED", "STOPPED", "CALLBACK_PENDING"]
ACTIVE_STATUSES = frozenset({"WAITING", "CALLBACK_PENDING"})
CORRELATION_TTL = timedelta(days=90)


class Engagement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    incident_id: str
    engagement_id: str
    role: str
    ask: str
    policy_id: str
    task_token: str
    facts: dict[str, Any]
    contact_id: str
    status: EngagementStatus
    rung: int = Field(ge=1)
    deadline: datetime


class EngagementStore(Protocol):
    """Persistence behind an open engagement and its quoted outbound ids."""

    def open_engagement(
        self,
        tenant_id: str,
        incident_id: str,
        engagement_id: str,
        role: str,
        ask: str,
        policy_id: str,
        task_token: str,
        facts: dict[str, Any],
        *,
        contact_id: str,
        rung: int,
        deadline: datetime,
    ) -> Engagement:
        """Create a WAITING engagement, or return the one already stored."""

    def prepare_wait(
        self,
        tenant_id: str,
        incident_id: str,
        engagement_id: str,
        task_token: str,
        *,
        rung: int,
        deadline: datetime,
    ) -> Engagement:
        """Persist the current rung token before outbound delivery begins."""

    def register_outbound(self, provider_msg_id: str, engagement: Engagement) -> None:
        """Map one provider message id to a waiting engagement."""

    def register_address(self, channel: str, address: str, engagement: Engagement) -> None:
        """Map one channel address to the open engagement a later unquoted reply can use."""

    def for_address(self, channel: str, address: str) -> list[Engagement]:
        """Return WAITING engagements registered at this channel address."""

    def correlate(self, provider_msg_id: str) -> Engagement | None:
        """Return the WAITING engagement quoted by this id, if it is still waiting."""

    def get(
        self, tenant_id: str, incident_id: str, engagement_id: str
    ) -> Engagement | None:
        """Return one tenant's engagement. Another tenant's key does not match."""

    def stop(self, tenant_id: str, incident_id: str, engagement_id: str) -> Engagement | None:
        """Mark an active engagement STOPPED. A second stop is a no-op."""

    def answer(self, tenant_id: str, incident_id: str, engagement_id: str) -> Engagement:
        """Mark a WAITING engagement ANSWERED."""

    def for_contact(self, tenant_id: str, contact_id: str) -> list[Engagement]:
        """Return engagements opened for one contact in one tenant."""


class MemoryEngagementStore:
    """In-memory engagement store used by tests and local runs."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._engagements: dict[tuple[str, str, str], Engagement] = {}
        self._outbound: dict[str, tuple[Engagement, datetime]] = {}
        self._by_contact: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self._by_address: dict[tuple[str, str], list[tuple[str, str, str]]] = {}

    def open_engagement(
        self,
        tenant_id: str,
        incident_id: str,
        engagement_id: str,
        role: str,
        ask: str,
        policy_id: str,
        task_token: str,
        facts: dict[str, Any],
        *,
        contact_id: str,
        rung: int,
        deadline: datetime,
    ) -> Engagement:
        engagement = _new_engagement(
            tenant_id,
            incident_id,
            engagement_id,
            role,
            ask,
            policy_id,
            task_token,
            facts,
            contact_id=contact_id,
            rung=rung,
            deadline=deadline,
        )
        stored_key = (engagement.tenant_id, engagement.incident_id, engagement.engagement_id)
        existing = self._engagements.get(stored_key)
        if existing is not None:
            return existing
        self._engagements[stored_key] = engagement
        refs = self._by_contact.setdefault((engagement.tenant_id, engagement.contact_id), [])
        refs.append((engagement.incident_id, engagement.engagement_id))
        return engagement

    def prepare_wait(
        self,
        tenant_id: str,
        incident_id: str,
        engagement_id: str,
        task_token: str,
        *,
        rung: int,
        deadline: datetime,
    ) -> Engagement:
        """Replace the active token for a new rung, conditional on WAITING."""
        current = self.get(tenant_id, incident_id, engagement_id)
        if current is None or current.status != "WAITING":
            raise ValueError("only a WAITING engagement can prepare a task-token wait")
        updated = current.model_copy(
            update={
                "task_token": required_text(task_token, "task_token"),
                "rung": rung,
                "deadline": deadline,
            }
        )
        self._engagements[(updated.tenant_id, updated.incident_id, updated.engagement_id)] = updated
        return updated

    def register_outbound(self, provider_msg_id: str, engagement: Engagement) -> None:
        message_id = required_text(provider_msg_id, "provider_msg_id")
        current = self.get(engagement.tenant_id, engagement.incident_id, engagement.engagement_id)
        if current is None or current.status != "WAITING":
            raise ValueError("outbound correlation requires a WAITING engagement")
        if current.task_token != engagement.task_token:
            raise ValueError("outbound correlation must use the current task token")
        existing = self._outbound.get(message_id)
        if existing is not None:
            if _same_engagement(existing[0], current):
                return
            raise ValueError(f"provider message {message_id!r} is already correlated")
        self._outbound[message_id] = (current, self._clock() + CORRELATION_TTL)

    def register_address(self, channel: str, address: str, engagement: Engagement) -> None:
        current = _waiting(self, engagement)
        pointer = (current.tenant_id, current.incident_id, current.engagement_id)
        refs = self._by_address.setdefault(_address_key(channel, address), [])
        if pointer not in refs:
            refs.append(pointer)

    def for_address(self, channel: str, address: str) -> list[Engagement]:
        found: list[Engagement] = []
        for tenant_id, incident_id, engagement_id in self._by_address.get(
            _address_key(channel, address), []
        ):
            current = self.get(tenant_id, incident_id, engagement_id)
            if current is not None and current.status == "WAITING":
                found.append(current)
        return found

    def correlate(self, provider_msg_id: str) -> Engagement | None:
        found = self._outbound.get(required_text(provider_msg_id, "provider_msg_id"))
        if found is None:
            return None
        engagement, expires_at = found
        if self._clock() >= expires_at:
            return None
        current = self.get(engagement.tenant_id, engagement.incident_id, engagement.engagement_id)
        if current is None or current.status != "WAITING":
            return None
        return current

    def get(self, tenant_id: str, incident_id: str, engagement_id: str) -> Engagement | None:
        context = TenantContext(tenant_id=tenant_id)
        incident = required_text(incident_id, "incident_id")
        engagement = required_text(engagement_id, "engagement_id")
        return self._engagements.get((context.tenant_id, incident, engagement))

    def stop(self, tenant_id: str, incident_id: str, engagement_id: str) -> Engagement | None:
        current = self.get(tenant_id, incident_id, engagement_id)
        if current is None or current.status == "STOPPED":
            return current
        if current.status not in ACTIVE_STATUSES:
            return current
        updated = current.model_copy(update={"status": "STOPPED"})
        self._engagements[(updated.tenant_id, updated.incident_id, updated.engagement_id)] = updated
        return updated

    def answer(self, tenant_id: str, incident_id: str, engagement_id: str) -> Engagement:
        current = self.get(tenant_id, incident_id, engagement_id)
        if current is None or current.status != "WAITING":
            raise ValueError("only a WAITING engagement can be answered")
        updated = current.model_copy(update={"status": "ANSWERED"})
        self._engagements[(updated.tenant_id, updated.incident_id, updated.engagement_id)] = updated
        return updated

    def for_contact(self, tenant_id: str, contact_id: str) -> list[Engagement]:
        context = TenantContext(tenant_id=tenant_id)
        contact = required_text(contact_id, "contact_id")
        found: list[Engagement] = []
        for incident_id, engagement_id in self._by_contact.get((context.tenant_id, contact), []):
            engagement = self.get(context.tenant_id, incident_id, engagement_id)
            if engagement is not None:
                found.append(engagement)
        return found


class DynamoEngagementStore:
    """Engagement rows on the incident table and correlation rows on the routing table."""

    def __init__(
        self,
        client: Any,
        incident_table: str,
        routing_table: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._incident = incident_table
        self._routing = routing_table
        self._clock = clock or (lambda: datetime.now(UTC))

    def open_engagement(
        self,
        tenant_id: str,
        incident_id: str,
        engagement_id: str,
        role: str,
        ask: str,
        policy_id: str,
        task_token: str,
        facts: dict[str, Any],
        *,
        contact_id: str,
        rung: int,
        deadline: datetime,
    ) -> Engagement:
        engagement = _new_engagement(
            tenant_id,
            incident_id,
            engagement_id,
            role,
            ask,
            policy_id,
            task_token,
            facts,
            contact_id=contact_id,
            rung=rung,
            deadline=deadline,
        )
        existing = self.get(engagement.tenant_id, engagement.incident_id, engagement.engagement_id)
        if existing is not None:
            return existing
        pk = incident_pk(engagement.tenant_id, engagement.incident_id)
        sk = f"ENG#{engagement.engagement_id}"
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._incident,
                            "Item": _engagement_item(pk, sk, engagement),
                            "ConditionExpression": "attribute_not_exists(#sk)",
                            "ExpressionAttributeNames": {"#sk": "sk"},
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._routing,
                            "Item": {
                                **key(
                                    f"CONTACTENG#{engagement.tenant_id}#{engagement.contact_id}",
                                    f"ENG#{engagement.incident_id}#{engagement.engagement_id}",
                                ),
                                "tenant_id": s(engagement.tenant_id),
                                "incident_id": s(engagement.incident_id),
                                "engagement_id": s(engagement.engagement_id),
                            },
                            "ConditionExpression": "attribute_not_exists(#sk)",
                            "ExpressionAttributeNames": {"#sk": "sk"},
                        }
                    },
                ]
            )
        except Exception as exc:
            if not is_conditional_failure(exc):
                raise
            stored = self.get(
                engagement.tenant_id, engagement.incident_id, engagement.engagement_id
            )
            if stored is None:
                raise
            return stored
        return engagement

    def prepare_wait(
        self,
        tenant_id: str,
        incident_id: str,
        engagement_id: str,
        task_token: str,
        *,
        rung: int,
        deadline: datetime,
    ) -> Engagement:
        """Replace the active token before this rung's outbound message."""
        current = self.get(tenant_id, incident_id, engagement_id)
        if current is None:
            raise KeyError(f"engagement {engagement_id!r} is not open")
        if current.status != "WAITING":
            raise ValueError("only a WAITING engagement can prepare a task-token wait")
        updated = current.model_copy(
            update={
                "task_token": required_text(task_token, "task_token"),
                "rung": rung,
                "deadline": deadline,
            }
        )
        try:
            self._client.update_item(
                TableName=self._incident,
                Key=key(
                    incident_pk(updated.tenant_id, updated.incident_id),
                    f"ENG#{updated.engagement_id}",
                ),
                UpdateExpression=(
                    "SET #token = :token, #rung = :rung, "
                    "#deadline = :deadline, #document = :document"
                ),
                ConditionExpression="#status = :waiting",
                ExpressionAttributeNames={
                    "#token": "task_token",
                    "#rung": "rung",
                    "#deadline": "deadline",
                    "#document": "document",
                    "#status": "status",
                },
                ExpressionAttributeValues={
                    ":token": s(updated.task_token),
                    ":rung": n(updated.rung),
                    ":deadline": s(iso_z(updated.deadline)),
                    ":document": s(updated.model_dump_json()),
                    ":waiting": s("WAITING"),
                },
            )
        except Exception as exc:
            if is_conditional_failure(exc):
                raise ValueError("only a WAITING engagement can prepare a task-token wait") from exc
            raise
        return updated

    def register_outbound(self, provider_msg_id: str, engagement: Engagement) -> None:
        message_id = required_text(provider_msg_id, "provider_msg_id")
        current = self.get(engagement.tenant_id, engagement.incident_id, engagement.engagement_id)
        if current is None or current.status != "WAITING":
            raise ValueError("outbound correlation requires a WAITING engagement")
        if current.task_token != engagement.task_token:
            raise ValueError("outbound correlation must use the current task token")
        existing = self._correlation(message_id)
        if existing is not None:
            if _same_engagement(existing, current):
                self._note_routing_row(current, outmsg_pk(message_id), _engagement_sk(current))
                return
            raise ValueError(f"provider message {message_id!r} is already correlated")
        expires = self._clock() + CORRELATION_TTL
        item = {
            **key(
                outmsg_pk(message_id),
                f"ENG#{current.tenant_id}#{current.incident_id}#{current.engagement_id}",
            ),
            "status": s(current.status),
            "expires_at": n(int(expires.timestamp())),
            "document": s(current.model_dump_json()),
        }
        try:
            self._client.put_item(
                TableName=self._routing,
                Item=item,
                ConditionExpression="attribute_not_exists(#sk)",
                ExpressionAttributeNames={"#sk": "sk"},
            )
        except Exception as exc:
            if not is_conditional_failure(exc):
                raise
            found = self._correlation(message_id)
            if found is not None and _same_engagement(found, current):
                self._note_routing_row(current, outmsg_pk(message_id), _engagement_sk(current))
                return
            raise ValueError(f"provider message {message_id!r} is already correlated") from exc
        self._note_routing_row(current, outmsg_pk(message_id), _engagement_sk(current))

    def register_address(self, channel: str, address: str, engagement: Engagement) -> None:
        current = _waiting(self, engagement)
        normalized_channel, normalized_address = _address_key(channel, address)
        self._client.put_item(
            TableName=self._routing,
            Item={
                **key(
                    addr_pk(normalized_channel, normalized_address),
                    f"ENG#{current.tenant_id}#{current.incident_id}#{current.engagement_id}",
                ),
                "tenant_id": s(current.tenant_id),
                "incident_id": s(current.incident_id),
                "engagement_id": s(current.engagement_id),
                "contact_id": s(current.contact_id),
                "status": s("WAITING"),
                "channel": s(normalized_channel),
                "address": s(normalized_address),
            },
        )
        self._note_routing_row(
            current,
            addr_pk(normalized_channel, normalized_address),
            f"ENG#{current.tenant_id}#{current.incident_id}#{current.engagement_id}",
        )

    def for_address(self, channel: str, address: str) -> list[Engagement]:
        normalized_channel, normalized_address = _address_key(channel, address)
        response = self._client.query(
            TableName=self._routing,
            KeyConditionExpression="#pk = :pk AND begins_with(#sk, :prefix)",
            ExpressionAttributeNames={"#pk": "pk", "#sk": "sk"},
            ExpressionAttributeValues={
                ":pk": s(addr_pk(normalized_channel, normalized_address)),
                ":prefix": s("ENG#"),
            },
        )
        found: list[Engagement] = []
        for item in response.get("Items", []):
            if text(item.get("status", {"S": ""})) != "WAITING":
                continue
            current = self.get(
                text(item["tenant_id"]), text(item["incident_id"]), text(item["engagement_id"])
            )
            if current is not None and current.status == "WAITING":
                found.append(current)
        return found

    def correlate(self, provider_msg_id: str) -> Engagement | None:
        message_id = required_text(provider_msg_id, "provider_msg_id")
        response = self._client.query(
            TableName=self._routing,
            KeyConditionExpression="#pk = :pk AND begins_with(#sk, :prefix)",
            ExpressionAttributeNames={"#pk": "pk", "#sk": "sk"},
            ExpressionAttributeValues={":pk": s(outmsg_pk(message_id)), ":prefix": s("ENG#")},
        )
        now = int(self._clock().timestamp())
        for item in response.get("Items", []):
            if text(item["status"]) != "WAITING" or number(item["expires_at"]) <= now:
                continue
            engagement = Engagement.model_validate_json(text(item["document"]))
            current = self.get(
                engagement.tenant_id, engagement.incident_id, engagement.engagement_id
            )
            if current is not None and current.status == "WAITING":
                return current
        return None

    def get(self, tenant_id: str, incident_id: str, engagement_id: str) -> Engagement | None:
        context = TenantContext(tenant_id=tenant_id)
        loaded = self._client.get_item(
            TableName=self._incident,
            Key=key(
                incident_pk(context.tenant_id, required_text(incident_id, "incident_id")),
                f"ENG#{required_text(engagement_id, 'engagement_id')}",
            ),
        )
        item = loaded.get("Item")
        if not item:
            return None
        return Engagement.model_validate_json(text(item["document"]))

    def stop(self, tenant_id: str, incident_id: str, engagement_id: str) -> Engagement | None:
        return self._set_status(tenant_id, incident_id, engagement_id, "STOPPED", ACTIVE_STATUSES)

    def answer(self, tenant_id: str, incident_id: str, engagement_id: str) -> Engagement:
        updated = self._set_status(
            tenant_id, incident_id, engagement_id, "ANSWERED", frozenset({"WAITING"})
        )
        if updated is None or updated.status != "ANSWERED":
            raise ValueError("only a WAITING engagement can be answered")
        return updated

    def for_contact(self, tenant_id: str, contact_id: str) -> list[Engagement]:
        context = TenantContext(tenant_id=tenant_id)
        contact = required_text(contact_id, "contact_id")
        response = self._client.query(
            TableName=self._routing,
            KeyConditionExpression="#pk = :pk AND begins_with(#sk, :prefix)",
            ExpressionAttributeNames={"#pk": "pk", "#sk": "sk"},
            ExpressionAttributeValues={
                ":pk": s(f"CONTACTENG#{context.tenant_id}#{contact}"),
                ":prefix": s("ENG#"),
            },
        )
        found: list[Engagement] = []
        for item in response.get("Items", []):
            engagement = self.get(
                context.tenant_id, text(item["incident_id"]), text(item["engagement_id"])
            )
            if engagement is not None:
                found.append(engagement)
        return found

    def _set_status(
        self,
        tenant_id: str,
        incident_id: str,
        engagement_id: str,
        status: EngagementStatus,
        from_statuses: frozenset[str],
    ) -> Engagement | None:
        current = self.get(tenant_id, incident_id, engagement_id)
        if current is None or current.status == status or current.status not in from_statuses:
            return current
        updated = current.model_copy(update={"status": status})
        names = {"#status": "status", "#document": "document"}
        values = {":document": s(updated.model_dump_json()), ":status": s(status)}
        allowed = " OR ".join(f"#status = :from{index}" for index, _ in enumerate(from_statuses))
        for index, previous in enumerate(sorted(from_statuses)):
            values[f":from{index}"] = s(previous)
        try:
            self._client.update_item(
                TableName=self._incident,
                Key=key(
                    incident_pk(updated.tenant_id, updated.incident_id),
                    f"ENG#{updated.engagement_id}",
                ),
                UpdateExpression="SET #status = :status, #document = :document",
                ConditionExpression=allowed,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except Exception as exc:
            if not is_conditional_failure(exc):
                raise
            return self.get(tenant_id, incident_id, engagement_id)
        self._sync_outbound_status(updated)
        return updated

    def _note_routing_row(self, engagement: Engagement, target_pk: str, target_sk: str) -> None:
        """Remember one routing key under this engagement so status updates stay a query."""

        self._client.put_item(
            TableName=self._routing,
            Item={
                **key(_routing_ref_pk(engagement), target_pk),
                "target_pk": s(target_pk),
                "target_sk": s(target_sk),
            },
        )

    def _sync_outbound_status(self, engagement: Engagement) -> None:
        response = self._client.query(
            TableName=self._routing,
            KeyConditionExpression="#pk = :pk AND begins_with(#sk, :prefix)",
            ExpressionAttributeNames={"#pk": "pk", "#sk": "sk"},
            ExpressionAttributeValues={
                ":pk": s(_routing_ref_pk(engagement)),
                ":prefix": s(""),
            },
        )
        for item in response.get("Items", []):
            target_pk = text(item["target_pk"])
            target_sk = text(item["target_sk"])
            if target_pk.startswith("ADDR#") and engagement.status != "WAITING":
                self._client.delete_item(
                    TableName=self._routing,
                    Key=key(target_pk, target_sk),
                )
                self._client.delete_item(
                    TableName=self._routing,
                    Key={"pk": item["pk"], "sk": item["sk"]},
                )
                continue
            if not target_pk.startswith("OUTMSG#"):
                continue
            self._client.update_item(
                TableName=self._routing,
                Key=key(target_pk, target_sk),
                UpdateExpression="SET #status = :status, #document = :document",
                ExpressionAttributeNames={"#status": "status", "#document": "document"},
                ExpressionAttributeValues={
                    ":status": s(engagement.status),
                    ":document": s(engagement.model_dump_json()),
                },
            )

    def _correlation(self, provider_msg_id: str) -> Engagement | None:
        response = self._client.query(
            TableName=self._routing,
            KeyConditionExpression="#pk = :pk AND begins_with(#sk, :prefix)",
            ExpressionAttributeNames={"#pk": "pk", "#sk": "sk"},
            ExpressionAttributeValues={":pk": s(outmsg_pk(provider_msg_id)), ":prefix": s("ENG#")},
        )
        items = response.get("Items", [])
        if not items:
            return None
        return Engagement.model_validate_json(text(items[0]["document"]))


_ADDRESS_CHANNELS = frozenset({"whatsapp", "sms", "rcs", "email", "voice", "dashboard"})


def _waiting(store: EngagementStore, engagement: Engagement) -> Engagement:
    current = store.get(engagement.tenant_id, engagement.incident_id, engagement.engagement_id)
    if current is None or current.status != "WAITING":
        raise ValueError("address correlation requires a WAITING engagement")
    return current


def _engagement_sk(engagement: Engagement) -> str:
    return f"ENG#{engagement.tenant_id}#{engagement.incident_id}#{engagement.engagement_id}"


def _routing_ref_pk(engagement: Engagement) -> str:
    return (
        f"ENGREF#{engagement.tenant_id}#{engagement.incident_id}#{engagement.engagement_id}"
    )


def _address_key(channel: str, address: str) -> tuple[str, str]:
    normalized = required_text(channel, "channel").strip().lower()
    if normalized not in _ADDRESS_CHANNELS:
        raise ValueError(f"channel {channel!r} cannot be correlated by address")
    value = required_text(address, "address").strip()
    if normalized == "email":
        value = value.lower()
    return normalized, value


def open_engagement(
    store: EngagementStore,
    tenant_id: str,
    incident_id: str,
    engagement_id: str,
    role: str,
    ask: str,
    policy_id: str,
    task_token: str,
    facts: dict[str, Any],
    *,
    contact_id: str,
    rung: int,
    deadline: datetime,
) -> Engagement:
    """Open a WAITING engagement on the given store."""

    return store.open_engagement(
        tenant_id,
        incident_id,
        engagement_id,
        role,
        ask,
        policy_id,
        task_token,
        facts,
        contact_id=contact_id,
        rung=rung,
        deadline=deadline,
    )


def _new_engagement(
    tenant_id: str,
    incident_id: str,
    engagement_id: str,
    role: str,
    ask: str,
    policy_id: str,
    task_token: str,
    facts: dict[str, Any],
    *,
    contact_id: str,
    rung: int,
    deadline: datetime,
) -> Engagement:
    if not isinstance(facts, dict):
        raise ValueError("facts must be an object")
    return Engagement(
        tenant_id=TenantContext(tenant_id=tenant_id).tenant_id,
        incident_id=required_text(incident_id, "incident_id"),
        engagement_id=required_text(engagement_id, "engagement_id"),
        role=required_text(role, "role"),
        ask=required_text(ask, "ask"),
        policy_id=required_text(policy_id, "policy_id"),
        task_token=required_text(task_token, "task_token"),
        facts=dict(facts),
        contact_id=required_text(contact_id, "contact_id"),
        status="WAITING",
        rung=rung,
        deadline=deadline,
    )


def _engagement_item(pk: str, sk: str, engagement: Engagement) -> dict[str, dict[str, str]]:
    return {
        **key(pk, sk),
        "tenant_id": s(engagement.tenant_id),
        "status": s(engagement.status),
        "document": s(engagement.model_dump_json()),
        "rung": n(engagement.rung),
        "deadline": s(iso_z(engagement.deadline)),
        "task_token": s(engagement.task_token),
    }


def _same_engagement(left: Engagement, right: Engagement) -> bool:
    return (
        left.tenant_id == right.tenant_id
        and left.incident_id == right.incident_id
        and left.engagement_id == right.engagement_id
    )
