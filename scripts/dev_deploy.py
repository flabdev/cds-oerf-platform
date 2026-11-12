"""Context for a laptop dev deploy. This module does not run CDK.

Optional inputs follow ``.github/workflows/deploy.yml`` on the dev environment.
For ``WA_PHONE_NUMBER_ID``, ``ALARM_EMAIL``, ``SHARED_INBOUND_TOPIC_ARN``,
``CHANNELS_LIVE``, and ``STAGE_URL_TEMPLATES`` an explicit environment
variable wins (the workflow input).
Otherwise ``gh variable get <NAME> --env dev`` wins over the repository
variable, which is how ``vars.*`` resolves in that job. The phone id then
falls back to SSM ``/oerf/dev/whatsapp/phone-number-id``. A context key is
omitted only when the resolved value is empty. ``channels:live`` and
``whatsapp:stageUrlTemplates`` are always passed: an empty source becomes
``false``, matching the workflow.

The phone id is required only when ``channels:live`` is true. Before the
command returns, a deployed router ``WHATSAPP_PHONE_NUMBER_ID``, an alarm
email subscription, or a dev subscription to main's inbound topic refuses an
empty resolution. ``DEV_DEPLOY_ALLOW_OMIT=1`` allows that omission. The report
names the source
and whether the key is set or omitted. It does not print the values.

    python -m scripts.dev_deploy check --profile <profile> --account <account-id>
    python -m scripts.dev_deploy argv --profile <profile> --account <account-id>
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from infra.naming import Stage
from scripts._stage import COMPUTE_STACK, OBSERVABILITY_STACK, REGION, STACK_BASES

CONTEXT_KEYS = (
    "stage",
    "auth:managedLogin",
    "account",
    "agentcore:enabled",
    "dashboard:enabled",
    "desk:publish",
    "channels:live",
    "whatsapp:stageUrlTemplates",
    "whatsapp:sharedInboundTopicArn",
    "whatsapp:phoneNumberId",
    "alarms:email",
)
REMINDER = (
    "Normal dev deploys happen by merging to develop. "
    "dev-deploy and dev-diff are for laptop use."
)
LIVE_PHONE_ERROR = (
    "channels_live requires phone_number_id or repository variable "
    "WA_PHONE_NUMBER_ID so WhatsApp IAM is scoped."
)
GhGet = Callable[..., str]


@dataclass(frozen=True)
class Resolved:
    """One workflow input. ``source`` is gh env, gh repo, SSM, override, or empty."""

    name: str
    value: str
    source: str

    @property
    def present(self) -> bool:
        return bool(self.value)


def deploy_context(
    *,
    account: str,
    phone_id: str,
    topic_arn: str,
    alarm_email: str,
    channels_live: str,
    stage_url_templates: str = "false",
) -> dict[str, str]:
    """The ``-c`` values. Empty phone, topic, and email keys are omitted.

    ``channels_live`` true with an empty phone id is the workflow error.
    """

    if not account.strip():
        raise SystemExit("dev-deploy needs account")
    live = "true" if channels_live.strip() == "true" else "false"
    url_templates = "true" if stage_url_templates.strip() == "true" else "false"
    if live == "true" and not phone_id.strip():
        raise SystemExit(LIVE_PHONE_ERROR)
    context = {
        "stage": "dev",
        "auth:managedLogin": "true",
        "account": account.strip(),
        "agentcore:enabled": "true",
        "dashboard:enabled": "true",
        "desk:publish": "true",
        "channels:live": live,
        "whatsapp:stageUrlTemplates": url_templates,
    }
    if topic_arn.strip():
        context["whatsapp:sharedInboundTopicArn"] = topic_arn.strip()
    if phone_id.strip():
        context["whatsapp:phoneNumberId"] = phone_id.strip()
    if alarm_email.strip():
        context["alarms:email"] = alarm_email.strip()
    return context


def context_arguments(context: Mapping[str, str]) -> list[str]:
    """Stack names and ``-c`` pairs. Keys absent from ``context`` are omitted."""

    stage = Stage(name="dev")
    arguments = [stage.stack(base) for base in STACK_BASES]
    for key in CONTEXT_KEYS:
        if key not in context:
            continue
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


def resolve_named(
    name: str,
    environ: Mapping[str, str],
    gh_get: GhGet,
    *,
    ssm: Callable[[], str] | None = None,
) -> Resolved:
    """Workflow order: environment override, gh dev, gh repo, then SSM for the phone."""

    override = environ.get(name, "").strip()
    if override:
        _single_line(override)
        return Resolved(name, override, "override")
    for environment, source in (("dev", "gh env"), (None, "gh repo")):
        found = gh_get(name, environment=environment)
        found = found.strip() if isinstance(found, str) else ""
        if found:
            _single_line(found)
            return Resolved(name, found, source)
    if ssm is not None:
        found = ssm().strip()
        if found:
            _single_line(found)
            return Resolved(name, found, "SSM")
    return Resolved(name, "", "empty")


def resolve_inputs(
    session: Any,
    *,
    account: str,
    environ: Mapping[str, str] | None = None,
    gh_get: GhGet | None = None,
) -> dict[str, str]:
    """Resolve, report, and refuse a deploy that would drop a live dev setting."""

    env = os.environ if environ is None else environ
    reader = gh_get or _gh_variable
    ssm = session.client("ssm", region_name=REGION)
    phone = resolve_named(
        "WA_PHONE_NUMBER_ID",
        env,
        reader,
        ssm=lambda: read_phone_id(ssm),
    )
    alarm = resolve_named("ALARM_EMAIL", env, reader)
    topic = resolve_named("SHARED_INBOUND_TOPIC_ARN", env, reader)
    channels = resolve_named("CHANNELS_LIVE", env, reader)
    stage_urls = resolve_named("STAGE_URL_TEMPLATES", env, reader)
    live = "true" if channels.value == "true" else "false"
    url_templates = "true" if stage_urls.value == "true" else "false"
    _report(phone, alarm, topic, channels, stage_urls)
    context = deploy_context(
        account=account,
        phone_id=phone.value,
        topic_arn=topic.value,
        alarm_email=alarm.value,
        channels_live=live,
        stage_url_templates=url_templates,
    )
    _guard_omit(session, env, phone=phone, alarm=alarm, topic=topic)
    return context


def main(
    argv: list[str] | None = None,
    *,
    session_factory: Any = None,
    gh_get: GhGet | None = None,
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


def _guard_omit(
    session: Any,
    environ: Mapping[str, str],
    *,
    phone: Resolved,
    alarm: Resolved,
    topic: Resolved,
) -> None:
    """Stop when an empty value would widen IAM, drop email, or leave WhatsApp."""

    if environ.get("DEV_DEPLOY_ALLOW_OMIT") == "1":
        return
    problems: list[str] = []
    if not phone.present and _router_phone_is_set(session):
        problems.append(
            "OerfComputeStack-dev router has WHATSAPP_PHONE_NUMBER_ID set; "
            "omitting whatsapp:phoneNumberId would widen the router permission"
        )
    if not alarm.present and _alarm_subscription_is_set(session):
        problems.append(
            "OerfObservabilityStack-dev has an alarm email subscription; "
            "omitting alarms:email would delete the subscription"
        )
    if not topic.present and _dev_subscribes_to_main_inbound(session):
        problems.append(
            "would move the dev inbound subscription off main's topic; "
            "dev would stop receiving WhatsApp events"
        )
    if problems:
        raise SystemExit(
            "refusing dev-deploy: "
            + "; ".join(problems)
            + ". Set DEV_DEPLOY_ALLOW_OMIT=1 to omit anyway."
        )


def _router_phone_is_set(session: Any) -> bool:
    name = Stage(name="dev").res("channel-router")
    lambda_client = session.client("lambda", region_name=REGION)
    try:
        configuration = lambda_client.get_function_configuration(FunctionName=name)
    except Exception:
        return False
    environment = configuration.get("Environment") if isinstance(configuration, dict) else None
    variables = environment.get("Variables") if isinstance(environment, dict) else None
    if not isinstance(variables, dict):
        return False
    value = variables.get("WHATSAPP_PHONE_NUMBER_ID", "")
    return isinstance(value, str) and bool(value.strip())


def _dev_subscribes_to_main_inbound(session: Any) -> bool:
    """True when the dev compute stack subscribes to main's inbound topic.

    Main's topic name starts with ``OerfComputeStack-InboundTopic``. The dev
    stack's own topic starts with ``OerfComputeStack-dev-``, so that name does
    not match. The topic ARN is not printed.
    """

    stack_name = Stage(name="dev").stack(COMPUTE_STACK)
    cloudformation = session.client("cloudformation", region_name=REGION)
    sns = session.client("sns", region_name=REGION)
    token = None
    while True:
        request: dict[str, str] = {"StackName": stack_name}
        if token:
            request["NextToken"] = token
        try:
            page = cloudformation.list_stack_resources(**request)
        except Exception:
            return False
        for item in page.get("StackResourceSummaries") or []:
            if not isinstance(item, dict):
                continue
            if item.get("ResourceType") != "AWS::SNS::Subscription":
                continue
            physical = str(item.get("PhysicalResourceId") or "").strip()
            if _subscription_topic_is_main_inbound(sns, physical):
                return True
        token = page.get("NextToken")
        if not token:
            return False


def _subscription_topic_is_main_inbound(sns: Any, subscription_arn: str) -> bool:
    if not subscription_arn:
        return False
    topic_arn = ""
    try:
        found = sns.get_subscription_attributes(SubscriptionArn=subscription_arn)
    except Exception:
        found = None
    attributes = found.get("Attributes") if isinstance(found, dict) else None
    if isinstance(attributes, dict):
        raw = attributes.get("TopicArn")
        if isinstance(raw, str):
            topic_arn = raw.strip()
    if not topic_arn:
        topic_arn = subscription_arn
    return _topic_name(topic_arn).startswith("OerfComputeStack-InboundTopic")


def _topic_name(topic_arn: str) -> str:
    parts = topic_arn.split(":")
    if len(parts) >= 6 and parts[0] == "arn" and parts[2] == "sns":
        return parts[5]
    return ""


def _alarm_subscription_is_set(session: Any) -> bool:
    stack_name = Stage(name="dev").stack(OBSERVABILITY_STACK)
    cloudformation = session.client("cloudformation", region_name=REGION)
    token = None
    while True:
        request: dict[str, str] = {"StackName": stack_name}
        if token:
            request["NextToken"] = token
        try:
            page = cloudformation.list_stack_resources(**request)
        except Exception:
            return False
        for item in page.get("StackResourceSummaries") or []:
            if isinstance(item, dict) and item.get("ResourceType") == "AWS::SNS::Subscription":
                return True
        token = page.get("NextToken")
        if not token:
            return False


def _report(
    phone: Resolved,
    alarm: Resolved,
    topic: Resolved,
    channels: Resolved,
    stage_urls: Resolved,
) -> None:
    """One line per value. The line has the name, the source, and set or omitted."""

    rows = (
        (phone, phone.present),
        (alarm, alarm.present),
        (topic, topic.present),
        (channels, True),
        (stage_urls, True),
    )
    for item, included in rows:
        status = "set" if included else "omitted"
        print(f"{item.name} {item.source} {status}", file=sys.stderr)


def _single_line(value: str) -> None:
    if "\n" in value or "\r" in value:
        raise SystemExit("Variables must be a single line.")


def _gh_variable(name: str, *, environment: str | None) -> str:
    command = ["gh", "variable", "get", name]
    if environment:
        command.extend(["--env", environment])
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout


def _session(profile: str) -> Any:
    import boto3

    return boto3.Session(profile_name=profile, region_name=REGION)


if __name__ == "__main__":
    raise SystemExit(main())
