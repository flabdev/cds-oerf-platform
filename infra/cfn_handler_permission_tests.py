"""CloudFormation handler permission coverage for dev deploy policies.

Loads ``infra/bootstrap/cfn-handler-permissions/*.json`` and checks that
``intersection_allows`` succeeds for every IAM action the handlers need,
using dev-scoped probe ARNs and the same tag contexts as
``scripts/check_dev_boundary.TAGGED_POLICY_CHECKS`` where the boundary is
tag-gated.

Synthetic types ``AWS::CDK::Metadata`` and empty handler files are skipped.
``AWS::CloudFormation::CustomResource`` has no handler IAM actions; the
provider Lambda is checked via ``AWS::Lambda::Function``. Bedrock guardrail
``kms:*`` handler calls apply only when ``KmsKeyArn`` is set; the dev template
omits it, so those actions are reported as not applicable rather than required
allows.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from infra.dev_boundary import intersection_allows, is_workload_permissions_boundary

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


def handler_permission_dir(repo_root: Path | None = None) -> Path:
    root = repo_root or Path(__file__).resolve().parents[1]
    return root / "infra" / "bootstrap" / "cfn-handler-permissions"


def type_to_filename(cfn_type: str) -> str:
    return cfn_type.replace("::", "__") + ".json"


def load_handler_files(
    directory: Path,
    *,
    types: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Load JSON files; keys are CloudFormation type names."""

    want = frozenset(types) if types is not None else None
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        if path.stat().st_size == 0:
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        cfn_type = data.get("type")
        if not isinstance(cfn_type, str):
            continue
        if want is not None and cfn_type not in want:
            continue
        out[cfn_type] = data
    return out


