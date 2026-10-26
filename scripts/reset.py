"""Reset one demo tenant so the cold-chain demo can be run again.

Operator tool. Never runs in Lambda or CI. The default is a dry run; ``--yes``
deletes. ``--tenant all`` is refused unless ``--yes-all`` is also set, and that
refusal happens before any AWS call.

    python -m scripts.reset --profile cds-oerf-poc --tenant t1
    python -m scripts.reset --profile cds-oerf-poc --tenant t1 --yes --reseed

``--reseed`` rewrites that tenant's demo devices and restores its cold-chain
allowance when ``PP-2-8-45`` is not 45 minutes. It does not seed the other
tenants, and it does not replace a device row owned by someone else.

The incident-id counter (``TENANT#{tid}`` / ``COUNTER#INCIDENT`` on the tenant
table) is left in place, as are tenant config rows, ``INGEST#`` dedupe rows,
report objects, live websocket rows, and the platform table.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from oerf.platform.agent.context import DOMAIN
from oerf.platform.channels.devices import register_device
from oerf.platform.storage.dynamo import device_pk, key
from oerf.platform.tenancy.dynamo_config import DynamoConfigRepository
from oerf.platform.tenancy.publisher import ConfigPublisher
from sim.seed.load import load_seed

REGION = "us-east-1"
DEMO_TENANTS = ("t1", "t2", "sbx")
STACK = "OerfDataStack"
PROFILE_ID = "PP-2-8-45"
_DRAFT_SKIP = frozenset(
    {
        "tenant_id",
        "PK",
        "SK",
        "pk",
        "sk",
        "version",
        "status",
        "published_by",
        "published_at",
        "previous_version",
        "domain",
    }
)
COUNT_KEYS = (
    "executions",
    "incident_items",
    "shipment_locks",
    "debounce",
    "routing_rows",
    "routing_targets",
    "inbound_messages",
    "evidence_objects",
    "devices",
    "config",
)


@dataclass(frozen=True)
class ResetClients:
    """The tables and buckets one reset is allowed to touch."""

    dynamodb: Any
    s3: Any
    stepfunctions: Any
    incident_table: str
    routing_table: str
    tenant_table: str
    evidence_bucket: str
    cold_chain_arn: str
    engage_party_arn: str


def tenants_for(tenant: str, *, yes_all: bool) -> tuple[str, ...]:
    """The tenants this invocation may touch. ``all`` without ``--yes-all`` refuses."""

    if tenant == "all":
        if not yes_all:
            raise SystemExit("--tenant all requires --yes-all")
        return DEMO_TENANTS
    if tenant not in DEMO_TENANTS:
        raise SystemExit(f"unknown tenant {tenant!r}")
    return (tenant,)


def reset_tenant(
    clients: ResetClients,
    tenant_id: str,
    *,
    apply: bool,
    reseed: bool,
) -> dict[str, int]:
    """Count, and when ``apply`` is set delete, one tenant's demo incident data."""

    if tenant_id not in DEMO_TENANTS:
        raise SystemExit(f"unknown tenant {tenant_id!r}")
    counts = dict.fromkeys(COUNT_KEYS, 0)
    counts["executions"] = _stop_executions(clients, tenant_id, apply=apply)
    counts["incident_items"] = _delete_matching(
        clients.dynamodb,
        clients.incident_table,
        lambda item: _incident_item(tenant_id, item),
        apply=apply,
    )
    counts["shipment_locks"] = _delete_matching(
        clients.dynamodb,
        clients.incident_table,
        lambda item: _lock_item(tenant_id, item),
        apply=apply,
    )
    counts["debounce"] = _delete_matching(
        clients.dynamodb,
        clients.incident_table,
        lambda item: _text(item, "pk").startswith(f"DEBOUNCE#{tenant_id}#"),
        apply=apply,
    )
    routing, targets = _routing(clients, tenant_id, apply=apply)
    counts["routing_rows"] = routing
    counts["routing_targets"] = targets
    counts["inbound_messages"] = _delete_matching(
        clients.dynamodb,
        clients.routing_table,
        lambda item: _inbound(tenant_id, item),
        apply=apply,
    )
    counts["evidence_objects"] = _evidence(clients, tenant_id, apply=apply)
    if reseed:
        counts["devices"] = _reseed_devices(clients, tenant_id, apply=apply)
        counts["config"] = _reseed_config(clients, tenant_id, apply=apply)
    return counts


def main(
    argv: list[str] | None = None,
    *,
    session_factory: Callable[[str], Any] | None = None,
) -> int:
    """Parse, refuse an unconfirmed ``all``, then reset. No AWS client before the refusal."""

    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    tenants = tenants_for(args.tenant, yes_all=args.yes_all)
    factory = session_factory or _session
    session = factory(args.profile)
    clients = clients_from_session(session, args.stack)
    mode = "applied" if args.yes else "dry-run"
    for tenant_id in tenants:
        counts = reset_tenant(clients, tenant_id, apply=args.yes, reseed=args.reseed)
        for name in COUNT_KEYS:
            print(f"{mode} {tenant_id} {name} {counts[name]}")
    return 0


