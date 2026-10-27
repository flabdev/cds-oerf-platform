"""Dashboard REST contract.

Incident reads come from the query port OERF-208 implements. Actions are
enqueued. This module does not change incident state and does not hold a
task token.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from oerf.platform.api.claims import (
    DEFAULT_ROLE,
    GROUPS_CLAIM,
    PLATFORM_ADMIN_GROUP,
    TENANT_CLAIM,
    parse_cognito_groups,
    resolve_role,
)
from oerf.platform.tenancy.config_resolver import ConfigInvalid, FileConfigRepository
from oerf.platform.tenancy.dynamo_config import DynamoConfigRepository
from oerf.platform.tenancy.publisher import ConfigPublisher

_ACTIONS = frozenset({"confirm_transfer", "pick_option", "cancel"})
_ACTION_FIELDS = frozenset({"action", "action_id", "engagement_id", "option_id", "reason"})
_ACTION_ROLES = frozenset({"platform_admin", "tenant_admin", "ops_user"})
_NEEDS_ENGAGEMENT = frozenset({"confirm_transfer", "pick_option"})
_CATALOGS = frozenset({"contacts", "facilities", "faultcodes"})
_ADMIN_ROLES = frozenset({PLATFORM_ADMIN_GROUP, "tenant_admin"})


class IncidentQuery(Protocol):
    def list_incidents(
        self,
        tenant_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return one page of tenant-scoped incident summaries."""

    def get_incident(self, tenant_id: str, incident_id: str) -> dict[str, Any] | None:
        """Return one incident detail, or None when this tenant has no such row."""


class ActionQueue(Protocol):
    def send(self, *, body: dict[str, Any], dedup_id: str) -> bool:
        """Enqueue one action. A repeated dedup id does not add a second message."""


class CatalogStore(Protocol):
    def get(self, tenant_id: str, kind: str) -> dict[str, Any]:
        """Return the stored catalog document, or an empty document."""

    def put(self, tenant_id: str, kind: str, document: dict[str, Any]) -> None:
        """Replace the catalog document for one tenant."""


class AuditLog(Protocol):
    def record(self, event: dict[str, Any]) -> None: ...


@dataclass
class Principal:
    tenant_id: str | None
    role: str
    sub: str | None
    authenticated: bool


@dataclass
class DashboardServices:
    queries: IncidentQuery
    repository: DynamoConfigRepository | None = None
    publisher: ConfigPublisher | None = None
    catalog: CatalogStore | None = None
    queue: ActionQueue | None = None
    audits: AuditLog | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    cadence_schema: dict[str, Any] | None = None


class MemoryIncidentQuery:
    def __init__(self, incidents: list[dict[str, Any]]) -> None:
        self._incidents = incidents

    def list_incidents(
        self,
        tenant_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        rows = [item for item in self._incidents if item.get("tenant_id") == tenant_id]
        rows.sort(key=lambda item: str(item.get("incident_id")))
        start = 0
        if cursor:
            start = next(
                (
                    index + 1
                    for index, item in enumerate(rows)
                    if item.get("incident_id") == cursor
                ),
                len(rows),
            )
        size = min(max(limit, 1), 50)
        page = rows[start : start + size]
        next_cursor = page[-1]["incident_id"] if start + size < len(rows) and page else None
        return {"incidents": page, "next_cursor": next_cursor}

    def get_incident(self, tenant_id: str, incident_id: str) -> dict[str, Any] | None:
        for item in self._incidents:
            if item.get("tenant_id") == tenant_id and item.get("incident_id") == incident_id:
                return item
        return None


class MemoryActionQueue:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self._seen: set[str] = set()

    def send(self, *, body: dict[str, Any], dedup_id: str) -> bool:
        fresh = dedup_id not in self._seen
        self._seen.add(dedup_id)
        if fresh:
            self.messages.append(body)
        return fresh


class MemoryCatalog:
    def __init__(self) -> None:
        self.documents: dict[tuple[str, str], dict[str, Any]] = {}

    def get(self, tenant_id: str, kind: str) -> dict[str, Any]:
        return dict(self.documents.get((tenant_id, kind), {}))

    def put(self, tenant_id: str, kind: str, document: dict[str, Any]) -> None:
        self.documents[(tenant_id, kind)] = dict(document)


class MemoryAuditLog:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, event: dict[str, Any]) -> None:
        self.events.append(dict(event))


