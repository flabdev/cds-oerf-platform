"""Dev CloudFormation execution policy and workload permissions boundary.

``OerfDevCfnExecPolicy`` and ``OerfDevCfnExecDenies`` replace
AdministratorAccess on ``cdk-oerfdev-cfn-exec-role``. The allows do not fit in
one managed policy once each dev name style is written per service, so the
denies are the second policy. ``OerfDevWorkloadBoundary`` is attached to that
role by the bootstrap and to every role the four dev application stacks
create. The documents are built from the stack account and region.

The boundary is ``Allow *`` plus a ``Deny`` on anything that is not a dev name,
a documented shared ARN, or a call whose resource is ``*`` because the API has
no resource type. Dev names are ``-dev-``, ``_dev_``, ``/dev/``, ``Stackdev``
and ``oerfdev``. They do not match a main name that merely contains ``dev``,
such as ``oerf-device-seed``. IAM does not allow a wildcard in the service
segment, so each style is written on the services that use it. Actions on
``*`` are listed in ``STAR_ACTION_REASONS``. Cognito, API Gateway, CloudFront
and Bedrock guardrails are denied unless the resource or the create request
carries ``oerf:stage=dev``.
"""

from __future__ import annotations

import fnmatch
import json
import re
from typing import Any

from aws_cdk import Stack, Tags, aws_iam as iam

EXEC_POLICY_NAME = "OerfDevCfnExecPolicy"
EXEC_DENY_POLICY_NAME = "OerfDevCfnExecDenies"
BOUNDARY_POLICY_NAME = "OerfDevWorkloadBoundary"
STAGE_TAG_KEY = "oerf:stage"
STAGE_TAG_VALUE = "dev"
_MANAGED_POLICY_LIMIT = 6144

# CreateRole and PutRolePermissionsBoundary send iam:PermissionsBoundary.
# PutRolePolicy, AttachRolePolicy, UpdateAssumeRolePolicy, and TagRole do not
# (IAM service authorization reference), so those allows use
# StringEqualsIfExists: a missing key still matches, and a different value
# does not. StringEquals on the two APIs that send the key fails closed.
_BOUNDARY_KEY = "iam:PermissionsBoundary"
_ROLE_CREATE_ACTIONS = ["iam:CreateRole", "iam:PutRolePermissionsBoundary"]
_ROLE_MUTATION_ACTIONS = [
    "iam:PutRolePolicy",
    "iam:AttachRolePolicy",
    "iam:UpdateAssumeRolePolicy",
    "iam:TagRole",
]
_ROLE_MUTATIONS = _ROLE_CREATE_ACTIONS + _ROLE_MUTATION_ACTIONS
_PASS_TO_SERVICES = [
    "apigateway.amazonaws.com",
    "bedrock-agentcore.amazonaws.com",
    "cognito-idp.amazonaws.com",
    "events.amazonaws.com",
    "lambda.amazonaws.com",
    "states.amazonaws.com",
]
_BROAD_MANAGED_POLICIES = [
    "arn:aws:iam::aws:policy/AdministratorAccess",
    "arn:aws:iam::aws:policy/IAMFullAccess",
    "arn:aws:iam::aws:policy/PowerUserAccess",
]
_SHARED_EVENTS_RULE = "StepFunctionsGetEventsForStepFunctionsExecutionRule"
_MAIN_INBOUND_TOPIC_SUFFIX = "OerfComputeStack-InboundTopic*"
_ASSET_KEY_ALIAS = "alias/cdk-oerfdev-assets-key"

