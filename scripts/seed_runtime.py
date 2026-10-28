"""Write the tenant rows ingest and assess read. Incidents stay unseeded.

Operator tool. Without ``--yes`` the command reads the tenant table and prints
how many rows would change. ``--yes`` writes. A second run writes nothing when
the stored numbers already match the seed files.

    python -m scripts.seed_runtime --profile Fission-OERF --tenant t1
    python -m scripts.seed_runtime --profile Fission-OERF --tenant t1 --yes
    python -m scripts.seed_runtime --profile Fission-OERF --tenant t1 --yes --live-contacts
    python -m scripts.seed_runtime --profile Fission-OERF --tenant t1 --yes \
        --live-contacts --roles driver,qa

This writes tenant META, the seed shipment, the two facilities, the contact
rows, the ``PHONE#`` index on the routing table, and publishes the cold-chain
config when no config is stored or ``PP-2-8-45`` is not already 45 minutes.
Contact phones come from ``SYNTHETIC_E164`` (the fictional 555-01xx range).
It does not read SSM, write a real number, write fault codes or incidents, or
publish readings unless ``--live-contacts`` is set. That flag reads
``/oerf/demo-numbers/{tenant}/{role}`` and writes one ``PHONE#`` ownership row
per seeded contact of that role. It prints the role and the count only. A role
with no parameter is skipped. An empty or invalid value stops the command.
``--roles`` limits the read to those seeded roles.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Collection
from decimal import Decimal, InvalidOperation
from typing import Any

from oerf.platform.agent.context import DOMAIN
from oerf.platform.channels.address import demo_number_name
from oerf.platform.storage.dynamo import canonical_e164, contact_sk, key, phone_pk, s
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
_KINDS = ("meta", "shipments", "facilities", "contacts", "phones", "config")
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


def seed_runtime(
    client: Any,
    table_name: str,
    tenant_id: str,
    *,
    apply: bool,
    routing_table: str | None = None,
) -> dict[str, int]:
    """Put the rows this tenant's seed files define. Returns how many would change.

    ``routing_table`` receives one ``PHONE#`` row per contact. Inbound resolves
    the sender from that index. Omit it only when the caller has no routing table.
    """

    if tenant_id not in DEMO_TENANTS:
        raise ValueError(f"unknown tenant {tenant_id!r}")
    seed = load_seed(tenant_id)
    contacts = seed["contacts"]
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
            _put(client, table_name, _contact_item(row), apply=apply) for row in contacts
        ),
        "phones": (
            0
            if routing_table is None
            else sum(
                _put(client, routing_table, _phone_item(row), apply=apply) for row in contacts
            )
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
    chosen = _role_filter(args.roles)
    if chosen is not None and not args.live_contacts:
        raise SystemExit("--roles needs --live-contacts")
    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    table_name, routing_table = _table_names(session, args.stack)
    client = session.client("dynamodb", region_name=REGION)
    counts = seed_runtime(
        client, table_name, tenant_id, apply=args.yes, routing_table=routing_table
    )
    verb = "applied" if args.yes else "would change"
    for name in _KINDS:
        print(f"{verb} {tenant_id} {name} {counts[name]}")
    if args.live_contacts:
        roles = seed_live_contacts(
            client,
            routing_table,
            tenant_id,
            session.client("ssm", region_name=REGION),
            apply=args.yes,
            roles=chosen,
        )
        for role, count in roles.items():
            print(f"{verb} {tenant_id} live-contacts {role} {count}")
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


def _synthetic_contact(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """Tenant, contact, role, canonical E.164, and the ``opt_in`` string."""

    role = str(row["party_role"])
    number = SYNTHETIC_E164.get(role)
    if number is None:
        raise ValueError(f"no synthetic number for role {role!r}")
    opted_in = "true" if row.get("opt_in") is True else "false"
    return (
        str(row["tenant_id"]),
        str(row["contact_id"]),
        role,
        canonical_e164(number),
        opted_in,
    )


def _contact_item(row: dict[str, Any]) -> dict[str, Any]:
    """One ``CONTACT#`` row in the shape ``DynamoPhoneRegistry.get_contact`` reads.

    ``role``, ``e164``, and ``opt_in`` are strings. ``opt_in`` is ``"true"`` or
    ``"false"`` because the reader compares that text. The phone is the
    synthetic number for ``party_role``, never the seed token or a literal
    ``+91`` value. An email on the seed becomes ``{role}@example.com``.
    """

    tenant_id, contact_id, role, number, opted_in = _synthetic_contact(row)
    item = {
        **key(f"TENANT#{tenant_id}", f"CONTACT#{contact_id}"),
        "tenant_id": s(tenant_id),
        "contact_id": s(contact_id),
        "role": s(role),
        "e164": s(number),
        "opt_in": s(opted_in),
    }
    if isinstance(row.get("email"), str) and row["email"].strip():
        item["email"] = s(f"{role}@example.com")
    return item


def _phone_item(row: dict[str, Any]) -> dict[str, Any]:
    """One routing ``PHONE#`` row in the shape ``resolve_sender`` queries."""

    tenant_id, contact_id, role, number, opted_in = _synthetic_contact(row)
    return _ownership_item(tenant_id, contact_id, role, number, opted_in)


