"""Confirm the demo device and contact seed is on the deployed tables.

Operator tool. It does not write. After a deploy with ``demo:seedDevices=true``
and ``python -m scripts.seed_runtime --yes``, this command should exit 0.

    python -m scripts.verify_seed --profile <profile> --tenant t1
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import boto3

from oerf.platform.channels.devices import verify_demo_devices
from scripts.seed_runtime import REGION, STACK, table_names, tenants_for, verify_opt_in_contacts


def verify_seed(client: Any, tenant_table: str, routing_table: str, tenant_id: str) -> list[str]:
    """Return human-readable gaps. Empty means the seed is present."""

    gaps: list[str] = []
    for device_id in verify_demo_devices(client, routing_table):
        gaps.append(f"device {device_id}")
    for contact_id in verify_opt_in_contacts(client, tenant_table, tenant_id):
        gaps.append(f"contact {contact_id}")
    return gaps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Confirm demo device and contact seed rows.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--stack", default=STACK)
    args = parser.parse_args(argv)
    tenant_id = tenants_for(args.tenant)[0]
    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    tenant_table, routing_table = table_names(session, args.stack)
    client = session.client("dynamodb")
    gaps = verify_seed(client, tenant_table, routing_table, tenant_id)
    if gaps:
        print(f"missing {tenant_id} " + ", ".join(gaps))
        return 1
    print(f"ok {tenant_id} devices and contacts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