def iter_handler_actions(data: dict[str, Any]) -> list[str]:
    """Union of all handler operation lists."""

    seen: list[str] = []
    handlers = data.get("handlers")
    if not isinstance(handlers, dict):
        return seen
    for ops in handlers.values():
        if not isinstance(ops, list):
            continue
        for item in ops:
            if isinstance(item, str) and item not in seen:
                seen.append(item)
    return seen


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
    """Synthetic resource ARN for IAM simulation."""

    account = values["account"]
    region = values["region"]
    svc = action.split(":", 1)[0]

    if svc == "s3":
        return "arn:aws:s3:::oerf-dev-bucket/o"
    if svc == "sns":
        return f"arn:aws:sns:{region}:{account}:OerfComputeStack-dev-InboundTopicX"
    if svc == "sqs":
        return f"arn:aws:sqs:{region}:{account}:OerfComputeStack-dev-queue"
    if svc == "lambda":
        return f"arn:aws:lambda:{region}:{account}:function:oerf-dev-fn"
    if svc == "dynamodb":
        return f"arn:aws:dynamodb:{region}:{account}:table/OerfDataStack-dev-TableX"
    if svc == "logs":
        return f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/oerf-dev-x"
    if svc == "states":
        return f"arn:aws:states:{region}:{account}:stateMachine:OerfDataStack-dev-sm"
    if svc == "events":
        return f"arn:aws:events:{region}:{account}:rule/OerfComputeStack-dev-rule"
    if svc == "cloudwatch":
        return f"arn:aws:cloudwatch:{region}:{account}:alarm:OerfObservabilityStack-dev-a"
    if svc == "ssm":
        return f"arn:aws:ssm:{region}:{account}:parameter/oerf/dev/param"
    if svc == "iam":
        role = f"arn:aws:iam::{account}:role/OerfDataStack-dev-Example"
        if action == "iam:CreateServiceLinkedRole":
            return f"arn:aws:iam::{account}:role/aws-service-role/bedrock-agentcore.amazonaws.com"
        if action == "iam:CreateRole":
            return role
        if action == "iam:PassRole":
            return role
        if action == "iam:ListRoles":
            return "*"
        if "RolePolicy" in action or action in (
            "iam:GetRole",
            "iam:AttachRolePolicy",
            "iam:DetachRolePolicy",
            "iam:DeleteRole",
            "iam:ListAttachedRolePolicies",
            "iam:ListRolePolicies",
            "iam:ListRoleTags",
            "iam:TagRole",
            "iam:UntagRole",
            "iam:UpdateRole",
            "iam:UpdateRoleDescription",
            "iam:UpdateAssumeRolePolicy",
            "iam:DeleteRolePermissionsBoundary",
            "iam:PutRolePermissionsBoundary",
            "iam:GetRolePolicy",
            "iam:PutRolePolicy",
            "iam:DeleteRolePolicy",
        ):
            return role
        if "UserPolicy" in action or action in ("iam:GetUser",):
            return f"arn:aws:iam::{account}:user/oerf-dev-placeholder"
        if "GroupPolicy" in action:
            return f"arn:aws:iam::{account}:group/oerf-dev-placeholder"
        return f"arn:aws:iam::{account}:policy/OerfDataStack-dev-inline"
    if svc == "apigateway":
        if action == "apigateway:POST":
            return f"arn:aws:apigateway:{region}::/restapis"
        return f"arn:aws:apigateway:{region}::/restapis/{values['dev_rest_api_id']}/*"
    if svc == "execute-api":
        return f"arn:aws:execute-api:{region}:{account}:*"
    if svc == "cognito-idp":
        if action == "cognito-idp:CreateUserPool":
            return "*"
        return values["dev_user_pool_arn"]
    if svc == "cloudfront":
        if action in (
            "cloudfront:CreateDistribution",
            "cloudfront:CreateDistributionWithTags",
            "cloudfront:CreateConnectionGroup",
        ):
            return "*"
        if action in (
            "cloudfront:DeleteOriginAccessControl",
            "cloudfront:UpdateOriginAccessControl",
            "cloudfront:GetOriginAccessControl",
        ):
            return f"arn:aws:cloudfront::{account}:origin-access-control/OERFDEVOAC"
        return values["dev_distribution_arn"]
    if svc == "ecr":
        return f"arn:aws:ecr:{region}:{account}:repository/cdk-oerfdev-assets"
    if svc == "bedrock-agentcore":
        return values["dev_runtime_arn"]
    if svc == "social-messaging":
        return phone_arn(region, account, values.get("_phone_suffix", "0000000000"))
    if svc == "kinesis":
        return f"arn:aws:kinesis:{region}:{account}:stream/oerf-dev-stream"
    if svc == "vpc-lattice":
        return f"arn:aws:vpc-lattice:{region}:{account}:servicenetwork/oerf-dev-sn"
    if svc in ("ec2", "elasticfilesystem", "s3files"):
        return "*"
    if svc == "firehose":
        return f"arn:aws:firehose:{region}:{account}:deliverystream/oerf-dev-delivery"
    if svc == "kms":
        return f"arn:aws:kms:{region}:{account}:key/00000000-0000-0000-0000-000000000000"
    if svc == "bedrock":
        if action == "bedrock:CreateGuardrail":
            return "*"
        if action == "bedrock:ListGuardrails":
            return "*"
        if action in (
            "bedrock:TagResource",
            "bedrock:UntagResource",
            "bedrock:ListTagsForResource",
        ):
            return values["dev_guardrail_arn"]
        return values["dev_guardrail_arn"]
    if svc in ("codewhisperer", "autoloop", "workmail"):
        return "*"
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

    if action == "apigateway:POST":
        ctx.update(_DEV_REQUEST_TAG)
    elif action.startswith("apigateway:") and "/restapis/" in resource:
        ctx.update(_DEV_TAG)
    elif action.startswith("apigateway:") and "/apis/" in resource:
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
    handler_dir: Path | None = None,
    kms_key_arn_set: bool = False,
    dev_templates: Iterable[dict[str, Any]] | None = None,
) -> HandlerPermissionResult:
    """Check each applicable handler action; return failures and skipped rows."""

    values = default_probe_values(account, region, guardrail_arn=guardrail_arn)
    directory = handler_dir or handler_permission_dir()
    load_types = frozenset(types) if types is not None else _DEV_APP_TEMPLATE_TYPES
    documents = load_handler_files(directory, types=load_types)
    failures: list[str] = []
    skipped: list[tuple[str, str, str]] = []

    for cfn_type in sorted(load_types):
        path = directory / type_to_filename(cfn_type)
        if cfn_type == CUSTOM_RESOURCE_CFN_TYPE:
            skipped.append((cfn_type, "*", _CUSTOM_RESOURCE_SKIP))
            continue
        if not path.exists():
            failures.append(f"{cfn_type} <missing JSON {path.name}>")
            continue
        if path.stat().st_size == 0:
            continue
        data = documents.get(cfn_type)
        if data is None:
            data = json.loads(path.read_text(encoding="utf-8"))

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
    handler_dir: Path | None = None,
    kms_key_arn_set: bool = False,
    dev_templates: Iterable[dict[str, Any]] | None = None,
) -> tuple[
    list[tuple[str, str, str, bool, dict[str, str]]],
    list[tuple[str, str, str]],
]:
    """Rows for IAM simulation: (cfn_type, action, resource, expect_allow, context)."""

    values = default_probe_values(account, region, guardrail_arn=guardrail_arn)
    directory = handler_dir or handler_permission_dir()
    load_types = frozenset(types) if types is not None else _DEV_APP_TEMPLATE_TYPES
    documents = load_handler_files(directory, types=load_types)
    rows: list[tuple[str, str, str, bool, dict[str, str]]] = []
    skipped: list[tuple[str, str, str]] = []

    for cfn_type in sorted(load_types):
        path = directory / type_to_filename(cfn_type)
        if cfn_type == CUSTOM_RESOURCE_CFN_TYPE:
            skipped.append((cfn_type, "*", _CUSTOM_RESOURCE_SKIP))
            continue
        if not path.exists() or path.stat().st_size == 0:
            continue
        data = documents.get(cfn_type) or json.loads(path.read_text(encoding="utf-8"))

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
    """Rewrite probe ARNs so ``resourceGroupList`` matches (drop :cdk* index)."""

    if resource == "*":
        return resource
    if _CD_INDEX.search(resource):
        return resource
    parts = resource.split(":", 5)
    if len(parts) < 6:
        return resource
    # arn:aws:service:region:account:rest...
    svc = parts[2]
    rest = parts[5]
    if svc in (
        "dynamodb",
        "sns",
        "sqs",
        "lambda",
        "states",
        "ssm",
        "logs",
        "cognito-idp",
        "cloudfront",
        "events",
        "cloudwatch",
        "kms",
        "kinesis",
        "social-messaging",
        "ecr",
        "bedrock-agentcore",
        "vpc-lattice",
        "s3",
        "iam",
        "bedrock",
    ):
        return f"{':'.join(parts[:5])}:{rest}:cdk{hash(rest) % 100000}"
    return resource