def principal_from_event(event: dict[str, Any]) -> Principal:
    """Read Cognito claims. A tenant in the URL is not a claim."""

    authorizer = event.get("requestContext", {}).get("authorizer", {})
    claims = authorizer.get("claims") or authorizer.get("jwt", {}).get("claims") or {}
    if not claims and authorizer.get("tenant_id"):
        claims = authorizer
    if not claims:
        return Principal(None, DEFAULT_ROLE, None, False)
    groups = parse_cognito_groups(claims.get(GROUPS_CLAIM))
    tenant = claims.get(TENANT_CLAIM) or claims.get("tenant_id")
    return Principal(
        tenant_id=str(tenant).strip() if tenant else None,
        role=resolve_role(groups),
        sub=claims.get("sub"),
        authenticated=True,
    )


def dispatch(
    method: str,
    path: str,
    principal: Principal,
    body: dict[str, Any] | None,
    services: DashboardServices,
    *,
    idempotency_key: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Route one authorized dashboard call. Returns the status and JSON body."""

    if not principal.authenticated:
        return 401, {"error": "unauthorized"}
    payload = body or {}
    if _conflicts(principal, payload.get("tenant_id")):
        return 403, {"error": "tenant_mismatch"}

    incident_id = _match(r"^/incidents/([^/]+)$", path)
    action_id = _match(r"^/incidents/([^/]+)/actions$", path)
    if method == "GET" and path == "/incidents":
        return _list(principal, services, limit=limit, cursor=cursor)
    if method == "GET" and incident_id and "/actions" not in path:
        return _detail(principal, incident_id, services)
    if method == "POST" and action_id:
        return _action(principal, action_id, payload, services, idempotency_key)
    if path.startswith("/admin/"):
        return _admin(method, path, principal, payload, services)
    return 404, {"error": "not_found"}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """API Gateway proxy entry. Services are built from the environment."""

    principal = principal_from_event(event)
    raw = event.get("body") or "{}"
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        parsed = {}
    headers = {str(key).lower(): value for key, value in (event.get("headers") or {}).items()}
    query = event.get("queryStringParameters") or {}
    status, body = dispatch(
        str(event.get("httpMethod") or "GET"),
        str(event.get("path") or "/"),
        principal,
        parsed if isinstance(parsed, dict) else {},
        services_from_env(),
        idempotency_key=headers.get("idempotency-key"),
        limit=_limit(query.get("limit")),
        cursor=query.get("cursor"),
    )
    return {
        "statusCode": status,
        "headers": _response_headers(),
        "body": json.dumps(body),
    }


def _response_headers() -> dict[str, str]:
    """CORS for the dashboard origin. A missing origin leaves the header off."""

    headers = {"Content-Type": "application/json"}
    origin = os.environ.get("DASHBOARD_ORIGIN", "").strip()
    if not origin or origin == "*":
        return headers
    headers["Access-Control-Allow-Origin"] = origin
    headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization,Idempotency-Key"
    headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,OPTIONS"
    return headers


def services_from_env() -> DashboardServices:
    """Construct AWS-backed services when the lambda environment is configured."""

    import os

    import boto3

    from oerf.platform.api.queries import DynamoIncidentQuery, SqsActionQueue

    table = os.environ.get("INCIDENT_TABLE_NAME", "").strip()
    queue_url = (
        os.environ.get("INBOUND_QUEUE_URL", "").strip()
        or os.environ.get("ACTION_QUEUE_URL", "").strip()
    )
    if table:
        queries: IncidentQuery = DynamoIncidentQuery(boto3.client("dynamodb"), table)
    else:
        queries = MemoryIncidentQuery([])
    queue = SqsActionQueue(boto3.client("sqs"), queue_url) if queue_url else None
    return DashboardServices(queries=queries, queue=queue)


def _list(
    principal: Principal,
    services: DashboardServices,
    *,
    limit: int,
    cursor: str | None,
) -> tuple[int, dict[str, Any]]:
    denied = _need_tenant(principal)
    if denied:
        return denied
    page = services.queries.list_incidents(principal.tenant_id or "", limit=limit, cursor=cursor)
    rows = [_summary_row(item) for item in page["incidents"]]
    return 200, {"incidents": rows, "next_cursor": page.get("next_cursor")}


def _detail(
    principal: Principal, incident_id: str, services: DashboardServices
) -> tuple[int, dict[str, Any]]:
    denied = _need_tenant(principal)
    if denied:
        return denied
    item = services.queries.get_incident(principal.tenant_id or "", incident_id)
    if item is None:
        return 404, {"error": "not_found"}
    return 200, {
        "incident_id": item.get("incident_id"),
        "tenant_id": item.get("tenant_id"),
        "shipment_id": item.get("shipment_id"),
        "state": item.get("state"),
        "reason": item.get("reason") or "",
        "opened_at": item.get("opened_at") or "",
        "config_version": item.get("config_version"),
        "config_snapshot": item.get("config_snapshot"),
    }


def _action(
    principal: Principal,
    incident_id: str,
    body: dict[str, Any],
    services: DashboardServices,
    idempotency_key: str | None,
) -> tuple[int, dict[str, Any]]:
    denied = _need_tenant(principal)
    if denied:
        return denied
    if principal.role not in _ACTION_ROLES:
        return 403, {"error": "forbidden"}
    parsed = _action_body(body)
    if parsed and idempotency_key and idempotency_key != parsed["action_id"]:
        return 400, {"error": "invalid_body"}
    if parsed is None:
        return 400, {"error": "invalid_body"}
    if services.queue is None:
        return 503, {"error": "queue_unavailable"}
    action_id = parsed["action_id"]
    message = {
        "tenant_id": principal.tenant_id,
        "incident_id": incident_id,
        "engagement_id": parsed["engagement_id"],
        "channel": "dashboard",
        "provider_msg_id": None,
        "context_msg_id": None,
        "action_id": action_id,
        "action": parsed["action"],
        "option_id": parsed["option_id"],
        "user_id": principal.sub,
        "role": principal.role,
        "type": "text",
        "text": None,
        "media_ref": None,
        "received_at": services.clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        services.queue.send(body=message, dedup_id=action_id)
    except Exception:
        return 503, {"error": "queue_unavailable"}
    return 202, {"action_id": action_id, "status": "queued"}


def _admin(
    method: str,
    path: str,
    principal: Principal,
    body: dict[str, Any],
    services: DashboardServices,
) -> tuple[int, dict[str, Any]]:
    if path == "/admin/tenants" and method == "GET":
        if principal.role != PLATFORM_ADMIN_GROUP:
            return 403, {"error": "forbidden"}
        if services.repository is None:
            return 200, {"tenants": []}
        return 200, {"tenants": sorted(services.repository.list_tenant_ids())}
    if path == "/admin/tenants" and method == "POST":
        if principal.role != PLATFORM_ADMIN_GROUP or services.repository is None:
            return 403, {"error": "forbidden"}
        tenant_id = str(body.get("tenant_id") or "").strip()
        domains = body.get("domains")
        if not tenant_id or not isinstance(domains, list) or not domains:
            return 400, {"error": "invalid_tenant"}
        services.repository.register_tenant(tenant_id, [str(item) for item in domains])
        _audit(services, principal, tenant_id, "register_tenant")
        return 201, {"tenant_id": tenant_id}

    tenant_id = _match(r"/admin/tenants/([^/]+)(?:/.*)?", path)
    if tenant_id is None:
        return 404, {"error": "not_found"}
    scoped = _admin_tenant(principal, tenant_id)
    if scoped is None:
        return 403, {"error": "forbidden"}

    catalog = _match(r"^/admin/tenants/[^/]+/(contacts|facilities|faultcodes)$", path)
    if catalog:
        return _catalog(method, scoped, catalog, body, services, principal)
    domain = _match(r"^/admin/tenants/[^/]+/domains/([^/]+)/config$", path)
    if domain and method == "GET":
        return _published(scoped, domain, services)
    if domain and method == "PUT":
        return _draft(scoped, domain, body, services, principal)
    if path.endswith("/config/validate") and method == "POST":
        domain_name = _match(r"^/admin/tenants/[^/]+/domains/([^/]+)/config/validate$", path)
        return _validate(scoped, domain_name, services)
    if path.endswith("/config/publish") and method == "POST":
        domain_name = _match(r"^/admin/tenants/[^/]+/domains/([^/]+)/config/publish$", path)
        return _publish(scoped, domain_name, principal, services)
    cadence_domain = _match(r"^/admin/tenants/[^/]+/domains/([^/]+)/cadence$", path)
    if cadence_domain:
        return _cadence(method, scoped, cadence_domain, body, services, principal)
    return 404, {"error": "not_found"}


def _catalog(
    method: str,
    tenant_id: str,
    kind: str,
    body: dict[str, Any],
    services: DashboardServices,
    principal: Principal,
) -> tuple[int, dict[str, Any]]:
    if kind not in _CATALOGS or services.catalog is None:
        return 404, {"error": "not_found"}
    if method == "GET":
        return 200, services.catalog.get(tenant_id, kind)
    if method != "PUT":
        return 404, {"error": "not_found"}
    if not isinstance(body, dict) or not body:
        return 400, {"error": "invalid_document"}
    services.catalog.put(tenant_id, kind, body)
    _audit(services, principal, tenant_id, f"put_{kind}")
    return 200, {"saved": kind}


def _published(
    tenant_id: str, domain: str, services: DashboardServices
) -> tuple[int, dict[str, Any]]:
    if services.repository is None:
        return 404, {"error": "not_found"}
    return 200, services.repository.read_tenant_config(tenant_id, domain)


def _draft(
    tenant_id: str,
    domain: str,
    body: dict[str, Any],
    services: DashboardServices,
    principal: Principal,
) -> tuple[int, dict[str, Any]]:
    if services.publisher is None:
        return 503, {"error": "publisher_unavailable"}
    services.publisher.save_draft(tenant_id, domain, body)
    _audit(services, principal, tenant_id, "save_draft")
    return 200, {"saved": "draft"}


def _validate(
    tenant_id: str, domain: str | None, services: DashboardServices
) -> tuple[int, dict[str, Any]]:
    if domain is None or services.publisher is None:
        return 404, {"error": "not_found"}
    issues = services.publisher.validate_draft(tenant_id, domain)
    return 200, {"issues": [issue.model_dump() for issue in issues]}


def _publish(
    tenant_id: str,
    domain: str | None,
    principal: Principal,
    services: DashboardServices,
) -> tuple[int, dict[str, Any]]:
    if domain is None or services.publisher is None:
        return 404, {"error": "not_found"}
    author = principal.sub or principal.role
    try:
        published = services.publisher.publish(tenant_id, domain, published_by=author)
    except ConfigInvalid as exc:
        return 400, {"error": "invalid_config", "message": str(exc)}
    _audit(services, principal, tenant_id, "publish")
    return 200, published.model_dump()


def _cadence(
    method: str,
    tenant_id: str,
    domain: str,
    body: dict[str, Any],
    services: DashboardServices,
    principal: Principal,
) -> tuple[int, dict[str, Any]]:
    if services.catalog is None:
        return 503, {"error": "catalog_unavailable"}
    kind = f"cadence:{domain}"
    if method == "GET":
        return 200, services.catalog.get(tenant_id, kind)
    if method != "PUT":
        return 404, {"error": "not_found"}
    schema = services.cadence_schema or _cadence_schema()
    errors = list(Draft202012Validator(schema).iter_errors(body))
    if errors:
        return 400, {"error": "invalid_cadence", "message": errors[0].message}
    services.catalog.put(tenant_id, kind, body)
    _audit(services, principal, tenant_id, "put_cadence")
    return 200, {"saved": "cadence"}


def _cadence_schema() -> dict[str, Any]:
    files = FileConfigRepository()
    path = files.domains_dir / "cold_chain_excursion" / "cadence_schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _audit(
    services: DashboardServices, principal: Principal, tenant_id: str, operation: str
) -> None:
    if services.audits is None:
        return
    services.audits.record(
        {
            "actor": principal.sub,
            "role": principal.role,
            "tenant_id": tenant_id,
            "operation": operation,
            "at": services.clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )


def _summary_row(item: dict[str, Any]) -> dict[str, Any]:
    state = item.get("state")
    version = item.get("version")
    return {
        "incident_id": item.get("incident_id"),
        "state": state,
        "stage": item.get("stage") or state,
        "version": 1 if version is None else int(version),
    }


def _action_body(body: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(body, dict) or set(body) - _ACTION_FIELDS:
        return None
    action = body.get("action")
    action_id = body.get("action_id")
    if action not in _ACTIONS or not isinstance(action_id, str) or not action_id.strip():
        return None
    engagement = body.get("engagement_id")
    if action in _NEEDS_ENGAGEMENT and not _text(engagement):
        return None
    option = body.get("option_id")
    if action == "pick_option" and not _text(option):
        return None
    if option is not None and not isinstance(option, str):
        return None
    if "reason" in body and not isinstance(body.get("reason"), str):
        return None
    return {
        "action": action,
        "action_id": action_id.strip(),
        "engagement_id": engagement.strip() if _text(engagement) else None,
        "option_id": option.strip() if _text(option) else None,
    }


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _limit(raw: Any) -> int:
    try:
        return min(max(int(raw), 1), 50)
    except (TypeError, ValueError):
        return 50


def _conflicts(principal: Principal, requested: Any) -> bool:
    if not isinstance(requested, str) or not requested.strip():
        return False
    if principal.role == PLATFORM_ADMIN_GROUP:
        return False
    return requested.strip() != principal.tenant_id


def _need_tenant(principal: Principal) -> tuple[int, dict[str, Any]] | None:
    if not principal.tenant_id:
        return 403, {"error": "tenant_required"}
    return None


def _admin_tenant(principal: Principal, tenant_id: str) -> str | None:
    if principal.role not in _ADMIN_ROLES:
        return None
    if principal.role != PLATFORM_ADMIN_GROUP and principal.tenant_id != tenant_id:
        return None
    return tenant_id


def _match(pattern: str, path: str) -> str | None:
    found = re.fullmatch(pattern, path)
    if found is None:
        return None
    return found.group(1)
