"""DynamoDB tenant configuration. Schema and defaults stay in packaged JSON.

The client is injected. Active config is `CONFIG#{domain}#ACTIVE`. A missing
pointer falls back to a `v1` row so an existing seed item remains readable.
"""

from __future__ import annotations

import json
from typing import Any

from oerf.platform.storage.dynamo import key, required_text, s, text
from oerf.platform.tenancy.config_resolver import FileConfigRepository
from oerf.platform.tenancy.context import TenantContext

Item = dict[str, dict[str, str]]
_SKIP = {"pk", "sk", "tenant_id", "published_by", "published_at", "previous_version"}


class DynamoConfigRepository:
    """`ConfigRepository` backed by the tenant table plus packaged domain files."""

    def __init__(
        self,
        client: Any,
        table_name: str,
        *,
        files: FileConfigRepository | None = None,
    ) -> None:
        self._client = client
        self._table = table_name
        self._files = files or FileConfigRepository()

    def register_tenant(self, tenant_id: str, domains: list[str]) -> None:
        context = TenantContext(tenant_id=tenant_id)
        self._client.put_item(
            TableName=self._table,
            Item={
                **key(f"TENANT#{context.tenant_id}", "META"),
                "tenant_id": s(context.tenant_id),
                "domains": s(json.dumps(domains)),
            },
        )

    def list_tenant_ids(self) -> set[str]:
        response = self._client.scan(TableName=self._table)
        return {
            text(item["tenant_id"])
            for item in response.get("Items", [])
            if item.get("sk", {}).get("S") == "META" and "tenant_id" in item
        }

    def subscribed_domains(self, tenant_id: str) -> set[str]:
        context = TenantContext(tenant_id=tenant_id)
        item = self._load(f"TENANT#{context.tenant_id}", "META")
        if item is None:
            return set()
        domains = json.loads(text(item["domains"]))
        return {str(domain) for domain in domains}

    def read_defaults(self, domain: str) -> dict[str, Any]:
        return self._files.read_defaults(domain)

    def read_schema(self, domain: str) -> dict[str, Any]:
        return self._files.read_schema(domain)

    def read_tenant_config(self, tenant_id: str, domain: str) -> dict[str, Any]:
        context = TenantContext(tenant_id=tenant_id)
        domain_name = required_text(domain, "domain")
        pk = f"TENANT#{context.tenant_id}"
        active = self._load(pk, f"CONFIG#{domain_name}#ACTIVE")
        if active is not None:
            version = int(active["version"]["N"])
            stored = self._load(pk, f"CONFIG#{domain_name}#v{version}")
            return _document(stored) if stored else {}
        seeded = self._load(pk, f"CONFIG#{domain_name}#v1")
        return _document(seeded) if seeded else {}

    def read_version(self, tenant_id: str, domain: str, version: int) -> dict[str, Any] | None:
        context = TenantContext(tenant_id=tenant_id)
        item = self._load(
            f"TENANT#{context.tenant_id}",
            f"CONFIG#{required_text(domain, 'domain')}#v{version}",
        )
        if item is None:
            return None
        return _document(item)

    def _load(self, pk: str, sk: str) -> Item | None:
        loaded = self._client.get_item(TableName=self._table, Key=key(pk, sk))
        item = loaded.get("Item")
        return item if item else None


def _document(item: Item) -> dict[str, Any]:
    if "document" in item:
        parsed = json.loads(text(item["document"]))
        return parsed if isinstance(parsed, dict) else {}
    document: dict[str, Any] = {}
    for name, attribute in item.items():
        if name in _SKIP:
            continue
        if "N" in attribute:
            raw = attribute["N"]
            document[name] = int(raw) if "." not in raw else float(raw)
            continue
        raw_text = attribute["S"]
        if raw_text.startswith("{") or raw_text.startswith("["):
            document[name] = json.loads(raw_text)
        else:
            document[name] = raw_text
    return document