# Resource "*" actions. The reason is the IAM or CloudFormation limitation.
STAR_ACTION_REASONS: dict[str, str] = {
    "apigateway:*": "REST API ids are generated. The stage name is not in the ARN.",
    "apigatewayv2:*": "HTTP and WebSocket API ids are generated. The stage name is not in the ARN.",
    "cloudfront:*": "Distribution and invalidation APIs do not accept a stage name.",
    "cloudwatch:DescribeAlarms": "DescribeAlarms is a read with no alarm name.",
    "cloudwatch:PutMetricData": "PutMetricData has no resource type.",
    "cognito-idp:*": "User pool ARNs contain the generated pool id, not oerf-dev-users.",
    "dynamodb:ListStreams": "ListStreams has no resource type.",
    "ecr:GetAuthorizationToken": "GetAuthorizationToken has no resource type.",
    "lambda:CreateEventSourceMapping": "The mapping id does not exist yet, so the resource is *.",
    "lambda:DeleteEventSourceMapping": "Mapping ids are UUIDs. IAM authorizes the call on *.",
    "lambda:GetEventSourceMapping": "Mapping ids are UUIDs. IAM authorizes the call on *.",
    "lambda:ListEventSourceMappings": "The list API has no resource type.",
    "lambda:UpdateEventSourceMapping": "Mapping ids are UUIDs. IAM authorizes the call on *.",
    "logs:CreateLogDelivery": "Log delivery APIs authorize on *.",
    "logs:DeleteLogDelivery": "Log delivery APIs authorize on *.",
    "logs:DeleteResourcePolicy": "Account log resource policies have no stage in the ARN.",
    "logs:DescribeLogGroups": "The agent runtime lists groups with log-group:*.",
    "logs:DescribeResourcePolicies": "Account log resource policies have no stage in the ARN.",
    "logs:GetLogDelivery": "Log delivery APIs authorize on *.",
    "logs:ListLogDeliveries": "Log delivery APIs authorize on *.",
    "logs:PutResourcePolicy": "Account log resource policies have no stage in the ARN.",
    "logs:UpdateLogDelivery": "Log delivery APIs authorize on *.",
    "sns:Publish": "SMS publish has no topic ARN. Publish to the main inbound topic is denied.",
    "states:SendTaskSuccess": "Task-token APIs have no resource type.",
    "xray:*": "X-Ray APIs have no resource type.",
    "kms:Decrypt": (
        "Asset-bucket key id is generated. The execution policy requires the oerfdev alias."
    ),
    "kms:DescribeKey": (
        "Asset-bucket key id is generated. The execution policy requires the oerfdev alias."
    ),
}

# Granted to the execution role on "*". Workload-only star actions stay off
# this list so CloudFormation cannot call them.
_EXEC_STAR_ACTIONS = [
    "apigateway:*",
    "apigatewayv2:*",
    "cloudfront:*",
    "cloudwatch:DescribeAlarms",
    "cognito-idp:*",
    "ecr:GetAuthorizationToken",
    "lambda:CreateEventSourceMapping",
    "lambda:DeleteEventSourceMapping",
    "lambda:GetEventSourceMapping",
    "lambda:ListEventSourceMappings",
    "lambda:UpdateEventSourceMapping",
    "logs:CreateLogDelivery",
    "logs:DeleteLogDelivery",
    "logs:DeleteResourcePolicy",
    "logs:DescribeLogGroups",
    "logs:DescribeResourcePolicies",
    "logs:GetLogDelivery",
    "logs:ListLogDeliveries",
    "logs:PutResourcePolicy",
    "logs:UpdateLogDelivery",
    "bedrock:CreateGuardrail",
    "bedrock:CreateGuardrailVersion",
    "bedrock:DeleteGuardrail",
    "bedrock:GetGuardrail",
    "bedrock:TagResource",
    "bedrock:UntagResource",
    "bedrock:UpdateGuardrail",
]

# Excluded from the name deny. The tag denies are what fence these in.
# ApplyGuardrail is workload-only, so it is not in the execution allow.
_BEDROCK_TAG_ACTIONS = [
    "bedrock:ApplyGuardrail",
    "bedrock:CreateGuardrail",
    "bedrock:CreateGuardrailVersion",
    "bedrock:DeleteGuardrail",
    "bedrock:GetGuardrail",
    "bedrock:TagResource",
    "bedrock:UntagResource",
    "bedrock:UpdateGuardrail",
]
_RESOURCE_TAG = f"aws:ResourceTag/{STAGE_TAG_KEY}"
_REQUEST_TAG = f"aws:RequestTag/{STAGE_TAG_KEY}"

_MAIN_TOPIC_ALLOWED = [
    "sns:GetSubscriptionAttributes",
    "sns:GetTopicAttributes",
    "sns:SetSubscriptionAttributes",
    "sns:Subscribe",
    "sns:Unsubscribe",
]
_SERVICE_LINKED = [
    "apigateway.amazonaws.com",
    "bedrock-agentcore.amazonaws.com",
    "events.amazonaws.com",
    "lambda.amazonaws.com",
    "logs.amazonaws.com",
    "states.amazonaws.com",
]

_COMPLETE_ARN = re.compile(r"^arn:[^:\s]+:[^:\s]*:[^:\s]*:[^:\s]*:.+")


