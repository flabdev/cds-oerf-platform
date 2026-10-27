"""Write the tenant rows ingest and assess read. Incidents stay unseeded.

Operator tool. Without ``--yes`` the command reads the tenant table and prints
how many rows would change. ``--yes`` writes. A second run writes nothing when
the stored numbers already match the seed files.

    python -m scripts.seed_runtime --profile Fission-OERF --tenant t1
    python -m scripts.seed_runtime --profile Fission-OERF --tenant t1 --yes

This writes tenant META, the seed shipment, the two facilities, the contact
rows, and publishes the cold-chain config when no config is stored or
``PP-2-8-45`` is not already 45 minutes. Contact phones come from
``SYNTHETIC_E164`` (the fictional 555-01xx range). It does not read SSM, write
a real number, write fault codes or incidents, or publish readings.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from typing import Any

from oerf.platform.agent.context import DOMAIN
from oerf.platform.storage.dynamo import key, s
from oerf.platform.tenancy.dynamo_config import DynamoConfigRepository
from oerf.platform.tenancy.publisher import ConfigPublisher
from sim.seed.load import load_seed

REGION = "us-east-1"
STACK = "OerfDataStack"
DEMO_TENANTS = ("t1", "t2", "sbx")
PROFILE_ID = "PP-2-8-45"
#: Fictional NANP numbers. 555-0100 through 555-0199 are reserved for fiction.
#: Mocked channels read these from the contact registry. Live channels read
#: SSM ``/oerf/demo-numbers/{tenant}/{role}`` and never this map.
SYNTHETIC_E164 = {
    "driver": "+15550100001",
    "ops": "+15550100002",
    "ops_manager": "+15550100002",
    "facility": "+15550100003",
    "qa": "+15550100004",
    "fleet_supervisor": "+15550100005",
    "ops_director": "+15550100006",
    "qa_lead": "+15550100007",
}
_KINDS = ("meta", "shipments", "facilities", "contacts", "config")
_SKIP = frozenset({"PK", "SK", "pk", "sk"})
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


def tenants_for(tenant: str) -> tuple[str, ...]:
    """One demo tenant. ``all`` is refused so a seed run cannot span tenants."""

    if tenant == "all":
        raise SystemExit("--tenant all is refused")
    if tenant not in DEMO_TENANTS:
        raise SystemExit(f"unknown tenant {tenant!r}")
    return (tenant,)


def seed_runtime(client: Any, table_name: str, tenant_id: str, *, apply: bool) -> dict[str, int]:
    """Put the rows this tenant's seed files define. Returns how many would change."""

    if tenant_id not in DEMO_TENANTS:
        raise ValueError(f"unknown tenant {tenant_id!r}")
    seed = load_seed(tenant_id)
    counts = {
        "meta": _put(client, table_name, _meta_item(seed["tenant"]), apply=apply),
        "shipments": sum(
            _put(client, table_name, _record_item(row), apply=apply)
            for row in seed["shipments"]
        ),
        "facilities": sum(
            _put(client, table_name, _record_item(row), apply=apply)
            for row in seed["facilities"]
        ),
        "contacts": sum(
            _put(client, table_name, _contact_item(row), apply=apply)
            for row in seed["contacts"]
        ),
        "config": _publish_config(client, table_name, tenant_id, seed, apply=apply),
    }
    return counts


def main(argv: list[str] | None = None) -> int:
    """Read the table, then write only when ``--yes`` is set.

    Prints one count line per record kind. A dry run still calls
    ``seed_runtime(apply=False)`` so the counts are the rows that differ.
    """

    args = _parser().parse_args(argv)
    tenant_id = tenants_for(args.tenant)[0]
    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    table_name = _tenant_table(session, args.stack)
    client = session.client("dynamodb", region_name=REGION)
    counts = seed_runtime(client, table_name, tenant_id, apply=args.yes)
    verb = "applied" if args.yes else "would change"
    for name in _KINDS:
        print(f"{verb} {tenant_id} {name} {counts[name]}")
    return 0


def _meta_item(tenant: dict[str, Any]) -> dict[str, Any]:
    tenant_id = str(tenant["tenant_id"])
    return {
        **key(f"TENANT#{tenant_id}", "META"),
        "tenant_id": s(tenant_id),
        "brand_name": s(str(tenant["brand_name"])),
        "timezone": s(str(tenant["timezone"])),
        "domains": s(json.dumps(list(tenant["domains"]))),
        "demo": {"BOOL": bool(tenant["demo"])},
        "clock_multiplier": {"N": str(int(tenant["clock_multiplier"]))},
    }