def _ownership_item(
    tenant_id: str, contact_id: str, role: str, number: str, opted_in: str
) -> dict[str, Any]:
    """``PHONE#{e164}`` / ``TENANT#{tid}#CONTACT#{id}`` for sender ownership."""

    return {
        **key(phone_pk(number), contact_sk(tenant_id, contact_id)),
        "tenant_id": s(tenant_id),
        "contact_id": s(contact_id),
        "role": s(role),
        "e164": s(canonical_e164(number)),
        "opt_in": s(opted_in),
    }


def seed_live_contacts(
    client: Any,
    routing_table: str,
    tenant_id: str,
    ssm: Any,
    *,
    apply: bool,
    roles: Collection[str] | None = None,
) -> dict[str, int]:
    """Write one ``PHONE#`` row per seeded contact from that role's SSM number.

    The return value is ``role -> row count`` for parameters that exist. It
    does not contain the number. A missing parameter is printed and omitted.
    Contacts that share a role share one SecureString and keep their own
    contact id, which is what inbound ownership compares.
    """

    if tenant_id not in DEMO_TENANTS:
        raise ValueError(f"unknown tenant {tenant_id!r}")
    if not routing_table:
        raise SystemExit("live contacts need the routing table")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in load_seed(tenant_id)["contacts"]:
        grouped.setdefault(str(row["party_role"]), []).append(row)
    if roles is not None:
        for role in roles:
            if role not in grouped:
                raise SystemExit(f"unknown role {role}")
        grouped = {role: grouped[role] for role in roles}
    counts: dict[str, int] = {}
    for role, rows in grouped.items():
        number = _demo_number(ssm, tenant_id, role)
        if number is None:
            print(f"skipped {tenant_id} live-contacts {role} (no parameter)")
            continue
        counts[role] = sum(
            _put(
                client,
                routing_table,
                _ownership_item(
                    str(row["tenant_id"]),
                    str(row["contact_id"]),
                    role,
                    number,
                    "true" if row.get("opt_in") is True else "false",
                ),
                apply=apply,
            )
            for row in rows
        )
    return counts


def _demo_number(ssm: Any, tenant_id: str, role: str) -> str | None:
    """Read one SecureString. A missing parameter returns ``None``.

    An empty or invalid value exits. The value is not included in an error.
    """

    name = demo_number_name(tenant_id, role)
    try:
        found = ssm.get_parameter(Name=name, WithDecryption=True)
    except Exception as exc:
        if _parameter_missing(exc):
            return None
        raise SystemExit(f"could not read {name}") from None
    parameter = found.get("Parameter") if isinstance(found, dict) else None
    raw = parameter.get("Value") if isinstance(parameter, dict) else None
    number = canonical_e164(raw.strip()) if isinstance(raw, str) else ""
    digits = number[1:]
    if not (number.startswith("+") and digits.isdigit() and 8 <= len(digits) <= 15):
        raise SystemExit(f"could not read {name}")
    return number


def _parameter_missing(exc: BaseException) -> bool:
    """True only for SSM ``ParameterNotFound``. Other errors stay fatal."""

    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error")
    return isinstance(error, dict) and error.get("Code") == "ParameterNotFound"


def _role_filter(raw: str | None) -> tuple[str, ...] | None:
    """Comma-separated roles, in the order given. ``None`` means every role."""

    if raw is None:
        return None
    chosen = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    if not chosen:
        raise SystemExit("--roles needs at least one role")
    return chosen


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
    parser.add_argument(
        "--live-contacts",
        action="store_true",
        help="Write PHONE# rows from SSM /oerf/demo-numbers/{tenant}/{role}.",
    )
    parser.add_argument(
        "--roles",
        default=None,
        help="Comma-separated roles for --live-contacts, for example driver,qa.",
    )
    parser.add_argument("--stack", default=STACK)
    return parser


def _table_names(session: Any, stack: str) -> tuple[str, str]:
    response = session.client("cloudformation", region_name=REGION).describe_stacks(StackName=stack)
    outputs = {
        item.get("OutputKey"): str(item.get("OutputValue") or "").strip()
        for item in response["Stacks"][0].get("Outputs") or []
    }
    tenant = outputs.get("TenantTableName") or ""
    routing = outputs.get("RoutingTableName") or ""
    if not tenant or not routing:
        raise LookupError("TenantTableName and RoutingTableName")
    return tenant, routing


if __name__ == "__main__":
    sys.exit(main())
