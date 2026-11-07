"""CloudFormation handler permission coverage for dev deploy policies.

Loads ``infra/bootstrap/iam-fixtures/cfn-handler-permissions.json`` and checks that
``intersection_allows`` succeeds for every IAM action the handlers need,
using dev-scoped probe ARNs and the same tag contexts as
``scripts/check_dev_boundary.TAGGED_POLICY_CHECKS`` where the boundary is
tag-gated.

Synthetic types ``AWS::CDK::Metadata`` and empty handler files are skipped.
``AWS::CloudFormation::CustomResource`` has no handler IAM actions; the
provider Lambda is checked via ``AWS::Lambda::Function``. Bedrock guardrail
``kms:*`` handler calls apply only when ``KmsKeyArn`` is set; the dev template
omits it, so those actions are reported as not applicable rather than required
allows. Other skips use ``template_property_rules()`` plus delivery-graph checks,
verified against synthesized dev templates when ``dev_templates`` is passed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from infra.dev_boundary import (
    _glob as iam_glob_match,
    intersection_allows,
    is_workload_permissions_boundary,
)
from infra.service_reference_probes import probe_arn_from_service_reference

# Invoked via provider Lambda only; CFN has no handler policy for this type.
CUSTOM_RESOURCE_CFN_TYPE = "AWS::CloudFormation::CustomResource"
_CUSTOM_RESOURCE_SKIP = (
    "not checked: CustomResource has no handler permissions; "
    "provider Lambda uses AWS::Lambda::Function allows"
)

# Resource types that appear in a dev synth of the four application stacks
# (data, compute, API, observability). Keep in sync when templates change.
_DEV_APP_TEMPLATE_TYPES: frozenset[str] = frozenset(
    {
        "AWS::ApiGateway::Authorizer",
        "AWS::ApiGateway::Deployment",
        "AWS::ApiGateway::GatewayResponse",
        "AWS::ApiGateway::Method",
        "AWS::ApiGateway::Resource",
        "AWS::ApiGateway::RestApi",
        "AWS::ApiGateway::Stage",
        "AWS::ApiGatewayV2::Api",
        "AWS::ApiGatewayV2::Authorizer",
        "AWS::ApiGatewayV2::Integration",
        "AWS::ApiGatewayV2::Route",
        "AWS::ApiGatewayV2::Stage",
        "AWS::Bedrock::Guardrail",
        "AWS::Bedrock::GuardrailVersion",
        "AWS::BedrockAgentCore::Runtime",
        "AWS::CloudFormation::CustomResource",
        "AWS::CloudFront::Distribution",
        "AWS::CloudFront::OriginAccessControl",
        "AWS::CloudWatch::Alarm",
        "AWS::CloudWatch::Dashboard",
        "AWS::Cognito::UserPool",
        "AWS::Cognito::UserPoolClient",
        "AWS::Cognito::UserPoolDomain",
        "AWS::Cognito::UserPoolGroup",
        "AWS::DynamoDB::Table",
        "AWS::Events::EventBus",
        "AWS::Events::Rule",
        "AWS::IAM::Policy",
        "AWS::IAM::Role",
        "AWS::Lambda::EventSourceMapping",
        "AWS::Lambda::Function",
        "AWS::Lambda::Permission",
        "AWS::Logs::Delivery",
        "AWS::Logs::DeliveryDestination",
        "AWS::Logs::DeliverySource",
        "AWS::Logs::LogGroup",
        "AWS::Logs::MetricFilter",
        "AWS::S3::Bucket",
        "AWS::S3::BucketPolicy",
        "AWS::SNS::Subscription",
        "AWS::SNS::Topic",
        "AWS::SNS::TopicPolicy",
        "AWS::SQS::Queue",
        "AWS::SQS::QueuePolicy",
        "AWS::SSM::Parameter",
        "AWS::StepFunctions::StateMachine",
    }
)

_RESOURCE_TAG = "aws:ResourceTag/oerf:stage"
_REQUEST_TAG = "aws:RequestTag/oerf:stage"
_DEV_TAG = {_RESOURCE_TAG: "dev"}
_DEV_REQUEST_TAG = {_REQUEST_TAG: "dev"}
_CD_INDEX = re.compile(r":cd[0-9]+$", re.IGNORECASE)


def iam_fixtures_dir(repo_root: Path | None = None) -> Path:
    root = repo_root or Path(__file__).resolve().parents[1]
    return root / "infra" / "bootstrap" / "iam-fixtures"


def handler_permissions_fixture(repo_root: Path | None = None) -> Path:
    return iam_fixtures_dir(repo_root) / "cfn-handler-permissions.json"


def service_reference_fixture(repo_root: Path | None = None) -> Path:
    return iam_fixtures_dir(repo_root) / "service-reference.json"


def handler_permission_dir(repo_root: Path | None = None) -> Path:
    """Default path for handler fixture data (consolidated JSON)."""

    return handler_permissions_fixture(repo_root)


def dev_template_resource_types(template: dict[str, Any]) -> set[str]:
    """``AWS::…`` types declared in one synthesized CloudFormation template."""

    found: set[str] = set()
    resources = template.get("Resources")
    if not isinstance(resources, dict):
        return found
    for resource in resources.values():
        if not isinstance(resource, dict):
            continue
        cfn_type = resource.get("Type")
        if isinstance(cfn_type, str) and cfn_type.startswith("AWS::"):
            found.add(cfn_type)
    return found


def type_to_filename(cfn_type: str) -> str:
    return cfn_type.replace("::", "__") + ".json"


def load_handler_files(
    fixture: Path,
    *,
    types: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Load handler permissions; keys are CloudFormation type names."""

    want = frozenset(types) if types is not None else None
    out: dict[str, dict[str, Any]] = {}
    if not fixture.is_file():
        return out
    blob = json.loads(fixture.read_text(encoding="utf-8"))
    if not isinstance(blob, dict):
        return out
    for cfn_type in sorted(blob):
        if want is not None and cfn_type not in want:
            continue
        handlers = blob[cfn_type]
        if not isinstance(handlers, dict):
            continue
        out[cfn_type] = {"type": cfn_type, "handlers": handlers}
    return out


