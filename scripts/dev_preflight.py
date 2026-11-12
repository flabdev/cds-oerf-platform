"""Check the dev stage before a live rehearsal. Read-only.

Prints parameter names and PASS or FAIL. It does not print parameter values,
topic ARNs, or phone numbers.

    python -m scripts.dev_preflight --profile <profile>

``STAGE`` must be ``dev`` (the ``dev-preflight`` target sets it). The shared
inbound topic is a read of main's ``OerfComputeStack`` and is not modified.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from oerf.platform.channels.address import driver_demo_number_name
from scripts._stage import remember_stage, resolve_stage
from scripts.seed_runtime import DEMO_TENANTS, contact_is_live
from sim.seed.load import load_seed


def main(
    argv: list[str] | None = None,
    *,
    session_factory: Any = None,
) -> int:
    parser = argparse.ArgumentParser(description="Read-only checks for the dev stage.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--stage", default=None)
    args = parser.parse_args(argv)
    target = remember_stage(resolve_stage(args.stage))
    if target.name != "dev":
        raise SystemExit("dev-preflight only checks the dev stage")
    target.announce()
    session = (session_factory or _session)(args.profile)
    failed = False
    if not _credentials(session):
        print("FAIL credentials")
        return 1
    print("PASS credentials")
    formed = session.client("cloudformation", region_name="us-east-1")
    for name in target.stacks:
        if _stack_ready(formed, name):
            print(f"PASS stack {name}")
        else:
            print(f"FAIL stack {name}")
            failed = True
    phone_name = target.phone_number_id_parameter
    if _parameter_exists(session.client("ssm", region_name="us-east-1"), phone_name):
        print(f"PASS parameter {phone_name}")
    else:
        print(f"FAIL parameter {phone_name}")
        failed = True
    if _topic_resolves(formed):
        print("PASS shared inbound topic")
    else:
        print("FAIL shared inbound topic")
        failed = True
    prefix = target.demo_numbers_prefix()
    names = live_driver_parameter_names(prefix)
    if not names:
        print("FAIL live drivers none")
        failed = True
    ssm = session.client("ssm", region_name="us-east-1")
    for name in names:
        if _parameter_exists(ssm, name):
            print(f"PASS parameter {name}")
        else:
            print(f"FAIL parameter {name}")
            failed = True
    return 1 if failed else 0


def live_driver_parameter_names(prefix: str) -> list[str]:
    """SSM names for seeded drivers with ``live_contacts`` not false. Values stay out."""

    names: list[str] = []
    for tenant_id in DEMO_TENANTS:
        for contact in load_seed(tenant_id)["contacts"]:
            if contact.get("party_role") != "driver":
                continue
            if not contact_is_live(contact):
                continue
            names.append(
                driver_demo_number_name(tenant_id, str(contact["contact_id"]), prefix=prefix)
            )
    return names


def _credentials(session: Any) -> bool:
    try:
        identity = session.client("sts", region_name="us-east-1").get_caller_identity()
    except Exception:
        return False
    account = identity.get("Account") if isinstance(identity, dict) else None
    return isinstance(account, str) and bool(account.strip())


def _stack_ready(cloudformation: Any, name: str) -> bool:
    try:
        response = cloudformation.describe_stacks(StackName=name)
    except Exception:
        return False
    stacks = response.get("Stacks") if isinstance(response, dict) else None
    if not isinstance(stacks, list) or not stacks:
        return False
    status = stacks[0].get("StackStatus")
    return isinstance(status, str) and status.endswith("_COMPLETE")


def _parameter_exists(ssm: Any, name: str) -> bool:
    try:
        found = ssm.get_parameter(Name=name, WithDecryption=True)
    except Exception:
        return False
    parameter = found.get("Parameter") if isinstance(found, dict) else None
    if not isinstance(parameter, dict):
        return False
    parameter.pop("Value", None)
    return parameter.get("Name") == name


def _topic_resolves(cloudformation: Any) -> bool:
    """True when main's inbound topic has a physical id. The id is not printed."""

    token = None
    while True:
        request: dict[str, str] = {"StackName": "OerfComputeStack"}
        if token:
            request["NextToken"] = token
        try:
            page = cloudformation.list_stack_resources(**request)
        except Exception:
            return False
        for item in page.get("StackResourceSummaries") or []:
            if not isinstance(item, dict):
                continue
            if item.get("ResourceType") != "AWS::SNS::Topic":
                continue
            logical = str(item.get("LogicalResourceId") or "")
            physical = str(item.get("PhysicalResourceId") or "").strip()
            if logical.startswith("InboundTopic") and physical.startswith("arn:aws:sns:"):
                return True
        token = page.get("NextToken")
        if not token:
            return False


def _session(profile: str) -> Any:
    import boto3

    return boto3.Session(profile_name=profile, region_name="us-east-1")


if __name__ == "__main__":
    sys.exit(main())