def policy_arn(account: str, name: str) -> str:
    """Managed policy ARN in this account. The name is fixed."""

    return f"arn:aws:iam::{account}:policy/{name}"


def nonwhitespace_size(document: dict[str, Any]) -> int:
    """IAM managed-policy quota is 6,144 characters excluding whitespace."""

    return len(json.dumps(document, separators=(",", ":")))


def execution_policy(account: str, region: str) -> dict[str, Any]:
    """Identity policy for the dev CloudFormation execution role."""

    boundary = policy_arn(account, BOUNDARY_POLICY_NAME)
    document = {
        "Version": "2012-10-17",
        "Statement": [
            _allow("DevNames", _NAMED_SERVICE_ACTIONS, _service_dev_patterns(account)),
            _allow("DevS3", "s3:*", "arn:aws:s3:::*-dev-*"),
            _allow("AssetRead", ["s3:Get*", "s3:List*"], _asset_bucket_arns(account, region)),
            _allow("MainTopic", _MAIN_TOPIC_ALLOWED, _main_topic(account, region)),
            _allow(
                "Unscoped",
                _EXEC_STAR_ACTIONS,
                "*",
            ),
            _kms_allow(account, region),
            _allow(
                "CreateDevRoleWithBoundary",
                _ROLE_CREATE_ACTIONS,
                _role_pattern(account),
                condition={"StringEquals": {_BOUNDARY_KEY: boundary}},
            ),
            _allow(
                "MutateDevRoleWhenBoundaryPresent",
                _ROLE_MUTATION_ACTIONS,
                _role_pattern(account),
                condition={"StringEqualsIfExists": {_BOUNDARY_KEY: boundary}},
            ),
            _allow(
                "DeleteAndReadDevRoles",
                [
                    "iam:DeleteRole",
                    "iam:DeleteRolePolicy",
                    "iam:DetachRolePolicy",
                    "iam:GetRole",
                    "iam:GetRolePolicy",
                    "iam:ListAttachedRolePolicies",
                    "iam:ListRolePolicies",
                    "iam:ListRoleTags",
                    "iam:UntagRole",
                    "iam:UpdateRole",
                    "iam:UpdateRoleDescription",
                ],
                _role_pattern(account),
            ),
            _allow(
                "PassDevRoles",
                ["iam:PassRole"],
                _role_pattern(account),
                condition={"StringEquals": {"iam:PassedToService": _PASS_TO_SERVICES}},
            ),
            _allow(
                "CreateServiceLinkedRole",
                ["iam:CreateServiceLinkedRole"],
                f"arn:aws:iam::{account}:role/aws-service-role/*",
                condition={"StringLike": {"iam:AWSServiceName": _SERVICE_LINKED}},
            ),
            *_tag_denies(),
        ],
    }
    _check_size(document, EXEC_POLICY_NAME)
    return document


def execution_deny_policy(account: str) -> dict[str, Any]:
    """Deny half of the execution role. Attached beside ``execution_policy``.

    The allows no longer fit in one managed policy. A deny in either policy
    still wins. ``NoPolicyEdit`` names both execution policies and the boundary.
    """

    document = {
        "Version": "2012-10-17",
        "Statement": _exec_denies(account),
    }
    _check_size(document, EXEC_DENY_POLICY_NAME)
    return document


def boundary_policy(account: str, region: str) -> dict[str, Any]:
    """Permissions boundary for dev roles and the execution role."""

    document = {
        "Version": "2012-10-17",
        "Statement": [
            _allow("AllowAll", "*", "*"),
            _outside_dev_deny(account, region),
            {
                "Sid": "NoMainTopicWrite",
                "Effect": "Deny",
                "NotAction": _MAIN_TOPIC_ALLOWED,
                "Resource": _main_topic(account, region),
            },
            *_tag_denies(),
            *_boundary_denies(account),
            _sts_deny(account),
        ],
    }
    _check_size(document, BOUNDARY_POLICY_NAME)
    return document


def tag_dev_stack(stack: Stack) -> None:
    """Tag every taggable resource in one dev application stack."""

    Tags.of(stack).add(STAGE_TAG_KEY, STAGE_TAG_VALUE)


def apply_to_stack(stack: Stack) -> None:
    """Attach the workload boundary to every role in one dev application stack.

    ``PermissionsBoundary`` visits ``CfnRole``, including roles CDK builds for
    custom resources, BucketDeployment, and log retention. The policy is an
    imported name, so the application stack does not depend on ``OerfCiStack``.
    """

    boundary = iam.ManagedPolicy.from_managed_policy_name(
        stack,
        "DevWorkloadBoundary",
        BOUNDARY_POLICY_NAME,
    )
    iam.PermissionsBoundary.of(stack).apply(boundary)