def _contact_item(row: dict[str, Any]) -> dict[str, Any]:
    """One ``CONTACT#`` row in the shape ``DynamoPhoneRegistry.get_contact`` reads.

    ``role``, ``e164``, and ``opt_in`` are strings. ``opt_in`` is ``"true"`` or
    ``"false"`` because the reader compares that text. The phone is the
    synthetic number for ``party_role``, never the seed token or a literal
    ``+91`` value. An email on the seed becomes ``{role}@example.com``.
    """

    role = str(row["party_role"])
    number = SYNTHETIC_E164.get(role)
    if number is None:
        raise ValueError(f"no synthetic number for role {role!r}")
    tenant_id = str(row["tenant_id"])
    contact_id = str(row["contact_id"])
    item = {
        **key(f"TENANT#{tenant_id}", f"CONTACT#{contact_id}"),
        "tenant_id": s(tenant_id),
        "contact_id": s(contact_id),
        "role": s(role),
        "e164": s(number),
        "opt_in": s("true" if row.get("opt_in") is True else "false"),
    }
    if isinstance(row.get("email"), str) and row["email"].strip():
        item["email"] = s(f"{role}@example.com")
    return item


def _record_item(row: dict[str, Any]) -> dict[str, Any]:
    item = key(str(row["PK"]), str(row["SK"]))
    for name, value in row.items():
        if name in _SKIP or value is None:
            continue
        item[name] = _attribute(value)
    return item


def _attribute(value: Any) -> dict[str, Any]:
    """Encode one seed value the way the low-level readers decode it.

    Shipment lookup understands ``S``, ``N``, ``BOOL``, ``L``, and ``M``.
    Facility lookup reads ``S``, ``N``, and ``BOOL``. Lists and maps stay
    native. ``domains`` is the exception: the tenant META reader
    ``json.loads`` that string, so ``_meta_item`` writes it as ``S``.
    """

    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, int):
        return {"N": str(value)}
    if isinstance(value, float):
        return {"N": format(Decimal(str(value)), "f")}
    if isinstance(value, list):
        return {"L": [_attribute(item) for item in value]}
    if isinstance(value, dict):
        return {"M": {str(name): _attribute(item) for name, item in value.items()}}
    return {"S": str(value)}


def _put(client: Any, table_name: str, item: dict[str, Any], *, apply: bool) -> int:
    current = client.get_item(TableName=table_name, Key={"pk": item["pk"], "sk": item["sk"]}).get(
        "Item"
    )
    if _same_item(current, item):
        return 0
    if apply:
        client.put_item(TableName=table_name, Item=item)
    return 1


def _same_item(current: dict[str, Any] | None, wanted: dict[str, Any]) -> bool:
    """True when every attribute matches, with numbers compared as ``Decimal``."""

    if not isinstance(current, dict) or current.keys() != wanted.keys():
        return False
    return all(_same_attribute(current[name], wanted[name]) for name in wanted)


def _same_attribute(current: Any, wanted: Any) -> bool:
    if not isinstance(current, dict) or not isinstance(wanted, dict):
        return False
    if "N" in current or "N" in wanted:
        return _decimal(current.get("N")) == _decimal(wanted.get("N"))
    if "L" in current or "L" in wanted:
        left = current.get("L")
        right = wanted.get("L")
        if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right):
            return False
        return all(_same_attribute(item, other) for item, other in zip(left, right, strict=True))
    if "M" in current or "M" in wanted:
        return _same_item(current.get("M"), wanted.get("M"))
    return current == wanted


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _publish_config(
    client: Any,
    table_name: str,
    tenant_id: str,
    seed: dict[str, Any],
    *,
    apply: bool,
) -> int:
    document = seed.get("domains/cold_chain_excursion")
    if not isinstance(document, dict):
        return 0
    repository = DynamoConfigRepository(client, table_name)
    stored = repository.read_tenant_config(tenant_id, DOMAIN)
    if _config_matches(stored, document):
        return 0
    if apply:
        draft = {name: value for name, value in document.items() if name not in _DRAFT_SKIP}
        publisher = ConfigPublisher(repository)
        publisher.save_draft(tenant_id, DOMAIN, draft)
        publisher.publish(tenant_id, DOMAIN, published_by="seed_runtime")
    return 1


def _config_matches(stored: Any, document: dict[str, Any]) -> bool:
    """A missing row (``None`` or ``{}``) is not a match, so the first run publishes."""

    if not isinstance(stored, dict) or not stored:
        return False
    return _allowance(stored) == _allowance(document)


def _allowance(document: dict[str, Any]) -> Decimal | None:
    profiles = document.get("product_profiles")
    if not isinstance(profiles, list):
        return None
    for profile in profiles:
        if not isinstance(profile, dict) or profile.get("id") != PROFILE_ID:
            continue
        return _decimal(profile.get("excursion_allowance_min"))
    return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Write one demo tenant's runtime seed rows.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--stack", default=STACK)
    return parser


def _tenant_table(session: Any, stack: str) -> str:
    response = session.client("cloudformation", region_name=REGION).describe_stacks(StackName=stack)
    for item in response["Stacks"][0].get("Outputs") or []:
        if item.get("OutputKey") != "TenantTableName":
            continue
        name = str(item.get("OutputValue") or "").strip()
        if name:
            return name
    raise LookupError("TenantTableName")


if __name__ == "__main__":
    sys.exit(main())