def iter_handler_actions(data: dict[str, Any]) -> list[str]:
    """Union of deploy handler operations (excludes the ``list`` handler)."""

    deploy, _listed = _split_handler_actions(data)
    return deploy


def iter_handler_list_actions(data: dict[str, Any]) -> list[str]:
    """Actions listed only under the ``list`` handler (resource discovery)."""

    _deploy, listed = _split_handler_actions(data)
    return listed


def collect_handler_list_permissions(
    handler_fixture: Path,
    *,
    types: Iterable[str] | None = None,
) -> list[tuple[str, str]]:
    """``(cfn_type, action)`` pairs from ``list`` handlers only."""

    load_types = frozenset(types) if types is not None else _DEV_APP_TEMPLATE_TYPES
    documents = load_handler_files(handler_fixture, types=load_types)
    out: list[tuple[str, str]] = []
    for cfn_type in sorted(load_types):
        data = documents.get(cfn_type)
        if data is None:
            continue
        for action in iter_handler_list_actions(data):
            out.append((cfn_type, action))
    return out


def _split_handler_actions(
    data: dict[str, Any],
) -> tuple[list[str], list[str]]:
    deploy: list[str] = []
    listed: list[str] = []
    handlers = data.get("handlers")
    if not isinstance(handlers, dict):
        return deploy, listed
    for op, ops in handlers.items():
        if not isinstance(ops, list):
            continue
        bucket = listed if op == "list" else deploy
        for item in ops:
            if isinstance(item, str) and item not in bucket:
                bucket.append(item)
    return deploy, listed


_CFN_RESOURCE_TYPE_HINT: dict[str, str] = {
    "AWS::ApiGateway::Authorizer": "Authorizer",
    "AWS::ApiGateway::Deployment": "Deployment",
    "AWS::ApiGateway::GatewayResponse": "GatewayResponse",
    "AWS::ApiGateway::Method": "Method",
    "AWS::ApiGateway::Resource": "Resource",
    "AWS::ApiGateway::RestApi": "RestApi",
    "AWS::ApiGateway::Stage": "Stage",
    "AWS::ApiGatewayV2::Api": "Api",
    "AWS::ApiGatewayV2::Authorizer": "Authorizer",
    "AWS::ApiGatewayV2::Integration": "Integration",
    "AWS::ApiGatewayV2::Route": "Route",
    "AWS::ApiGatewayV2::Stage": "Stage",
    "AWS::Bedrock::Guardrail": "guardrail",
    "AWS::Bedrock::GuardrailVersion": "guardrail",
    "AWS::BedrockAgentCore::Runtime": "runtime",
    "AWS::CloudFront::Distribution": "distribution",
    "AWS::CloudFront::OriginAccessControl": "origin-access-control",
    "AWS::CloudWatch::Alarm": "alarm",
    "AWS::CloudWatch::Dashboard": "dashboard",
    "AWS::Cognito::UserPool": "userpool",
    "AWS::Cognito::UserPoolClient": "userpoolClient",
    "AWS::Cognito::UserPoolDomain": "userpool",
    "AWS::Cognito::UserPoolGroup": "group",
    "AWS::DynamoDB::Table": "table",
    "AWS::Events::EventBus": "event-bus",
    "AWS::Events::Rule": "rule-on-default-event-bus",
    "AWS::IAM::Policy": "policy",
    "AWS::IAM::Role": "role",
    "AWS::Lambda::EventSourceMapping": "eventSourceMapping",
    "AWS::Lambda::Function": "function",
    "AWS::Lambda::Permission": "function",
    "AWS::Logs::Delivery": "delivery",
    "AWS::Logs::DeliveryDestination": "deliveryDestination",
    "AWS::Logs::DeliverySource": "deliverySource",
    "AWS::Logs::LogGroup": "logGroup",
    "AWS::Logs::MetricFilter": "logGroup",
    "AWS::S3::Bucket": "bucket",
    "AWS::S3::BucketPolicy": "bucket",
    "AWS::SNS::Subscription": "topic",
    "AWS::SNS::Topic": "topic",
    "AWS::SNS::TopicPolicy": "topic",
    "AWS::SQS::Queue": "queue",
    "AWS::SQS::QueuePolicy": "queue",
    "AWS::SSM::Parameter": "parameter",
    "AWS::StepFunctions::StateMachine": "stateMachine",
}


def bedrock_guardrail_has_kms_key_arn(template: dict[str, Any]) -> bool:
    """True when the dev synth includes ``KmsKeyArn`` on ``AWS::Bedrock::Guardrail``."""

    for resource in template.get("Resources", {}).values():
        if resource.get("Type") != "AWS::Bedrock::Guardrail":
            continue
        props = resource.get("Properties") or {}
        return "KmsKeyArn" in props
    return False


def dev_iam_roles_all_have_permissions_boundary(
    templates: Iterable[dict[str, Any]], *, account: str
) -> bool:
    """True when every ``AWS::IAM::Role`` has the workload boundary reference."""

    for template in templates:
        for resource in template.get("Resources", {}).values():
            if resource.get("Type") != "AWS::IAM::Role":
                continue
            props = resource.get("Properties") or {}
            if not is_workload_permissions_boundary(
                props.get("PermissionsBoundary"), account
            ):
                return False
    return True