def boundary_permits(
    action: str,
    resource: str,
    account: str,
    region: str,
    context: dict[str, str] | None = None,
) -> bool:
    """True when the workload boundary does not deny this call."""

    return not _any_deny(
        boundary_policy(account, region)["Statement"],
        action,
        resource,
        context or {},
    )


def intersection_allows(
    action: str,
    resource: str,
    account: str,
    region: str,
    context: dict[str, str] | None = None,
) -> bool:
    """True when the execution policy and the boundary both allow the call."""

    current = context or {}
    identities = [
        execution_policy(account, region)["Statement"],
        execution_deny_policy(account)["Statement"],
    ]
    boundary = boundary_policy(account, region)["Statement"]
    if any(_any_deny(identity, action, resource, current) for identity in identities):
        return False
    if _any_deny(boundary, action, resource, current):
        return False
    return any(_any_allow(identity, action, resource, current) for identity in identities)


def runtime_violations(
    template: dict[str, Any],
    account: str,
    region: str,
) -> list[str]:
    """Role-policy actions the workload boundary would deny.

    A same-stack ``Ref`` or ``GetAtt`` is dev-scoped. A finished ARN string is
    checked against the boundary. ``*`` is allowed only for ``STAR_ACTION_REASONS``.
    """

    problems: list[str] = []
    for logical_id, resource in template.get("Resources", {}).items():
        if resource.get("Type") != "AWS::IAM::Policy":
            continue
        document = resource.get("Properties", {}).get("PolicyDocument", {})
        for statement in _statements(document):
            if statement.get("Effect", "Allow") == "Deny":
                continue
            for action in _as_list(statement.get("Action")):
                for raw in _as_list(statement.get("Resource")):
                    problems.extend(
                        _resource_problems(logical_id, action, raw, account, region)
                    )
    return problems


def _resource_problems(
    logical_id: str,
    action: str,
    raw: Any,
    account: str,
    region: str,
) -> list[str]:
    problems: list[str] = []
    for value in _concrete_resources(raw):
        probes = _action_probes(action)
        if not all(boundary_permits(probe, value, account, region) for probe in probes):
            problems.append(f"{logical_id} {action} {value}")
    return problems


def _concrete_resources(raw: Any) -> list[str]:
    """ARN strings and ``*`` to check. Fragments and tokens are same-stack."""

    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if node == "*":
                found.append(node)
            elif _COMPLETE_ARN.match(node) and not node.endswith(":"):
                found.append(node)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if isinstance(node, dict):
            for item in node.values():
                walk(item)

    walk(raw)
    return found


def _action_probes(action: str) -> list[str]:
    if "*" not in action and "?" not in action:
        return [action]
    return [action.replace("*", ""), action.replace("*", "X")]


_NAMED_SERVICE_ACTIONS = [
    "bedrock-agentcore:*",
    "cloudwatch:*",
    "dynamodb:*",
    "ecr:*",
    "events:*",
    "lambda:*",
    "logs:*",
    "sns:*",
    "sqs:*",
    "ssm:*",
    "states:*",
]


# Services whose physical names contain ``-dev-`` (``oerf-dev-*`` and
# ``Oerf*Stack-dev-*``). SSM uses ``/oerf/dev/``. AgentCore and its vended
# log group use ``oerf_dev_agent``. Log delivery drops the hyphen
# (``Oerf*Stackdev``). The bootstrap asset repository is ``cdk-oerfdev-*``.
_HYPHEN_SERVICES = (
    "cloudwatch",
    "dynamodb",
    "events",
    "lambda",
    "logs",
    "sns",
    "sqs",
    "states",
)


