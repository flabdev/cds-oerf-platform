"""Phone registry. Tenant identity comes from this index, never from message text."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from oerf.platform.storage.dynamo import contact_sk, key, phone_pk, required_text, s, text
from oerf.platform.tenancy.context import TenantContext


class ContactMatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    contact_id: str
    role: str
    opt_in: bool
    e164: str


class SenderResolution(BaseModel):
    """Every registration for a number, or an explicit unknown sender."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    e164: str
    matches: list[ContactMatch]
    unknown: bool


class PhoneRegistry(Protocol):
    def register_contact(
        self,
        tenant_id: str,
        contact_id: str,
        role: str,
        e164: str,
        *,
        opt_in: bool = True,
    ) -> ContactMatch:
        """Store one tenant-scoped contact and its phone index row."""

    def resolve_sender(self, e164: str) -> SenderResolution:
        """Return every tenant/contact/role/opt-in row for this number."""

    def set_opt_in(self, tenant_id: str, contact_id: str, opt_in: bool) -> ContactMatch:
        """Update the contact and the matching phone row."""

    def get_contact(self, tenant_id: str, contact_id: str) -> ContactMatch | None:
        """Return one contact, or None when this tenant has no such contact."""

    def remove_contact(self, tenant_id: str, contact_id: str) -> None:
        """Delete one tenant contact and its phone-index row."""


class MemoryPhoneRegistry:
    def __init__(self) -> None:
        self._contacts: dict[tuple[str, str], ContactMatch] = {}
        self._by_phone: dict[str, list[tuple[str, str]]] = {}

    def register_contact(
        self,
        tenant_id: str,
        contact_id: str,
        role: str,
        e164: str,
        *,
        opt_in: bool = True,
    ) -> ContactMatch:
        match = _match(tenant_id, contact_id, role, e164, opt_in=opt_in)
        previous = self.get_contact(match.tenant_id, match.contact_id)
        self._contacts[(match.tenant_id, match.contact_id)] = match
        ref = (match.tenant_id, match.contact_id)
        if previous is not None and previous.e164 != match.e164:
            old_refs = self._by_phone.get(previous.e164, [])
            self._by_phone[previous.e164] = [item for item in old_refs if item != ref]
        refs = self._by_phone.setdefault(match.e164, [])
        if ref not in refs:
            refs.append(ref)
        return match

    def resolve_sender(self, e164: str) -> SenderResolution:
        number = required_text(e164, "e164")
        matches = [
            self._contacts[ref] for ref in self._by_phone.get(number, []) if ref in self._contacts
        ]
        return SenderResolution(e164=number, matches=matches, unknown=not matches)

    def set_opt_in(self, tenant_id: str, contact_id: str, opt_in: bool) -> ContactMatch:
        current = self.get_contact(tenant_id, contact_id)
        if current is None:
            raise KeyError(f"contact {contact_id!r} is not registered")
        updated = current.model_copy(update={"opt_in": opt_in})
        self._contacts[(updated.tenant_id, updated.contact_id)] = updated
        return updated

    def get_contact(self, tenant_id: str, contact_id: str) -> ContactMatch | None:
        context = TenantContext(tenant_id=tenant_id)
        return self._contacts.get((context.tenant_id, required_text(contact_id, "contact_id")))

    def remove_contact(self, tenant_id: str, contact_id: str) -> None:
        current = self.get_contact(tenant_id, contact_id)
        if current is None:
            return
        ref = (current.tenant_id, current.contact_id)
        self._contacts.pop(ref, None)
        refs = [item for item in self._by_phone.get(current.e164, []) if item != ref]
        if refs:
            self._by_phone[current.e164] = refs
        else:
            self._by_phone.pop(current.e164, None)