def clients_from_session(session: Any, stack: str) -> ResetClients:
    """Read the data-stack outputs and the clients that use them."""

    outputs = _outputs(session.client("cloudformation", region_name=REGION), stack)
    return ResetClients(
        dynamodb=session.client("dynamodb", region_name=REGION),
        s3=session.client("s3", region_name=REGION),
        stepfunctions=session.client("stepfunctions", region_name=REGION),
        incident_table=outputs["IncidentTableName"],
        routing_table=outputs["RoutingTableName"],
        tenant_table=outputs["TenantTableName"],
        evidence_bucket=outputs["EvidenceBucketName"],
        cold_chain_arn=outputs["ColdChainStateMachineArn"],
        engage_party_arn=outputs["EngagePartyStateMachineArn"],
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reset one demo tenant's incident data.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--yes-all", action="store_true")
    parser.add_argument("--reseed", action="store_true")
    parser.add_argument("--stack", default=STACK)
    return parser


def _session(profile: str) -> Any:
    import boto3

    return boto3.Session(profile_name=profile, region_name=REGION)


def _outputs(cloudformation: Any, stack: str) -> dict[str, str]:
    response = cloudformation.describe_stacks(StackName=stack)
    found = {
        str(item.get("OutputKey")): str(item.get("OutputValue") or "").strip()
        for item in response["Stacks"][0].get("Outputs") or []
    }
    required = (
        "IncidentTableName",
        "RoutingTableName",
        "TenantTableName",
        "EvidenceBucketName",
        "ColdChainStateMachineArn",
        "EngagePartyStateMachineArn",
    )
    missing = [name for name in required if not found.get(name)]
    if missing:
        raise LookupError(", ".join(missing))
    return found


def _stop_executions(clients: ResetClients, tenant_id: str, *, apply: bool) -> int:
    """Stop this tenant's running executions. Engage-party first, then cold-chain."""

    stopped = 0
    for arn in (clients.engage_party_arn, clients.cold_chain_arn):
        for execution in _pages(
            clients.stepfunctions.list_executions,
            stateMachineArn=arn,
            statusFilter="RUNNING",
            item_key="executions",
            token_key="nextToken",
        ):
            described = clients.stepfunctions.describe_execution(
                executionArn=execution["executionArn"]
            )
            if _input_tenant(described.get("input")) != tenant_id:
                continue
            if apply:
                clients.stepfunctions.stop_execution(
                    executionArn=execution["executionArn"],
                    error="Reset",
                    cause="demo_reset",
                )
            stopped += 1
    return stopped


def _routing(clients: ResetClients, tenant_id: str, *, apply: bool) -> tuple[int, int]:
    rows = 0
    targets = 0
    items = list(_scan(clients.dynamodb, clients.routing_table))
    for item in items:
        if _text(item, "sk") == "COUNTER#INCIDENT":
            continue
        if not _routing_row(tenant_id, item):
            continue
        rows += 1
        if _text(item, "pk").startswith(f"ENGREF#{tenant_id}#"):
            targets += _target(clients, tenant_id, item, apply=apply)
        if apply:
            _remove(clients.dynamodb, clients.routing_table, item)
    return rows, targets


def _target(clients: ResetClients, tenant_id: str, item: dict[str, Any], *, apply: bool) -> int:
    target_pk = _text(item, "target_pk")
    target_sk = _text(item, "target_sk")
    if not target_sk.startswith(f"ENG#{tenant_id}#"):
        return 0
    if not (target_pk.startswith("OUTMSG#") or target_pk.startswith("ADDR#")):
        return 0
    if apply:
        clients.dynamodb.delete_item(
            TableName=clients.routing_table,
            Key={"pk": {"S": target_pk}, "sk": {"S": target_sk}},
        )
    return 1


def _evidence(clients: ResetClients, tenant_id: str, *, apply: bool) -> int:
    prefix = f"tenants/{tenant_id}/"
    keys = [
        item["Key"]
        for item in _pages(
            clients.s3.list_objects_v2,
            Bucket=clients.evidence_bucket,
            Prefix=prefix,
            item_key="Contents",
            token_key="NextContinuationToken",
            more_key="IsTruncated",
        )
    ]
    if apply:
        for start in range(0, len(keys), 1000):
            batch = keys[start : start + 1000]
            if not batch:
                continue
            clients.s3.delete_objects(
                Bucket=clients.evidence_bucket,
                Delete={"Objects": [{"Key": name} for name in batch]},
            )
    return len(keys)


def _reseed_devices(clients: ResetClients, tenant_id: str, *, apply: bool) -> int:
    changed = 0
    for row in load_seed(tenant_id).get("shipments") or []:
        device = str(row.get("device") or "").strip()
        if not device:
            continue
        shipment = str(row["shipment_id"])
        asset = str(row.get("truck") or shipment)
        current = clients.dynamodb.get_item(
            TableName=clients.routing_table,
            Key=key(device_pk(device), "META"),
        ).get("Item")
        owner = _text(current, "tenant_id") if current else ""
        if owner and owner != tenant_id:
            continue
        if (
            current
            and owner == tenant_id
            and _text(current, "shipment_id") == shipment
            and _text(current, "asset_id") == asset
        ):
            continue
        changed += 1
        if apply:
            register_device(
                clients.dynamodb,
                clients.routing_table,
                device_id=device,
                tenant_id=tenant_id,
                shipment_id=shipment,
                asset_id=asset,
            )
    return changed


def _reseed_config(clients: ResetClients, tenant_id: str, *, apply: bool) -> int:
    seed = load_seed(tenant_id).get("domains/cold_chain_excursion")
    if not isinstance(seed, dict):
        return 0
    repository = DynamoConfigRepository(clients.dynamodb, clients.tenant_table)
    if _allowance(repository.read_tenant_config(tenant_id, DOMAIN)) == _allowance(seed):
        return 0
    if apply:
        draft = {name: value for name, value in seed.items() if name not in _DRAFT_SKIP}
        publisher = ConfigPublisher(repository)
        publisher.save_draft(tenant_id, DOMAIN, draft)
        publisher.publish(tenant_id, DOMAIN, published_by="demo_reset")
    return 1


def _delete_matching(
    client: Any,
    table: str,
    match: Callable[[dict[str, Any]], bool],
    *,
    apply: bool,
) -> int:
    count = 0
    for item in list(_scan(client, table)):
        if _text(item, "sk") == "COUNTER#INCIDENT" or not match(item):
            continue
        count += 1
        if apply:
            _remove(client, table, item)
    return count


def _incident_item(tenant_id: str, item: dict[str, Any]) -> bool:
    return _text(item, "pk").startswith(f"TENANT#{tenant_id}#INC#")


def _lock_item(tenant_id: str, item: dict[str, Any]) -> bool:
    return _text(item, "pk").startswith(f"TENANT#{tenant_id}#SHIP#") and _text(item, "sk") == (
        "OPEN_INCIDENT"
    )


def _routing_row(tenant_id: str, item: dict[str, Any]) -> bool:
    pk = _text(item, "pk")
    sk = _text(item, "sk")
    if pk.startswith(f"ENGREF#{tenant_id}#") or pk.startswith(f"CONTACTENG#{tenant_id}#"):
        return True
    return pk.startswith(f"TENANT#{tenant_id}#INCIDENT#") and (
        sk.startswith("MSG#") or sk == "ACTIVE_TASK_TOKEN"
    )


def _inbound(tenant_id: str, item: dict[str, Any]) -> bool:
    if not _text(item, "pk").startswith("INMSG#"):
        return False
    if "tenant_id" not in item:
        return False
    return _text(item, "tenant_id") == tenant_id


def _allowance(document: dict[str, Any], profile_id: str = PROFILE_ID) -> int | None:
    profiles = document.get("product_profiles")
    if not isinstance(profiles, list):
        return None
    for profile in profiles:
        if not isinstance(profile, dict) or profile.get("id") != profile_id:
            continue
        value = profile.get("excursion_allowance_min")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value)
    return None