def dev_name_patterns(account: str) -> list[str]:
    """Dev ARN patterns. None of these match ``oerf-device-seed``.

    Each pattern names one service. IAM rejects a wildcard in the service
    segment, so ``arn:aws:*:*:account:*-dev-*`` is not a valid resource.
    ``-dev-`` covers ``oerf-dev-*``, ``Oerf*Stack-dev-*`` and ``oerf*stack-dev-*``.
    ``_dev_`` covers ``oerf_dev_agent``. ``/dev/`` covers SSM ``/oerf/dev/*``.
    ``Stackdev`` covers log-delivery names that drop the hyphen.
    ``oerfdev`` covers ``cdk-oerfdev-*``. Roles stay on ``role/*-dev-*``.
    S3 has no account field, so its patterns are separate. ``s3:*`` is not
    granted on ``*oerfdev*``; that pattern only keeps the asset bucket inside
    the boundary. The execution role reads it with ``s3:Get*`` and ``s3:List*``.
    """

    hyphen = [f"arn:aws:{service}:*:{account}:*-dev-*" for service in _HYPHEN_SERVICES]
    return [
        *hyphen,
        f"arn:aws:logs:*:{account}:*_dev_*",
        f"arn:aws:logs:*:{account}:*Stackdev*",
        f"arn:aws:bedrock-agentcore:*:{account}:*_dev_*",
        f"arn:aws:ecr:*:{account}:*oerfdev*",
        f"arn:aws:ssm:*:{account}:parameter/oerf/dev/*",
        "arn:aws:s3:::*-dev-*",
        "arn:aws:s3:::*oerfdev*",
        _role_pattern(account),
    ]


def _service_dev_patterns(account: str) -> list[str]:
    """Name patterns for the execution role's service allows. Not S3 or IAM."""

    return [
        pattern
        for pattern in dev_name_patterns(account)
        if not pattern.startswith("arn:aws:s3:::") and ":iam:" not in pattern
    ]


def _outside_dev_deny(account: str, region: str) -> dict[str, Any]:
    """Deny every action that is not unscoped, on every resource that is not dev or shared.

    ``NotAction`` holds APIs whose request resource is ``*`` or an id with no
    stage name, plus the tag-gated Bedrock guardrail actions. ``NotResource``
    holds dev names and the shared ARNs. A bare ``*`` is not a NotResource
    entry: in IAM that would match every resource.
    """

    return {
        "Sid": "NoOutsideDev",
        "Effect": "Deny",
        "NotAction": sorted(set(STAR_ACTION_REASONS) | set(_BEDROCK_TAG_ACTIONS)),
        "NotResource": _not_resources(account, region),
    }


def _not_resources(account: str, region: str) -> list[str]:
    """Dev names plus shared ARNs the name styles do not cover."""

    runtime_logs = (
        f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*"
    )
    return [
        *dev_name_patterns(account),
        _main_topic(account, region),
        _arn("events", region, account, f"rule/{_SHARED_EVENTS_RULE}"),
        runtime_logs,
        runtime_logs + ":*",
        _arn("execute-api", region, account, "*"),
        "arn:aws:bedrock:*::foundation-model/*",
        "arn:aws:bedrock:*:*:guardrail-profile/*",
        _arn("bedrock-agentcore", region, account, "workload-identity-directory/*"),
        _arn("social-messaging", region, account, "phone-number-id/*"),
    ]