class DynamoPhoneRegistry:
    """Contact rows on the tenant table and ``PHONE#{e164}`` rows on the routing table."""

    def __init__(self, client: Any, routing_table: str, tenant_table: str) -> None:
        self._client = client
        self._routing = routing_table
        self._tenant = tenant_table

    def register_contact(
        self,
        tenant_id: str,
        contact_id: str,
        role: str,
        e164: str,
        *,
        opt_in: bool = True,
    ) -> ContactMatch:
        match = _match(tenant_id, contact_id, role, e164, opt_in=opt_in)
        previous = self.get_contact(match.tenant_id, match.contact_id)
        if previous is not None and previous.e164 != match.e164:
            self._client.delete_item(
                TableName=self._routing,
                Key=key(phone_pk(previous.e164), contact_sk(match.tenant_id, match.contact_id)),
            )
        self._client.put_item(TableName=self._tenant, Item=_contact_item(match))
        self._client.put_item(TableName=self._routing, Item=_phone_item(match))
        return match

    def resolve_sender(self, e164: str) -> SenderResolution:
        number = required_text(e164, "e164")
        response = self._client.query(
            TableName=self._routing,
            KeyConditionExpression="#pk = :pk AND begins_with(#sk, :prefix)",
            ExpressionAttributeNames={"#pk": "pk", "#sk": "sk"},
            ExpressionAttributeValues={":pk": s(phone_pk(number)), ":prefix": s("TENANT#")},
        )
        matches = [_match_from_item(item) for item in response.get("Items", [])]
        return SenderResolution(e164=number, matches=matches, unknown=not matches)

    def set_opt_in(self, tenant_id: str, contact_id: str, opt_in: bool) -> ContactMatch:
        current = self.get_contact(tenant_id, contact_id)
        if current is None:
            raise KeyError(f"contact {contact_id!r} is not registered")
        updated = current.model_copy(update={"opt_in": opt_in})
        self._client.put_item(TableName=self._tenant, Item=_contact_item(updated))
        self._client.put_item(TableName=self._routing, Item=_phone_item(updated))
        return updated

    def get_contact(self, tenant_id: str, contact_id: str) -> ContactMatch | None:
        context = TenantContext(tenant_id=tenant_id)
        contact = required_text(contact_id, "contact_id")
        loaded = self._client.get_item(
            TableName=self._tenant,
            Key=key(f"TENANT#{context.tenant_id}", f"CONTACT#{contact}"),
        )
        item = loaded.get("Item")
        if not item:
            return None
        return _match_from_item(item)

    def remove_contact(self, tenant_id: str, contact_id: str) -> None:
        current = self.get_contact(tenant_id, contact_id)
        if current is None:
            return
        self._client.delete_item(
            TableName=self._tenant,
            Key=key(f"TENANT#{current.tenant_id}", f"CONTACT#{current.contact_id}"),
        )
        self._client.delete_item(
            TableName=self._routing,
            Key=key(phone_pk(current.e164), contact_sk(current.tenant_id, current.contact_id)),
        )


def resolve_sender(registry: PhoneRegistry, e164: str) -> SenderResolution:
    """Resolve a sender from the phone registry."""

    return registry.resolve_sender(e164)


def _match(
    tenant_id: str,
    contact_id: str,
    role: str,
    e164: str,
    *,
    opt_in: bool,
) -> ContactMatch:
    return ContactMatch(
        tenant_id=TenantContext(tenant_id=tenant_id).tenant_id,
        contact_id=required_text(contact_id, "contact_id"),
        role=required_text(role, "role"),
        e164=required_text(e164, "e164"),
        opt_in=opt_in,
    )


def _contact_item(match: ContactMatch) -> dict[str, dict[str, str]]:
    return {
        **key(f"TENANT#{match.tenant_id}", f"CONTACT#{match.contact_id}"),
        "tenant_id": s(match.tenant_id),
        "contact_id": s(match.contact_id),
        "role": s(match.role),
        "e164": s(match.e164),
        "opt_in": s("true" if match.opt_in else "false"),
    }


def _phone_item(match: ContactMatch) -> dict[str, dict[str, str]]:
    return {
        **key(phone_pk(match.e164), contact_sk(match.tenant_id, match.contact_id)),
        "tenant_id": s(match.tenant_id),
        "contact_id": s(match.contact_id),
        "role": s(match.role),
        "e164": s(match.e164),
        "opt_in": s("true" if match.opt_in else "false"),
    }


def _match_from_item(item: dict[str, dict[str, str]]) -> ContactMatch:
    return ContactMatch(
        tenant_id=text(item["tenant_id"]),
        contact_id=text(item["contact_id"]),
        role=text(item["role"]),
        e164=text(item["e164"]),
        opt_in=text(item["opt_in"]) == "true",
    )
