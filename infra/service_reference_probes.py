"""IAM probe ARNs from ``infra/bootstrap/iam-fixtures/service-reference.json``."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "infra"
    / "bootstrap"
    / "iam-fixtures"
    / "service-reference.json"
)

# IAM prefix -> service-reference file stem (``{stem}.json`` on disk).
_SERVICE_FILES: dict[str, str] = {
    "apigateway": "apigateway",
    "bedrock": "bedrock",
    "bedrock-agentcore": "bedrock-agentcore",
    "cloudfront": "cloudfront",
    "cloudwatch": "cloudwatch",
    "cognito-idp": "cognito-idp",
    "dynamodb": "dynamodb",
    "ec2": "ec2",
    "ecr": "ecr",
    "events": "events",
    "iam": "iam",
    "kms": "kms",
    "lambda": "lambda",
    "logs": "logs",
    "s3": "s3",
    "sns": "sns",
    "sqs": "sqs",
    "ssm": "ssm",
    "states": "states",
    "xray": "xray",
}

# Services used by handlers but not present in the trimmed service-reference fixture.
_FALLBACK_PROBE_BUILDERS: dict[str, Any] = {}

_DEV_SUFFIX = "-dev-probe"
_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")


def _camel_to_dev_name(token: str) -> str:
    """``EventBusName`` -> ``oerf-dev-event-bus-name`` (contains ``-dev-``)."""

    parts = re.sub(r"([a-z])([A-Z])", r"\1-\2", token).replace("_", "-").lower()
    if parts.endswith("-name") or parts.endswith("-id"):
        base = parts.rsplit("-", 1)[0]
    else:
        base = parts
    return f"oerf-dev-{base}"


def _default_placeholders(values: dict[str, str]) -> dict[str, str]:
    account = values["account"]
    region = values["region"]
    base = {
        "Partition": "aws",
        "Account": account,
        "Region": region,
        "RestApiId": values.get("dev_rest_api_id", "devapi"),
        "ApiId": values.get("dev_http_api_id", "devhttp"),
        "StageName": "dev",
        "DashboardName": "oerf-dev-dashboard",
        "AlarmName": "oerf-dev-alarm",
        "TopicName": "OerfComputeStack-dev-InboundTopic",
        "QueueName": "OerfComputeStack-dev-queue",
        "TableName": "OerfDataStack-dev-IncidentTable",
        "FunctionName": "oerf-dev-fn",
        "StateMachineName": "OerfDataStack-dev-sm",
        "RuleName": "oerf-dev-rule",
        "EventBusName": "oerf-dev-events",
        "ParameterName": "oerf/dev/param",
        "ParameterNameWithoutLeadingSlash": "oerf/dev/param",
        "BucketName": "oerf-dev-assets",
        "ObjectName": "cdk/oerf-dev-key",
        "LogGroupName": "/aws/lambda/oerf-dev-fn",
        "KeyId": "00000000-0000-0000-0000-000000000001",
        "DeliveryStreamName": "oerf-dev-delivery",
        "StreamName": "oerf-dev-stream",
        "RepositoryName": "cdk-oerfdev-assets",
        "RoleName": "OerfDataStack-dev-Example",
        "PolicyName": "OerfDataStack-dev-inline",
        "UsagePlanId": "oerfdevplan",
        "VpcLinkId": "oerfdevlink",
        "ModelName": "oerfdevmodel",
        "ResourceId": "oerfdevresource",
        "RouteId": "oerfdevroute",
        "OpaqueId": "oerfdevopaque",
        "DistributionId": values.get("dev_distribution_arn", "").rsplit("/", 1)[-1]
        or "EDEV",
        "OriginAccessControlId": "OERFDEVOAC",
        "ResponseHeadersPolicyId": "OERFDEVRHP",
    }
    guardrail = values.get("dev_guardrail_arn", "")
    if guardrail:
        gid = guardrail.rsplit("/", 1)[-1]
        base["GuardrailIdentifier"] = gid
        base["GuardrailId"] = gid
    runtime = values.get("dev_runtime_arn", "")
    if runtime:
        base["RuntimeId"] = runtime.rsplit("/", 1)[-1]
    return base


def _fill_arn_format(template: str, placeholders: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in placeholders:
            return placeholders[key]
        if key not in placeholders:
            placeholders[key] = _camel_to_dev_name(key)
        return placeholders[key]

    return _PLACEHOLDER_RE.sub(repl, template)


@lru_cache(maxsize=1)
def _load_service_reference_blob() -> dict[str, Any]:
    if not _FIXTURE_PATH.is_file():
        return {}
    data = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=64)
def _load_service(file_stem: str) -> dict[str, Any] | None:
    blob = _load_service_reference_blob()
    doc = blob.get(file_stem)
    return doc if isinstance(doc, dict) else None


def _iam_action_name(action: str) -> tuple[str, str]:
    if ":" not in action:
        return action, action
    prefix, name = action.split(":", 1)
    if prefix == "s3" and name[:1].islower():
        # CFN handler JSON uses ``s3:getObject``; IAM docs use ``GetObject``.
        name = name[0].upper() + name[1:]
    return prefix, name


def _action_entry(doc: dict[str, Any], action_name: str) -> dict[str, Any] | None:
    for entry in doc.get("Actions", []):
        if entry.get("Name") == action_name:
            return entry
    return None


def _resource_arn_format(
    doc: dict[str, Any],
    resource_type: str,
    *,
    prefer: str | None = None,
) -> str | None:
    resources = doc.get("Resources") or []
    match: dict[str, Any] | None = None
    for item in resources:
        if item.get("Name") == resource_type:
            match = item
            break
    if match is None:
        return None
    formats = match.get("ARNFormats") or []
    if not formats:
        return None
    if prefer:
        for fmt in formats:
            if prefer in fmt:
                return fmt
    return formats[0]


def _pick_resource_type(
    resource_types: list[dict[str, Any]],
    *,
    hint: str | None,
) -> str | None:
    names = [item.get("Name") for item in resource_types if item.get("Name")]
    if not names:
        return None
    if hint and hint in names:
        return hint
    return names[0]


def probe_arn_from_service_reference(
    action: str,
    values: dict[str, str],
    *,
    resource_type_hint: str | None = None,
) -> str | None:
    """Return a probe ARN, ``*``, or ``None`` when the service file is missing."""

    prefix, action_name = _iam_action_name(action)
    file_stem = _SERVICE_FILES.get(prefix)
    if file_stem is None:
        return None
    doc = _load_service(file_stem)
    if doc is None:
        return None
    entry = _action_entry(doc, action_name)
    if entry is None:
        return "*"
    resource_types = entry.get("Resources")
    if not resource_types:
        return "*"
    first_type = _pick_resource_type(resource_types, hint=resource_type_hint)
    if not first_type:
        return "*"
    prefer: str | None = None
    if prefix == "apigateway":
        if "/apis/" in str(values.get("_apigateway_prefer", "")):
            prefer = "/apis/"
        elif "/restapis/" in str(values.get("_apigateway_prefer", "")):
            prefer = "/restapis/"
    fmt = _resource_arn_format(doc, first_type, prefer=prefer)
    if fmt is None:
        return "*"
    placeholders = _default_placeholders(values)
    return _fill_arn_format(fmt, placeholders)


def register_fallback_probe(prefix: str, builder: Any) -> None:
    _FALLBACK_PROBE_BUILDERS[prefix] = builder