def _tag_denies() -> list[dict[str, Any]]:
    """Deny Cognito, API Gateway, CloudFront and Bedrock guardrails without the dev tag.

    Existing resources use ``aws:ResourceTag``. Create calls use ``aws:RequestTag``,
    and only on the create ARN, so a child create under an already tagged API is
    judged by the parent resource tag. ``StringNotEqualsIfExists`` denies a missing
    key. ``AdminCreateUser`` is denied outright: it supports only the resource tag,
    so a copied dev tag would otherwise authorize user creation in any pool.

    ``cognito-idp:*``, ``apigatewayv2:*`` and ``cloudfront:*`` already include
    ``TagResource`` and ``UntagResource``. Cognito and CloudFront authorize
    those on the user pool and the distribution, and both support
    ``aws:ResourceTag``. API Gateway V2 has no ``TagResource`` action; tagging
    a REST or HTTP API is ``apigateway:*`` on ``/tags/*``, which does not
    support ``aws:ResourceTag`` and is denied outright. ``bedrock:*Guardrail*``
    does not match ``bedrock:TagResource``, so those two actions are listed.
    They support ``aws:ResourceTag`` on ``guardrail/*``.

    Actions with no tag condition key stay allowed and are residuals:
    Cognito ``DescribeUserPoolDomain`` and ``ListUserPools``; API Gateway
    ``GET`` on ``/account``; CloudFront ``ListDistributions`` and
    ``CreateOriginAccessControl`` (update and delete of an origin access
    control are denied outright); Bedrock ``InvokeModel`` on a foundation
    model, which cannot carry a customer tag. ``ApplyGuardrail`` on
    ``guardrail-profile/*`` has no tag key. The guardrail id itself is
    tag-checked.
    """

    missing_resource_tag = {"StringNotEqualsIfExists": {_RESOURCE_TAG: STAGE_TAG_VALUE}}
    missing_request_tag = {"StringNotEqualsIfExists": {_REQUEST_TAG: STAGE_TAG_VALUE}}
    return [
        {
            "Sid": "DenyExistingUnlessDevTag",
            "Effect": "Deny",
            "Action": [
                "cognito-idp:*",
                "apigateway:*",
                "apigatewayv2:*",
                "cloudfront:*",
                "bedrock:*Guardrail*",
                "bedrock:TagResource",
                "bedrock:UntagResource",
            ],
            "Resource": [
                "arn:aws:cognito-idp:*:*:userpool/*",
                "arn:aws:apigateway:*::/restapis/*",
                "arn:aws:apigateway:*::/apis/*",
                "arn:aws:cloudfront::*:distribution/*",
                "arn:aws:bedrock:*:*:guardrail/*",
            ],
            "Condition": missing_resource_tag,
        },
        {
            "Sid": "DenyCreateUnlessDevTag",
            "Effect": "Deny",
            "Action": [
                "cognito-idp:CreateUserPool",
                "apigatewayv2:CreateApi",
                "apigatewayv2:ImportApi",
                "apigatewayv2:ReimportApi",
                "cloudfront:CreateDistribution",
                "bedrock:CreateGuardrail",
            ],
            "Resource": "*",
            "Condition": missing_request_tag,
        },
        {
            "Sid": "DenyApiCreateUnlessDevTag",
            "Effect": "Deny",
            "Action": "apigateway:POST",
            "Resource": [
                "arn:aws:apigateway:*::/restapis",
                "arn:aws:apigateway:*::/apis",
            ],
            "Condition": missing_request_tag,
        },
        {
            "Sid": "DenyAdminCreateUser",
            "Effect": "Deny",
            "Action": "cognito-idp:AdminCreateUser",
            "Resource": "*",
        },
    ]


def _arn(service: str, region: str, account: str, resource: str) -> str:
    return f"arn:aws:{service}:{region}:{account}:{resource}"


def _exec_denies(account: str) -> list[dict[str, Any]]:
    """Denies the task requires on the execution policy itself.

    The boundary repeats these and adds the role-creation condition. The
    execution role carries that boundary, so a missing condition key still
    fails closed there.
    """

    return [item for item in _boundary_denies(account) if item["Sid"] in _EXEC_DENY_SIDS]


_EXEC_DENY_SIDS = {
    "DenyApiGatewayTags",
    "DenyOriginAccessControlWrite",
    "NoBoundaryDelete",
    "NoPolicyEdit",
    "NoUsers",
    "NoProtectedRoles",
    "NoBroadAttach",
    "NoOrgs",
}


