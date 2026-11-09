"""Context for a laptop dev deploy. This module does not run CDK.

It reads the same three values the dev workflow requires, and refuses to
print a command when any of them is empty. The shared inbound topic is a
read-only lookup of main's ``OerfComputeStack``. Nothing here writes to AWS.

    python -m scripts.dev_deploy check --profile <profile> --account <account-id>
    python -m scripts.dev_deploy argv --profile <profile> --account <account-id>

``ALARM_EMAIL`` comes from the environment, or from ``gh variable get ALARM_EMAIL``
when that variable is empty. ``CHANNELS_LIVE`` defaults to false.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping
from typing import Any

from infra.naming import Stage
from scripts._stage import STACK_BASES

CONTEXT_KEYS = (
    "stage",
    "auth:managedLogin",
    "account",
    "agentcore:enabled",
    "dashboard:enabled",
    "desk:publish",
    "channels:live",
    "whatsapp:sharedInboundTopicArn",
    "whatsapp:phoneNumberId",
    "alarms:email",
)
REMINDER = (
    "Normal dev deploys happen by merging to develop. "
    "dev-deploy and dev-diff are for laptop use."
)


def deploy_context(
    *,
    account: str,
    phone_id: str,
    topic_arn: str,
    alarm_email: str,
    channels_live: str,
) -> dict[str, str]:
    """The ``-c`` values. Raises ``SystemExit`` when any required value is empty."""

    missing: list[str] = []
    if not phone_id.strip():
        missing.append("whatsapp phone-number id")
    if not topic_arn.strip():
        missing.append("SHARED_INBOUND_TOPIC_ARN")
    if not alarm_email.strip():
        missing.append("ALARM_EMAIL")
    if not account.strip():
        missing.append("account")
    if missing:
        raise SystemExit("dev-deploy needs " + ", ".join(missing))
    live = "true" if channels_live.strip().lower() == "true" else "false"
    return {
        "stage": "dev",
        "auth:managedLogin": "true",
        "account": account.strip(),
        "agentcore:enabled": "true",
        "dashboard:enabled": "true",
        "desk:publish": "true",
        "channels:live": live,
        "whatsapp:sharedInboundTopicArn": topic_arn.strip(),
        "whatsapp:phoneNumberId": phone_id.strip(),
        "alarms:email": alarm_email.strip(),
    }


def context_arguments(context: Mapping[str, str]) -> list[str]:
    """Stack names and ``-c`` pairs for the four dev application stacks."""

    stage = Stage(name="dev")
    arguments = [stage.stack(base) for base in STACK_BASES]
    for key in CONTEXT_KEYS:
        arguments.extend(["-c", f"{key}={context[key]}"])
    return arguments


def read_phone_id(ssm: Any) -> str:
    name = Stage(name="dev").ssm("whatsapp/phone-number-id")
    try:
        found = ssm.get_parameter(Name=name)
    except Exception:
        return ""
    parameter = found.get("Parameter") if isinstance(found, dict) else None
    value = parameter.get("Value") if isinstance(parameter, dict) else ""
    if not isinstance(value, str):
        return ""
    return value.strip()


def read_shared_topic(cloudformation: Any) -> str:
    """Physical id of main's inbound topic. Empty when the lookup fails."""

    token = None
    while True:
        request: dict[str, str] = {"StackName": "OerfComputeStack"}
        if token:
            request["NextToken"] = token
        try:
            page = cloudformation.list_stack_resources(**request)
        except Exception:
            return ""
        for item in page.get("StackResourceSummaries") or []:
            if not isinstance(item, dict):
                continue
            if item.get("ResourceType") != "AWS::SNS::Topic":
                continue
            logical = str(item.get("LogicalResourceId") or "")
            physical = str(item.get("PhysicalResourceId") or "").strip()
            if logical.startswith("InboundTopic") and physical:
                return physical
        token = page.get("NextToken")
        if not token:
            return ""


def read_alarm_email(
    environ: Mapping[str, str],
    gh_get: Callable[[], str] | None = None,
) -> str:
    found = environ.get("ALARM_EMAIL", "").strip()
    if found:
        return found
    reader = gh_get or _gh_variable
    try:
        return reader().strip()
    except Exception:
        return ""


def resolve_inputs(
    session: Any,
    *,
    account: str,
    environ: Mapping[str, str] | None = None,
    gh_get: Callable[[], str] | None = None,
) -> dict[str, str]:
    env = os.environ if environ is None else environ
    return deploy_context(
        account=account,
        phone_id=read_phone_id(session.client("ssm", region_name="us-east-1")),
        topic_arn=read_shared_topic(session.client("cloudformation", region_name="us-east-1")),
        alarm_email=read_alarm_email(env, gh_get),
        channels_live=env.get("CHANNELS_LIVE", "false"),
    )


def main(
    argv: list[str] | None = None,
    *,
    session_factory: Any = None,
    gh_get: Callable[[], str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Assemble the dev CDK context. Does not deploy.")
    parser.add_argument("command", choices=("check", "argv"))
    parser.add_argument("--profile", required=True)
    parser.add_argument("--account", required=True)
    args = parser.parse_args(argv)
    if len(args.account) != 12 or not args.account.isdigit():
        raise SystemExit("account must be a 12-digit account id")
    session = (session_factory or _session)(args.profile)
    context = resolve_inputs(session, account=args.account, gh_get=gh_get)
    print(REMINDER, file=sys.stderr)
    if args.command == "argv":
        print(" ".join(shlex.quote(part) for part in context_arguments(context)))
    else:
        print("dev-deploy values present")
    return 0


def _gh_variable() -> str:
    completed = subprocess.run(
        ["gh", "variable", "get", "ALARM_EMAIL"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout


def _session(profile: str) -> Any:
    import boto3

    return boto3.Session(profile_name=profile, region_name="us-east-1")


if __name__ == "__main__":
    raise SystemExit(main())