def _input_tenant(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    tenant = parsed.get("tenant_id")
    return tenant if isinstance(tenant, str) else None


def _text(item: dict[str, Any] | None, name: str) -> str:
    if not item:
        return ""
    value = item.get(name)
    if not isinstance(value, dict):
        return ""
    return str(value.get("S") or "")


def _remove(client: Any, table: str, item: dict[str, Any]) -> None:
    client.delete_item(TableName=table, Key={"pk": item["pk"], "sk": item["sk"]})


def _scan(client: Any, table: str) -> Iterator[dict[str, Any]]:
    yield from _pages(client.scan, TableName=table, item_key="Items", token_key="LastEvaluatedKey")


def _pages(
    method: Callable[..., dict[str, Any]],
    *,
    item_key: str,
    token_key: str,
    more_key: str | None = None,
    **kwargs: Any,
) -> Iterator[dict[str, Any]]:
    token: Any = None
    while True:
        request = dict(kwargs)
        if token:
            if token_key == "LastEvaluatedKey":
                request["ExclusiveStartKey"] = token
            else:
                request_key = (
                    "ContinuationToken" if token_key == "NextContinuationToken" else "nextToken"
                )
                request[request_key] = token
        page = method(**request)
        yield from page.get(item_key) or []
        token = page.get(token_key)
        if more_key is not None and not page.get(more_key):
            return
        if not token:
            return


if __name__ == "__main__":
    raise SystemExit(main())