def _boundary_denies(account: str) -> list[dict[str, Any]]:
    boundary = policy_arn(account, BOUNDARY_POLICY_NAME)
    protected = [
        f"arn:aws:iam::{account}:policy/{BOUNDARY_POLICY_NAME}",
        f"arn:aws:iam::{account}:policy/{EXEC_POLICY_NAME}",
        f"arn:aws:iam::{account}:policy/{EXEC_DENY_POLICY_NAME}",
    ]
    return [
        *_outright_denies(),
        {
            "Sid": "NoBoundaryDelete",
            "Effect": "Deny",
            "Action": [
                "iam:DeleteRolePermissionsBoundary",
                "iam:DeleteUserPermissionsBoundary",
            ],
            "Resource": "*",
        },
        {
            "Sid": "NoPolicyEdit",
            "Effect": "Deny",
            "Action": [
                "iam:CreatePolicy",
                "iam:CreatePolicyVersion",
                "iam:DeletePolicy",
                "iam:DeletePolicyVersion",
                "iam:SetDefaultPolicyVersion",
            ],
            "Resource": protected,
        },
        {
            "Sid": "NoUsers",
            "Effect": "Deny",
            "Action": [
                "iam:CreateAccessKey",
                "iam:CreateLoginProfile",
                "iam:CreateUser",
            ],
            "Resource": "*",
        },
        {
            "Sid": "NoProtectedRoles",
            "Effect": "Deny",
            "Action": "iam:*",
            "Resource": [
                f"arn:aws:iam::{account}:role/cdk-hnb659fds-*",
                f"arn:aws:iam::{account}:role/cdk-oerfdev-*",
                f"arn:aws:iam::{account}:role/OerfGitHubDeployRole",
                f"arn:aws:iam::{account}:role/OerfGitHubDeployRole-dev",
            ],
        },
        {
            "Sid": "NoRoleWithoutBoundary",
            "Effect": "Deny",
            "Action": _ROLE_CREATE_ACTIONS,
            "Resource": "*",
            "Condition": {"StringNotEqualsIfExists": {_BOUNDARY_KEY: boundary}},
        },
        {
            "Sid": "NoCreatePolicy",
            "Effect": "Deny",
            "Action": "iam:CreatePolicy",
            "Resource": [f"arn:aws:iam::{account}:policy/*", "*"],
        },
        {
            "Sid": "NoIamOutsideDev",
            "Effect": "Deny",
            "Action": list(
                dict.fromkeys(
                    [
                        *_ROLE_MUTATIONS,
                        "iam:DeleteRole",
                        "iam:DeleteRolePolicy",
                        "iam:DetachRolePolicy",
                        "iam:PassRole",
                        "iam:UpdateRole",
                    ]
                )
            ),
            "NotResource": [_role_pattern(account)],
        },
        {
            "Sid": "NoBroadAttach",
            "Effect": "Deny",
            "Action": ["iam:AttachRolePolicy", "iam:AttachUserPolicy"],
            "Resource": "*",
            "Condition": {"ArnEquals": {"iam:PolicyARN": _BROAD_MANAGED_POLICIES}},
        },
        {
            "Sid": "NoOrgs",
            "Effect": "Deny",
            "Action": ["account:*", "organizations:*"],
            "Resource": "*",
        },
    ]


def _outright_denies() -> list[dict[str, Any]]:
    """Denies for actions that do not support ``aws:ResourceTag``.

    The API Gateway tags resource accepts ``aws:RequestTag`` only, so a tag
    condition cannot stop a write that marks a main API as dev. Origin access
    controls are not taggable. Create stays allowed; update and delete do not,
    so removing the dev API stack cannot delete that control.
    """

    return [
        {
            "Sid": "DenyApiGatewayTags",
            "Effect": "Deny",
            "Action": "apigateway:*",
            "Resource": "arn:aws:apigateway:*::/tags/*",
        },
        {
            "Sid": "DenyOriginAccessControlWrite",
            "Effect": "Deny",
            "Action": [
                "cloudfront:UpdateOriginAccessControl",
                "cloudfront:DeleteOriginAccessControl",
            ],
            "Resource": "*",
        },
    ]


def _sts_deny(account: str) -> dict[str, Any]:
    """Dev roles may be assumed. Bootstrap and GitHub roles may not.

    The GitHub deploy role is not under this boundary. It assumes the four
    ``cdk-oerfdev`` publishing and deploy roles. Nothing created by a dev
    stack needs to assume those roles, so they stay outside NotResource.
    ``OerfGitHubDeployRole-dev`` does not match ``*-dev-*`` (no trailing suffix).
    """

    return {
        "Sid": "NoAssumeOutsideDev",
        "Effect": "Deny",
        "Action": ["sts:AssumeRole", "sts:AssumeRoleWithWebIdentity"],
        "NotResource": [_role_pattern(account)],
    }


def _kms_allow(account: str, region: str) -> dict[str, Any]:
    """Read the oerfdev asset bucket key. The key id is generated.

    The key policy already requires ``kms:ViaService`` S3. This statement
    also requires the bootstrap alias so the execution role cannot use
    another key. The alias condition is omitted from the boundary because
    the key id is not a name; the identity policy is what scopes the call.
    """

    return {
        "Sid": "DecryptBootstrapAssetKey",
        "Effect": "Allow",
        "Action": ["kms:Decrypt", "kms:DescribeKey"],
        "Resource": f"arn:aws:kms:{region}:{account}:key/*",
        "Condition": {
            "ForAnyValue:StringEquals": {"kms:ResourceAliases": _ASSET_KEY_ALIAS},
            "StringEquals": {"kms:ViaService": f"s3.{region}.amazonaws.com"},
        },
    }