@dataclass(frozen=True)
class TemplatePropertyRule:
    """Skip handler actions when dev never sets a triggering property."""

    cfn_type: str
    action_pattern: str
    property_label: str
    predicate: Callable[[Iterable[dict[str, Any]]], bool]


def _resources_of_type(
    templates: Iterable[dict[str, Any]], cfn_type: str
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for template in templates:
        for resource in template.get("Resources", {}).values():
            if resource.get("Type") == cfn_type:
                found.append(resource)
    return found


def _props_nonempty(obj: Any) -> bool:
    if obj is None:
        return False
    if isinstance(obj, list):
        return len(obj) > 0
    if isinstance(obj, dict):
        return len(obj) > 0
    return True


def _any_cfn_resource_has_property(
    templates: Iterable[dict[str, Any]], cfn_type: str, key: str
) -> bool:
    for res in _resources_of_type(templates, cfn_type):
        props = (res.get("Properties") or {}) if isinstance(res, dict) else {}
        if key in props and _props_nonempty(props.get(key)):
            return True
    return False


def _iam_inline_policy_targets_users_or_groups(templates: Iterable[dict[str, Any]]) -> bool:
    for cfn_type in ("AWS::IAM::Policy",):
        for res in _resources_of_type(templates, cfn_type):
            props = res.get("Properties") or {}
            if _props_nonempty(props.get("Users")) or _props_nonempty(props.get("Groups")):
                return True
    return False


def _skip_inline_user_group_policies(templates: Iterable[dict[str, Any]]) -> bool:
    return not _iam_inline_policy_targets_users_or_groups(templates)


def _skip_inline_policy_get_user(templates: Iterable[dict[str, Any]]) -> bool:
    return not _any_cfn_resource_has_property(templates, "AWS::IAM::Policy", "Users")


def _skip_inline_policy_get_group(templates: Iterable[dict[str, Any]]) -> bool:
    return not _any_cfn_resource_has_property(templates, "AWS::IAM::Policy", "Groups")


def _skip_managed_policy_attach_users(templates: Iterable[dict[str, Any]]) -> bool:
    return not _any_cfn_resource_has_property(templates, "AWS::IAM::ManagedPolicy", "Users")


def _skip_managed_policy_attach_groups(templates: Iterable[dict[str, Any]]) -> bool:
    return not _any_cfn_resource_has_property(templates, "AWS::IAM::ManagedPolicy", "Groups")


def _skip_lambda_vpc_efs_s3files(templates: Iterable[dict[str, Any]]) -> bool:
    return not _lambda_has_vpc_or_filesystem(templates)


def _skip_dynamo_kinesis_actions(templates: Iterable[dict[str, Any]]) -> bool:
    return not _dynamo_has_kinesis_stream_spec(templates)


def _skip_s3_metadata_table(templates: Iterable[dict[str, Any]]) -> bool:
    return not _s3_metadata_table_enabled(templates)


def _skip_agentcore_vpc_lattice(templates: Iterable[dict[str, Any]]) -> bool:
    return not _agentcore_runtime_uses_private_network(templates)


def _lambda_has_vpc_or_filesystem(templates: Iterable[dict[str, Any]]) -> bool:
    for res in _resources_of_type(templates, "AWS::Lambda::Function"):
        props = res.get("Properties") or {}
        fn = props.get("Function") or props
        if not isinstance(fn, dict):
            continue
        if "VpcConfig" in fn and _props_nonempty(fn.get("VpcConfig")):
            return True
        if "FileSystemConfigs" in fn and _props_nonempty(fn.get("FileSystemConfigs")):
            return True
    return False


def _dynamo_has_kinesis_stream_spec(templates: Iterable[dict[str, Any]]) -> bool:
    for res in _resources_of_type(templates, "AWS::DynamoDB::Table"):
        props = res.get("Properties") or {}
        if "KinesisStreamSpecification" in props and _props_nonempty(
            props.get("KinesisStreamSpecification")
        ):
            return True
    return False


def _s3_metadata_table_enabled(templates: Iterable[dict[str, Any]]) -> bool:
    for res in _resources_of_type(templates, "AWS::S3::Bucket"):
        props = res.get("Properties") or {}
        if "MetadataTableConfiguration" in props and _props_nonempty(
            props.get("MetadataTableConfiguration")
        ):
            return True
    return False


def _logs_delivery_source_resource_arns(templates: Iterable[dict[str, Any]]) -> set[str]:
    """First two segments ``arn:aws:svc`` for each DeliverySource ResourceArn."""

    prefixes: set[str] = set()
    for res in _resources_of_type(templates, "AWS::Logs::DeliverySource"):
        props = res.get("Properties") or {}
        arn = props.get("ResourceArn") or props.get("Arn")
        if isinstance(arn, str) and arn.startswith("arn:aws:"):
            parts = arn.split(":", 3)
            if len(parts) >= 3:
                prefixes.add(f"{parts[0]}:{parts[1]}:{parts[2]}")
    return prefixes


def _dev_includes_logs_delivery_graph(templates: Iterable[dict[str, Any]]) -> bool:
    for template in templates:
        for body in template.get("Resources", {}).values():
            if isinstance(body, dict) and body.get("Type") == "AWS::Logs::Delivery":
                return True
    return False


def _agentcore_runtime_uses_private_network(templates: Iterable[dict[str, Any]]) -> bool:
    for res in _resources_of_type(templates, "AWS::BedrockAgentCore::Runtime"):
        props = res.get("Properties") or {}
        net = props.get("NetworkConfiguration") or props.get("networkConfiguration")
        if not isinstance(net, dict):
            continue
        if "VpcVPCId" in net or "vpcVPCId" in net:
            return True
        for key in ("Vpc", "vpc", "SubnetIds", "subnetIds", "SecurityGroupIds"):
            if key in net and _props_nonempty(net.get(key)):
                return True
    return False


def _skip_agentcore_capacity_or_workload(templates: Iterable[dict[str, Any]]) -> bool:
    for res in _resources_of_type(templates, "AWS::BedrockAgentCore::Runtime"):
        props = res.get("Properties") or {}
        for key in (
            "CapacityProviderArn",
            "capacityProviderArn",
            "WorkloadIdentityName",
            "workloadIdentityName",
        ):
            if key in props and _props_nonempty(props.get(key)):
                return False
    return True


def _skip_lambda_capacity_provider(templates: Iterable[dict[str, Any]]) -> bool:
    for res in _resources_of_type(templates, "AWS::Lambda::Function"):
        props = res.get("Properties") or {}
        fn = props.get("Function") or props
        if not isinstance(fn, dict):
            continue
        for key in ("CapacityProviderArn", "capacityProviderArn"):
            if key in fn and _props_nonempty(fn.get(key)):
                return False
    return True


def _skip_lambda_runtime_alias(templates: Iterable[dict[str, Any]]) -> bool:
    for res in _resources_of_type(templates, "AWS::Lambda::Function"):
        props = res.get("Properties") or {}
        fn = props.get("Function") or props
        if not isinstance(fn, dict):
            continue
        if "RuntimeAliases" in fn and _props_nonempty(fn.get("RuntimeAliases")):
            return False
    return True


def _skip_logs_index_policy(templates: Iterable[dict[str, Any]]) -> bool:
    for res in _resources_of_type(templates, "AWS::Logs::LogGroup"):
        props = res.get("Properties") or {}
        if "IndexPolicy" in props and _props_nonempty(props.get("IndexPolicy")):
            return False
    return True


def template_property_rules() -> tuple[TemplatePropertyRule, ...]:
    """Rules evaluated in order; first match wins."""

    return (
        TemplatePropertyRule(
            "AWS::IAM::Policy",
            "iam:*UserPolicy*",
            "Users (and Groups on the same type)",
            _skip_inline_user_group_policies,
        ),
        TemplatePropertyRule(
            "AWS::IAM::Policy",
            "iam:*GroupPolicy*",
            "Groups (and Users on the same type)",
            _skip_inline_user_group_policies,
        ),
        TemplatePropertyRule(
            "AWS::IAM::Policy",
            "iam:GetUserPolicy",
            "Users",
            _skip_inline_policy_get_user,
        ),
        TemplatePropertyRule(
            "AWS::IAM::Policy",
            "iam:GetGroupPolicy",
            "Groups",
            _skip_inline_policy_get_group,
        ),
        TemplatePropertyRule(
            "AWS::IAM::ManagedPolicy",
            "iam:AttachUserPolicy",
            "Users",
            _skip_managed_policy_attach_users,
        ),
        TemplatePropertyRule(
            "AWS::IAM::ManagedPolicy",
            "iam:DetachUserPolicy",
            "Users",
            _skip_managed_policy_attach_users,
        ),
        TemplatePropertyRule(
            "AWS::IAM::ManagedPolicy",
            "iam:AttachGroupPolicy",
            "Groups",
            _skip_managed_policy_attach_groups,
        ),
        TemplatePropertyRule(
            "AWS::IAM::ManagedPolicy",
            "iam:DetachGroupPolicy",
            "Groups",
            _skip_managed_policy_attach_groups,
        ),
        TemplatePropertyRule(
            "AWS::Lambda::Function",
            "ec2:*",
            "VpcConfig / FileSystemConfigs",
            _skip_lambda_vpc_efs_s3files,
        ),
        TemplatePropertyRule(
            "AWS::Lambda::Function",
            "elasticfilesystem:*",
            "FileSystemConfigs",
            _skip_lambda_vpc_efs_s3files,
        ),
        TemplatePropertyRule(
            "AWS::Lambda::Function",
            "s3files:*",
            "FileSystemConfigs",
            _skip_lambda_vpc_efs_s3files,
        ),
        TemplatePropertyRule(
            "AWS::DynamoDB::Table",
            "kinesis:*",
            "KinesisStreamSpecification",
            _skip_dynamo_kinesis_actions,
        ),
        TemplatePropertyRule(
            "AWS::S3::Bucket",
            "s3tables:*",
            "MetadataTableConfiguration",
            _skip_s3_metadata_table,
        ),
        TemplatePropertyRule(
            "AWS::BedrockAgentCore::Runtime",
            "vpc-lattice:*",
            "NetworkConfiguration (private/VPC)",
            _skip_agentcore_vpc_lattice,
        ),
        TemplatePropertyRule(
            "AWS::BedrockAgentCore::Runtime",
            "ec2:CreateNetworkInterface",
            "NetworkConfiguration (private/VPC)",
            _skip_agentcore_vpc_lattice,
        ),
        TemplatePropertyRule(
            "AWS::BedrockAgentCore::Runtime",
            "bedrock-agentcore:*CapacityProvider*",
            "CapacityProviderArn",
            _skip_agentcore_capacity_or_workload,
        ),
        TemplatePropertyRule(
            "AWS::BedrockAgentCore::Runtime",
            "bedrock-agentcore:*WorkloadIdentity*",
            "WorkloadIdentityName",
            _skip_agentcore_capacity_or_workload,
        ),
        TemplatePropertyRule(
            "AWS::Lambda::Function",
            "lambda:*CapacityProvider*",
            "CapacityProviderArn",
            _skip_lambda_capacity_provider,
        ),
        TemplatePropertyRule(
            "AWS::Lambda::Function",
            "lambda:AssociateRuntimeAlias",
            "RuntimeAliases",
            _skip_lambda_runtime_alias,
        ),
        TemplatePropertyRule(
            "AWS::Logs::LogGroup",
            "logs:*IndexPolic*",
            "IndexPolicy",
            _skip_logs_index_policy,
        ),
    )


def _vended_log_skip_for_action(
    action: str,
    *,
    templates: Iterable[dict[str, Any]],
) -> str | None:
    service = action.split(":", 1)[0]
    if service not in ("autoloop", "codewhisperer", "workmail", "logs"):
        return None
    if action == "logs:AllowVendedLogDeliveryForResource":
        prefixes = _logs_delivery_source_resource_arns(templates)
        if "arn:aws:logs" in prefixes:
            return None
        return "not applicable: no DeliverySource with ResourceArn to logs"
    if action.endswith("AllowVendedLogDeliveryForResource") and service in (
        "autoloop",
        "codewhisperer",
        "workmail",
    ):
        prefixes = _logs_delivery_source_resource_arns(templates)
        want = f"arn:aws:{service}"
        if want in prefixes:
            return None
        return f"not applicable: DeliverySource ResourceArn types do not include {service}"
    return None


def _logs_firehose_skip(action: str, templates: Iterable[dict[str, Any]]) -> str | None:
    if not action.startswith("firehose:"):
        return None
    if _dev_includes_logs_delivery_graph(templates):
        return None
    return "not applicable: no Logs::Delivery graph referencing DeliverySource/Destination"


def template_property_skip_reason(
    cfn_type: str,
    action: str,
    templates: Iterable[dict[str, Any]] | None,
) -> str | None:
    """N/A when every rule predicate is true for this (type, action); else None or raises."""

    if templates is None:
        return None

    vended = _vended_log_skip_for_action(action, templates=templates)
    if vended is not None:
        return vended
    fh = _logs_firehose_skip(action, templates)
    if fh is not None:
        return fh

    for rule in template_property_rules():
        if rule.cfn_type != cfn_type:
            continue
        if not iam_glob_match(rule.action_pattern, action):
            continue
        if not rule.predicate(templates):
            raise ValueError(
                f"{cfn_type} {action}: dev template sets {rule.property_label}; "
                "remove the skip or extend dev execution allows"
            )
        return f"not applicable: {rule.property_label} absent on every dev resource"
    return None


def _bedrock_kms_skipped_reason(
    cfn_type: str, action: str, *, kms_key_arn_set: bool
) -> str | None:
    """If this Bedrock handler KMS call is N/A for dev, return the reason string."""

    if cfn_type not in ("AWS::Bedrock::Guardrail", "AWS::Bedrock::GuardrailVersion"):
        return None
    if not action.startswith("kms:"):
        return None
    if kms_key_arn_set:
        return None
    return "not applicable: KmsKeyArn unset"


def _iam_role_delete_boundary_skipped_reason(
    cfn_type: str,
    action: str,
    *,
    dev_templates: Iterable[dict[str, Any]] | None,
    account: str,
) -> str | None:
    """Handlers may call delete-boundary on update; dev roles always keep the boundary."""

    if (
        cfn_type != "AWS::IAM::Role"
        or action != "iam:DeleteRolePermissionsBoundary"
    ):
        return None
    if dev_templates is None:
        return None
    if not dev_iam_roles_all_have_permissions_boundary(dev_templates, account=account):
        raise ValueError(
            "dev IAM roles must all set PermissionsBoundary before skipping "
            "iam:DeleteRolePermissionsBoundary for AWS::IAM::Role"
        )
    return "not applicable: every dev role sets PermissionsBoundary"


@dataclass
class HandlerPermissionResult:
    """Credential-free handler check outcome."""

    failures: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str, str]] = field(default_factory=list)


