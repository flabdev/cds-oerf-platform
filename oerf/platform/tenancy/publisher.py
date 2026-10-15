"""Draft, validate, and publish an immutable tenant config version."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, ValidationError

from oerf.platform.storage.dynamo import is_conditional_failure, iso_z, key, n, s
from oerf.platform.tenancy.config_resolver import (
    ConfigInvalid,
    ResolvedConfig,
    deep_merge,
)
from oerf.platform.tenancy.context import TenantContext
from oerf.platform.tenancy.dynamo_config import DynamoConfigRepository


class ConfigIssue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    message: str


class PublishResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    domain: str
    config_version: int
    status: str
    published_at: str
    published_by: str
    previous_version: int | None


class ConfigPublisher:
    """Publish moves the ACTIVE pointer and writes a new version row."""

    def __init__(
        self,
        repository: DynamoConfigRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(UTC))

    def save_draft(self, tenant_id: str, domain: str, document: dict[str, Any]) -> None:
        context = TenantContext(tenant_id=tenant_id)
        domain_name = _domain(domain)
        self._repository._client.put_item(
            TableName=self._repository._table,
            Item={
                **key(f"TENANT#{context.tenant_id}", f"CONFIG#{domain_name}#DRAFT"),
                "tenant_id": s(context.tenant_id),
                "document": s(json.dumps(document)),
            },
        )

    def validate_draft(self, tenant_id: str, domain: str) -> list[ConfigIssue]:
        context = TenantContext(tenant_id=tenant_id)
        domain_name = _domain(domain)
        draft = self._draft(context.tenant_id, domain_name)
        merged = deep_merge(self._repository.read_defaults(domain_name), draft)
        return _issues(merged, self._repository.read_schema(domain_name), context.tenant_id)

    def publish(self, tenant_id: str, domain: str, *, published_by: str) -> PublishResult:
        context = TenantContext(tenant_id=tenant_id)
        domain_name = _domain(domain)
        author = published_by.strip()
        if not author:
            raise ValueError("published_by must be a non-empty string")
        issues = self.validate_draft(context.tenant_id, domain_name)
        if issues:
            raise ConfigInvalid(
                "; ".join(f"{issue.path}: {issue.message}" for issue in issues)
            )
        draft = self._draft(context.tenant_id, domain_name)
        current = self._active_version(context.tenant_id, domain_name)
        version = current + 1
        published_at = iso_z(self._clock())
        document = {**draft, "domain": domain_name, "version": version, "status": "published"}
        previous = current or None
        version_item = {
            **key(f"TENANT#{context.tenant_id}", f"CONFIG#{domain_name}#v{version}"),
            "tenant_id": s(context.tenant_id),
            "document": s(json.dumps(document)),
            "version": n(version),
            "status": s("published"),
            "published_by": s(author),
            "published_at": s(published_at),
        }
        active_item = {
            **key(f"TENANT#{context.tenant_id}", f"CONFIG#{domain_name}#ACTIVE"),
            "tenant_id": s(context.tenant_id),
            "version": n(version),
            "status": s("published"),
            "published_by": s(author),
            "published_at": s(published_at),
        }
        if previous is not None:
            version_item["previous_version"] = n(previous)
            active_item["previous_version"] = n(previous)
        try:
            self._repository._client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._repository._table,
                            "Item": version_item,
                            "ConditionExpression": "attribute_not_exists(#sk)",
                            "ExpressionAttributeNames": {"#sk": "sk"},
                        }
                    },
                    {"Put": {"TableName": self._repository._table, "Item": active_item}},
                ]
            )
        except Exception as exc:
            if not is_conditional_failure(exc):
                raise
            raise ConfigInvalid(f"config version {version} already exists") from exc
        return PublishResult(
            tenant_id=context.tenant_id,
            domain=domain_name,
            config_version=version,
            status="published",
            published_at=published_at,
            published_by=author,
            previous_version=previous,
        )

    def _draft(self, tenant_id: str, domain: str) -> dict[str, Any]:
        item = self._repository._load(f"TENANT#{tenant_id}", f"CONFIG#{domain}#DRAFT")
        if item is None or "document" not in item:
            return {}
        parsed = json.loads(item["document"]["S"])
        return parsed if isinstance(parsed, dict) else {}

    def _active_version(self, tenant_id: str, domain: str) -> int:
        item = self._repository._load(f"TENANT#{tenant_id}", f"CONFIG#{domain}#ACTIVE")
        if item is None:
            return 0
        return int(item["version"]["N"])


def _domain(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("domain must be a non-empty string")
    return stripped


def _issues(document: dict[str, Any], schema: dict[str, Any], tenant_id: str) -> list[ConfigIssue]:
    try:
        validator = Draft202012Validator(schema)
    except SchemaError as exc:
        return [ConfigIssue(path="$", message=exc.message)]
    issues = [
        ConfigIssue(path=".".join(str(part) for part in error.path) or "$", message=error.message)
        for error in validator.iter_errors(document)
    ]
    if issues:
        return issues
    try:
        ResolvedConfig.model_validate({**document, "tenant_id": tenant_id})
    except ValidationError as exc:
        issues.extend(
            ConfigIssue(path=".".join(str(part) for part in error["loc"]), message=error["msg"])
            for error in exc.errors()
        )
    return issues