def _allow(
    sid: str,
    action: str | list[str],
    resource: str | list[str],
    condition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    statement: dict[str, Any] = {
        "Sid": sid,
        "Effect": "Allow",
        "Action": action,
        "Resource": resource,
    }
    if condition:
        statement["Condition"] = condition
    return statement


def _role_pattern(account: str) -> str:
    return f"arn:aws:iam::{account}:role/*-dev-*"


def _main_topic(account: str, region: str) -> str:
    return f"arn:aws:sns:{region}:{account}:{_MAIN_INBOUND_TOPIC_SUFFIX}"


def _asset_bucket_arns(account: str, region: str) -> list[str]:
    bucket = f"arn:aws:s3:::cdk-oerfdev-assets-{account}-{region}"
    return [bucket, f"{bucket}/*"]


def _check_size(document: dict[str, Any], name: str) -> None:
    size = nonwhitespace_size(document)
    if size > _MANAGED_POLICY_LIMIT:
        raise ValueError(f"{name} is {size} non-whitespace characters (limit 6144)")


def _statements(document: Any) -> list[dict[str, Any]]:
    if isinstance(document, str):
        document = json.loads(document)
    raw = document.get("Statement", []) if isinstance(document, dict) else []
    if isinstance(raw, dict):
        return [raw]
    return [item for item in raw if isinstance(item, dict)]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _any_deny(
    statements: list[dict[str, Any]],
    action: str,
    resource: str,
    context: dict[str, str],
) -> bool:
    return any(
        _matches(item, action, resource, context)
        for item in statements
        if item.get("Effect") == "Deny"
    )


def _any_allow(
    statements: list[dict[str, Any]],
    action: str,
    resource: str,
    context: dict[str, str],
) -> bool:
    return any(
        _matches(item, action, resource, context)
        for item in statements
        if item.get("Effect", "Allow") == "Allow"
    )


def _matches(
    statement: dict[str, Any],
    action: str,
    resource: str,
    context: dict[str, str],
) -> bool:
    if not _action_matches(statement, action):
        return False
    if not _resource_matches(statement, resource):
        return False
    return _condition_matches(statement.get("Condition"), context)


def _action_matches(statement: dict[str, Any], action: str) -> bool:
    if "NotAction" in statement:
        return not any(_glob(pattern, action) for pattern in _as_list(statement["NotAction"]))
    if "Action" not in statement:
        return False
    return any(_glob(pattern, action) for pattern in _as_list(statement["Action"]))


def _resource_matches(statement: dict[str, Any], resource: str) -> bool:
    if "Resource" in statement and not any(
        _glob(pattern, resource) for pattern in _as_list(statement["Resource"])
    ):
        return False
    if "NotResource" in statement and any(
        _glob(pattern, resource) for pattern in _as_list(statement["NotResource"])
    ):
        return False
    return "Resource" in statement or "NotResource" in statement


def _condition_matches(condition: Any, context: dict[str, str]) -> bool:
    if not condition:
        return True
    for operator, checks in condition.items():
        if not isinstance(checks, dict):
            return False
        name, quantifier = _operator_name(operator)
        if_exists = name.endswith("ifexists")
        base = name[: -len("ifexists")] if if_exists else name
        for key, expected in checks.items():
            values = [str(item) for item in _as_list(expected)]
            if key not in context:
                if if_exists or base == "null":
                    if base == "null" and values != ["true"]:
                        return False
                    continue
                return False
            actual = context[key]
            if base == "null":
                return False
            matched = _value_matches(base, actual, values)
            if quantifier == "foranyvalue":
                ok = matched
            elif quantifier == "forallvalues":
                ok = matched
            else:
                ok = matched
            if not ok:
                return False
    return True


def _operator_name(operator: str) -> tuple[str, str]:
    quantifier = ""
    name = operator.lower()
    if ":" in operator:
        prefix, name = operator.split(":", 1)
        quantifier = prefix.lower()
        name = name.lower()
    return name, quantifier


def _value_matches(base: str, actual: str, values: list[str]) -> bool:
    if base in {"stringequals", "arnequals"}:
        return actual in values
    if base in {"stringnotequals", "arnnotequals"}:
        return actual not in values
    if base in {"stringlike", "arnlike"}:
        return any(_glob(pattern, actual) for pattern in values)
    if base in {"stringnotlike", "arnnotlike"}:
        return all(not _glob(pattern, actual) for pattern in values)
    raise ValueError(f"unsupported IAM condition operator {base}")


def _glob(pattern: str, value: str) -> bool:
    """IAM ``*`` and ``?`` matching. ``*`` crosses ``/`` and ``:``."""

    return fnmatch.fnmatchcase(value, pattern.replace("[", "[[]"))