_DEV_HANDLER_SYNTH_CONTEXT: dict[str, str] = {
    "dashboard:enabled": "true",
    "agentcore:enabled": "true",
    "demo:seedDevices": "false",
}


def synth_dev_application_templates(
    account: str,
    region: str,
    *,
    extra_context: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """In-process synth of the four dev application stacks (no ``OerfCiStack``).

    Policies for the handler check come from ``infra.dev_boundary``; this only
    supplies template property scans. Does not set ``ci:deployRoleStack``.
    """

    import aws_cdk as cdk
    from aws_cdk.assertions import Template

    from infra.dev_boundary import apply_to_stack
    from infra.naming import Stage
    from infra.stacks.api_stack import OerfApiStack
    from infra.stacks.compute_stack import OerfComputeStack
    from infra.stacks.data_stack import OerfDataStack
    from infra.stacks.observability import OerfObservabilityStack, lambda_functions

    merged = {**_DEV_HANDLER_SYNTH_CONTEXT, "account": account}
    if extra_context:
        merged.update(extra_context)
    app = cdk.App(context=merged)
    env = cdk.Environment(account=account, region=region)
    stage = Stage("dev")
    data = OerfDataStack(app, "OerfDataStack", stage=stage, env=env)
    compute = OerfComputeStack(app, "OerfComputeStack", data_stack=data, stage=stage, env=env)
    api = OerfApiStack(
        app,
        "OerfApiStack",
        data_stack=data,
        inbound_queue=compute.inbound_queue,
        websocket_url=compute.ws_stage.url,
        stage=stage,
        env=env,
    )
    obs = OerfObservabilityStack(
        app,
        "OerfObservabilityStack",
        functions=lambda_functions(data, compute, api),
        dead_letter_queues=[compute.inbound_dlq, compute.ingest_dlq, compute.stream_dlq],
        state_machines=[data.cold_chain_state_machine, data.engage_party_state_machine],
        channel_router_log_group=compute.channel_router_fn.log_group,
        stage=stage,
        env=env,
    )
    stacks = (data, compute, api, obs)
    for stack in stacks:
        apply_to_stack(stack)
    return [Template.from_stack(stack).to_json() for stack in stacks]


def default_probe_values(
    account: str,
    region: str,
    *,
    guardrail_arn: str,
) -> dict[str, str]:
    """Placeholders aligned with ``test_dev_boundary._check_values``."""

    profile = f"arn:aws:bedrock:{region}:{account}:guardrail-profile/us.guardrail.v1:0"
    west = f"arn:aws:bedrock:us-west-2:{account}:guardrail-profile/us.guardrail.v1:0"
    boundary = f"arn:aws:iam::{account}:policy/OerfDevWorkloadBoundary"
    phone = phone_arn(region, account, "0000000000")
    return {
        "account": account,
        "region": region,
        "boundary": boundary,
        "phone": phone,
        "user_pool_arn": f"arn:aws:cognito-idp:{region}:{account}:userpool/us-east-1_MAIN",
        "dev_user_pool_arn": f"arn:aws:cognito-idp:{region}:{account}:userpool/us-east-1_DEV",
        "distribution_arn": f"arn:aws:cloudfront::{account}:distribution/EMAIN",
        "dev_distribution_arn": f"arn:aws:cloudfront::{account}:distribution/EDEV",
        "rest_api_id": "mainapi",
        "dev_rest_api_id": "devapi",
        "http_api_id": "mainhttp",
        "dev_http_api_id": "devhttp",
        "ws_api_id": "mainws",
        "dev_ws_api_id": "devws",
        "dev_guardrail_arn": guardrail_arn,
        "main_guardrail_arn": f"arn:aws:bedrock:{region}:{account}:guardrail/mainonly0001",
        "profile_arn": profile,
        "profile_west_arn": west,
        "dev_runtime_arn": (
            f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/oerf_dev_agent"
        ),
        "main_runtime_arn": f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/oerf_agent",
    }


def phone_arn(region: str, account: str, phone_number_id: str) -> str:
    from infra.constructs.social_messaging_arns import phone_number_arn as _p

    return _p(region, account, phone_number_id)


def probe_resource_for_handler(
    action: str,
    values: dict[str, str],
    *,
    cfn_type: str | None = None,
) -> str:
    """Probe resource from the AWS service reference (or ``*`` when unscoped)."""

    account = values["account"]
    region = values["region"]
    if action == "iam:CreateServiceLinkedRole":
        return (
            f"arn:aws:iam::{account}:role/aws-service-role/"
            f"{_service_linked_name(cfn_type)}"
        )
    if action in ("events:PutPermission", "events:RemovePermission"):
        return f"arn:aws:events:{region}:{account}:event-bus/oerf-dev-events"
    if action == "kms:ReEncrypt":
        return f"arn:aws:kms:{region}:{account}:key/00000000-0000-0000-0000-000000000001"
    probe_values = dict(values)
    if cfn_type and cfn_type.startswith("AWS::ApiGatewayV2::"):
        probe_values["_apigateway_prefer"] = "/apis/"
    elif cfn_type and cfn_type.startswith("AWS::ApiGateway::"):
        probe_values["_apigateway_prefer"] = "/restapis/"
    hint = _CFN_RESOURCE_TYPE_HINT.get(cfn_type or "")
    svc = action.split(":", 1)[0]
    if svc == "s3":
        tail = action.split(":", 1)[1]
        tail_norm = tail[:1].upper() + tail[1:] if tail[:1].islower() else tail
        object_ops = {
            "GetObject",
            "GetObjectVersion",
            "PutObjectAcl",
            "DeleteObject",
            "PutObject",
        }
        bucket_ops = {
            "ListBucket",
            "GetBucketDefaultStorageClass",
            "PutBucketDefaultStorageClass",
            "PutBucketReplication",
            "PutObjectLockConfiguration",
            "DeleteBucketAnalyticsConfiguration",
            "DeleteBucketCors",
            "DeleteBucketMetricsConfiguration",
            "DeleteBucketEncryption",
            "DeleteBucketLifecycle",
            "DeleteBucketReplication",
        }
        if tail_norm in object_ops:
            hint = "object"
        elif tail_norm in bucket_ops:
            hint = "bucket"
    arn = probe_arn_from_service_reference(
        action, probe_values, resource_type_hint=hint or None
    )
    if arn is not None:
        if arn == "*" and svc == "s3" and hint == "bucket":
            return "arn:aws:s3:::oerf-dev-assets"
        if arn == "*" and svc == "s3" and action.split(":", 1)[-1].lower() in (
            "getobjectmetadata",
            "headobject",
        ):
            return "arn:aws:s3:::oerf-dev-assets/cdk/oerf-dev-key"
        return arn

    svc = action.split(":", 1)[0]
    if svc == "kinesis":
        return f"arn:aws:kinesis:{region}:{account}:stream/oerf-dev-stream"
    if svc == "firehose":
        return f"arn:aws:firehose:{region}:{account}:deliverystream/oerf-dev-delivery"
    if svc == "vpc-lattice":
        return f"arn:aws:vpc-lattice:{region}:{account}:servicenetwork/oerf-dev-sn"
    if svc in ("ec2", "elasticfilesystem", "s3files", "codewhisperer", "autoloop", "workmail"):
        return "*"
    if svc == "execute-api":
        return f"arn:aws:execute-api:{region}:{account}:*"
    if svc == "social-messaging":
        return phone_arn(region, account, values.get("_phone_suffix", "0000000000"))
    return "*"


def _kms_via_for_type(cfn_type: str | None, region: str) -> str:
    """Approximate ViaService for handler KMS calls."""

    if cfn_type in (
        "AWS::Bedrock::Guardrail",
        "AWS::Bedrock::GuardrailVersion",
    ):
        return f"bedrock.{region}.amazonaws.com"
    if cfn_type in (
        "AWS::Cognito::UserPool",
        "AWS::Cognito::UserPoolClient",
        "AWS::Cognito::UserPoolDomain",
    ):
        return f"cognito-idp.{region}.amazonaws.com"
    if cfn_type == "AWS::DynamoDB::Table":
        return f"dynamodb.{region}.amazonaws.com"
    if cfn_type == "AWS::Events::EventBus":
        return f"events.{region}.amazonaws.com"
    if cfn_type == "AWS::Lambda::EventSourceMapping":
        return f"lambda.{region}.amazonaws.com"
    if cfn_type == "AWS::Lambda::Function":
        return f"lambda.{region}.amazonaws.com"
    if cfn_type == "AWS::StepFunctions::StateMachine":
        return f"states.{region}.amazonaws.com"
    return f"lambda.{region}.amazonaws.com"


def _service_linked_name(cfn_type: str | None) -> str:
    if cfn_type == "AWS::DynamoDB::Table":
        return "dynamodb.amazonaws.com"
    if cfn_type in (
        "AWS::Cognito::UserPool",
        "AWS::Cognito::UserPoolClient",
        "AWS::Cognito::UserPoolDomain",
    ):
        return "cognito-idp.amazonaws.com"
    return "bedrock-agentcore.amazonaws.com"


def _boundary_context_for_probe(
    action: str,
    resource: str,
    values: dict[str, str],
    *,
    cfn_type: str | None = None,
) -> dict[str, str]:
    """Tag or PassRole context so ``intersection_allows`` matches deployed behavior."""

    region = values["region"]
    boundary_arn = values["boundary"]

    ctx: dict[str, str] = {}

    if action.startswith("iam:CreateRole") or action == "iam:PutRolePermissionsBoundary":
        ctx["iam:PermissionsBoundary"] = boundary_arn
    elif action in _ROLE_MUTATIONS_EXCLUDING_CREATE:
        ctx["iam:PermissionsBoundary"] = boundary_arn
    elif action == "iam:PassRole":
        if cfn_type == "AWS::SNS::Subscription":
            ctx["iam:PassedToService"] = "sns.amazonaws.com"
        elif cfn_type == "AWS::SNS::Topic":
            ctx["iam:PassedToService"] = "sns.amazonaws.com"
        else:
            ctx["iam:PassedToService"] = "lambda.amazonaws.com"
    elif action == "iam:CreateServiceLinkedRole":
        ctx["iam:AWSServiceName"] = _service_linked_name(cfn_type)

    if action.startswith("kms:") and resource.startswith("arn:aws:kms:"):
        ctx["kms:ViaService"] = _kms_via_for_type(cfn_type, region)

    if action == "bedrock:CreateGuardrail":
        ctx.update(_DEV_REQUEST_TAG)

    if action == "cognito-idp:CreateUserPool":
        ctx.update(_DEV_REQUEST_TAG)
    elif action.startswith("cognito-idp:") and "userpool/" in resource:
        ctx.update(_DEV_TAG)

    if action in (
        "cloudfront:CreateDistribution",
        "cloudfront:CreateDistributionWithTags",
        "cloudfront:CreateConnectionGroup",
    ):
        ctx.update(_DEV_REQUEST_TAG)
    elif action.startswith("cloudfront:") and "distribution/" in resource:
        ctx.update(_DEV_TAG)
    elif action.startswith("cloudfront:") and "origin-access-control/" in resource:
        # OAC resources do not support ResourceTag; prod may retain untagged controls.
        if action in (
            "cloudfront:UpdateOriginAccessControl",
            "cloudfront:DeleteOriginAccessControl",
        ):
            ctx.update(_DEV_TAG)

    if action == "cloudwatch:PutDashboard":
        ctx.update(_DEV_REQUEST_TAG)
    elif action == "cloudwatch:PutMetricAlarm":
        ctx.update(_DEV_REQUEST_TAG)
    elif action.startswith("cloudwatch:") and (
        "dashboard/" in resource or ":alarm:" in resource
    ):
        if action in (
            "cloudwatch:TagResource",
            "cloudwatch:UntagResource",
            "cloudwatch:ListTagsForResource",
        ):
            ctx.update(_DEV_TAG)

    if action == "apigateway:POST":
        ctx.update(_DEV_REQUEST_TAG)
    elif action.startswith("apigateway:") and "/restapis/" in resource:
        ctx.update(_DEV_TAG)
    elif action.startswith("apigateway:") and "/apis/" in resource:
        ctx.update(_DEV_TAG)

    if action.startswith("events:") and "/event-bus/" in resource:
        if action == "events:CreateEventBus":
            ctx.update(_DEV_REQUEST_TAG)
        elif action in (
            "events:TagResource",
            "events:UntagResource",
            "events:ListTagsForResource",
        ):
            ctx.update(_DEV_TAG)
    elif action.startswith("events:") and "/rule/" in resource:
        if action in (
            "events:TagResource",
            "events:UntagResource",
            "events:ListTagsForResource",
        ):
            ctx.update(_DEV_TAG)

    if action in (
        "apigatewayv2:CreateApi",
        "apigatewayv2:ImportApi",
        "apigatewayv2:ReimportApi",
    ):
        ctx.update(_DEV_REQUEST_TAG)
    elif action.startswith("apigatewayv2:"):
        ctx.update(_DEV_TAG)

    return ctx


_ROLE_MUTATIONS_EXCLUDING_CREATE = frozenset(
    {
        "iam:PutRolePolicy",
        "iam:AttachRolePolicy",
        "iam:UpdateAssumeRolePolicy",
        "iam:TagRole",
    }
)


def check_handler_permissions(
    *,
    account: str,
    region: str,
    guardrail_arn: str,
    types: Iterable[str] | None = None,
    handler_fixture: Path | None = None,
    kms_key_arn_set: bool = False,
    dev_templates: Iterable[dict[str, Any]] | None = None,
    handler_dir: Path | None = None,
) -> HandlerPermissionResult:
    """Check each applicable handler action; return failures and skipped rows."""

    values = default_probe_values(account, region, guardrail_arn=guardrail_arn)
    fixture = handler_fixture or handler_dir or handler_permission_dir()
    load_types = frozenset(types) if types is not None else _DEV_APP_TEMPLATE_TYPES
    documents = load_handler_files(fixture, types=load_types)
    failures: list[str] = []
    skipped: list[tuple[str, str, str]] = []

    for cfn_type in sorted(load_types):
        if cfn_type == CUSTOM_RESOURCE_CFN_TYPE:
            skipped.append((cfn_type, "*", _CUSTOM_RESOURCE_SKIP))
            continue
        data = documents.get(cfn_type)
        if data is None:
            failures.append(f"{cfn_type} <missing in {fixture.name}>")
            continue
        if not _split_handler_actions(data)[0] and not _split_handler_actions(data)[1]:
            continue

        for action in iter_handler_actions(data):
            na_reason = _bedrock_kms_skipped_reason(
                cfn_type, action, kms_key_arn_set=kms_key_arn_set
            )
            if na_reason is not None:
                skipped.append((cfn_type, action, na_reason))
                continue

            probe_values = dict(values)
            resource = probe_resource_for_handler(action, probe_values, cfn_type=cfn_type)
            context = _boundary_context_for_probe(
                action, resource, probe_values, cfn_type=cfn_type
            )
            try:
                iam_na = _iam_role_delete_boundary_skipped_reason(
                    cfn_type,
                    action,
                    dev_templates=dev_templates,
                    account=account,
                )
            except ValueError as exc:
                failures.append(f"{cfn_type} {action} {resource} ({exc})")
                continue
            if iam_na is not None:
                skipped.append((cfn_type, action, iam_na))
                continue

            tpl_na: str | None = None
            try:
                tpl_na = template_property_skip_reason(cfn_type, action, dev_templates)
            except ValueError as exc:
                failures.append(f"{cfn_type} {action} {resource} ({exc})")
                continue
            if tpl_na is not None:
                skipped.append((cfn_type, action, tpl_na))
                continue

            if not intersection_allows(
                action,
                resource,
                account,
                region,
                context,
                guardrail_arn=guardrail_arn,
            ):
                failures.append(f"{cfn_type} {action} {resource}")

    return HandlerPermissionResult(failures=failures, skipped=skipped)


def handler_permission_checks(
    *,
    account: str,
    region: str,
    guardrail_arn: str,
    types: Iterable[str] | None = None,
    handler_fixture: Path | None = None,
    kms_key_arn_set: bool = False,
    dev_templates: Iterable[dict[str, Any]] | None = None,
    handler_dir: Path | None = None,
) -> tuple[
    list[tuple[str, str, str, bool, dict[str, str]]],
    list[tuple[str, str, str]],
]:
    """Rows for IAM simulation: (cfn_type, action, resource, expect_allow, context)."""

    values = default_probe_values(account, region, guardrail_arn=guardrail_arn)
    fixture = handler_fixture or handler_dir or handler_permission_dir()
    load_types = frozenset(types) if types is not None else _DEV_APP_TEMPLATE_TYPES
    documents = load_handler_files(fixture, types=load_types)
    rows: list[tuple[str, str, str, bool, dict[str, str]]] = []
    skipped: list[tuple[str, str, str]] = []

    for cfn_type in sorted(load_types):
        if cfn_type == CUSTOM_RESOURCE_CFN_TYPE:
            skipped.append((cfn_type, "*", _CUSTOM_RESOURCE_SKIP))
            continue
        data = documents.get(cfn_type)
        if data is None:
            continue
        if not _split_handler_actions(data)[0] and not _split_handler_actions(data)[1]:
            continue

        for action in iter_handler_actions(data):
            na_reason = _bedrock_kms_skipped_reason(
                cfn_type, action, kms_key_arn_set=kms_key_arn_set
            )
            if na_reason is not None:
                skipped.append((cfn_type, action, na_reason))
                continue

            probe_values = dict(values)
            resource = probe_resource_for_handler(action, probe_values, cfn_type=cfn_type)
            context = _boundary_context_for_probe(
                action, resource, probe_values, cfn_type=cfn_type
            )
            try:
                iam_na = _iam_role_delete_boundary_skipped_reason(
                    cfn_type,
                    action,
                    dev_templates=dev_templates,
                    account=account,
                )
            except ValueError as exc:
                skipped.append((cfn_type, action, f"CHECK ERROR: {exc}"))
                continue
            if iam_na is not None:
                skipped.append((cfn_type, action, iam_na))
                continue

            try:
                tpl_na = template_property_skip_reason(cfn_type, action, dev_templates)
            except ValueError as exc:
                skipped.append((cfn_type, action, f"CHECK ERROR: {exc}"))
                continue
            if tpl_na is not None:
                skipped.append((cfn_type, action, tpl_na))
                continue

            expect_allow = intersection_allows(
                action,
                resource,
                account,
                region,
                context,
                guardrail_arn=guardrail_arn,
            )
            rows.append((cfn_type, action, resource, expect_allow, context))

    return rows, skipped


def attach_compatible_resource_arn(resource: str, account: str, region: str) -> str:
    """Return the probe ARN unchanged (kept for CLI compatibility)."""

    _ = account, region
    return resource
