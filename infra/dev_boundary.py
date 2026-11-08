"""Dev CloudFormation execution policy and workload permissions boundary.

``OerfDevCfnExecPolicy``, ``OerfDevCfnExecPolicy2``, and ``OerfDevCfnExecDenies``
replace AdministratorAccess on ``cdk-oerfdev-cfn-exec-role``. Dev name patterns
and star-resource allows do not fit in one managed policy, so allows are split
across two identity policies and the denies are the third.
``OerfDevWorkloadBoundary`` is attached to that role by the bootstrap and to
every role the four dev application stacks
create. The documents are built from the stack account and region.

The boundary is ``Allow *`` plus a ``Deny`` on anything that is not a dev name,
a documented shared ARN, or a call whose resource is ``*`` because the API has
no resource type. Dev names are ``-dev-``, ``_dev_``, ``/dev/``, ``Stackdev``
and ``oerfdev``. They do not match a main name that merely contains ``dev``,
such as ``oerf-device-seed``. IAM does not allow a wildcard in the service
segment, so each style is written on the services that use it. Unscoped APIs
allowed by the name deny are listed in ``STAR_ACTION_REASONS``. Cognito, API Gateway and
CloudFront are denied unless the resource or the create request carries
``oerf:stage=dev``. Bedrock guardrail calls are denied unless the resource
is the dev guardrail ARN from context ``dev:guardrailArn``, a version of
that ARN, or this account's ``guardrail-profile/*``. ``CreateGuardrail``
stays on the request tag: the new id is not known yet.
"""

from __future__ import annotations

import json
import re
from typing import Any

from aws_cdk import Stack, Tags, aws_iam as iam

EXEC_POLICY_NAME = "OerfDevCfnExecPolicy"
EXEC_POLICY2_NAME = "OerfDevCfnExecPolicy2"
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
_DEV_BOOTSTRAP_QUALIFIER = "oerfdev"
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
    "dynamodb.amazonaws.com",
    "events.amazonaws.com",
    "lambda.amazonaws.com",
    "sns.amazonaws.com",
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
_LAMBDA_ENV_KEY_ALIAS = "alias/aws/lambda"
# Customer CMKs are out of scope for dev; block key administration on any key.
_KMS_KEY_ADMIN_DENY_ACTIONS: tuple[str, ...] = (
    "kms:Schedule*",
    "kms:PutKeyPolicy",
    "kms:Disable*",
    "kms:Delete*",
    "kms:Update*",
    "kms:CreateKey",
    "kms:CreateAlias",
)

# Resource "*" or unscoped actions exempted from the name-based deny (NOD).
# Rationale per action: docs/adr and STAR_ACTION_REASONS in git history.
STAR_ACTION_REASONS: list[str] = [
    "apigateway:*",
    "apigatewayv2:*",
    "cloudfront:*",
    "cloudwatch:DescribeAlarms",
    "cloudwatch:PutMetricData",
    "cognito-idp:*",
    "dynamodb:ListStreams",
    "ecr:GetAuthorizationToken",
    "lambda:*EventSourceMapping*",
    "logs:*LogDeliver*",
    "logs:*ResourcePolic*",
    "logs:Describe*",
    "sns:Publish",
    "states:SendTaskSuccess",
    "xray:*",
    "kinesis:*",
    "firehose:TagDeliveryStream",
    "bedrock:CreateGuardrail",
    "ec2:Describe*",
    "iam:ListRoles",
    "bedrock:ListTagsForResource",
    "bedrock-agentcore:CreateAgentRuntime",
    "s3:REST.PUT.OBJECT",
]

_OUTSIDE_DEV_NOT_ACTION_EXTRA: tuple[str, ...] = (
    "kms:*",
    "iam:CreateServiceLinkedRole",
)

# Granted to the execution role on "*". Workload-only star actions stay off
# this list so CloudFormation cannot call them.
_EXEC_STAR_ACTIONS = [
    "apigateway:*",
    "apigatewayv2:*",
    "cloudfront:*",
    "cloudwatch:DescribeAlarms",
    "cognito-idp:*",
    "ecr:GetAuthorizationToken",
    "lambda:*EventSourceMapping*",
    "logs:*LogDeliver*",
    "logs:*ResourcePolic*",
    "logs:Describe*",
    "iam:ListRoles",
    "bedrock:CreateGuardrail",
    "bedrock:CreateGuardrailVersion",
    "bedrock:DeleteGuardrail",
    "bedrock:GetGuardrail",
    "bedrock:ListGuardrails",
    "bedrock:ListTagsForResource",
    "bedrock:TagResource",
    "bedrock:UntagResource",
    "bedrock:UpdateGuardrail",
    "bedrock-agentcore:CreateAgentRuntime",
    "kms:ListAliases",
    "s3:REST.PUT.OBJECT",
]

# One line per ``_EXEC_STAR_ACTIONS`` entry (Resource ``*``); keep in sync with U.
_EXEC_STAR_REASONS: dict[str, str] = {
    "apigateway:*": "REST control plane has no stage-scoped ARN until create returns an id",
    "apigatewayv2:*": "HTTP/WebSocket control plane tags are separate; create has no id yet",
    "cloudfront:*": "Tag-unfriendly reads and OAC lifecycle; fenced elsewhere where needed",
    "cloudwatch:DescribeAlarms": "Describe has no resource-scoped denial matching dev names",
    "cognito-idp:*": "User pool CRUD is not expressible as a single dev ARN pattern in NotResource",
    "ecr:GetAuthorizationToken": "ECR auth has no resource ARN",
    "lambda:*EventSourceMapping*": (
        "Event source mapping ARNs are not folded into dev name patterns"
    ),
    "logs:*LogDeliver*": "Cross-service log delivery control plane uses unscopeable ARNs",
    "logs:*ResourcePolic*": "Account-wide logs resource policies are not dev-name scoped",
    "logs:Describe*": "Describe* matches names outside strict dev ARN forms",
    "iam:ListRoles": "ListRoles is account-level; used by IAM::Role handler list operation",
    "bedrock:CreateGuardrail": "Create authorizes on guardrail-profile; new guardrail id unknown",
    "bedrock:CreateGuardrailVersion": "Version create follows same unscoped pattern as create",
    "bedrock:DeleteGuardrail": "Service lists guardrail APIs as authorizing on resource *",
    "bedrock:GetGuardrail": "Same as delete for read path on handler",
    "bedrock:ListGuardrails": "List is account-wide in the service model",
    "bedrock:ListTagsForResource": "Handler list-tags path per resource",
    "bedrock:TagResource": "Tagging dev guardrail after create",
    "bedrock:UntagResource": "Untag on dev guardrail",
    "bedrock:UpdateGuardrail": "Update on dev guardrail",
    "bedrock-agentcore:CreateAgentRuntime": (
        "Runtime id unknown until CreateAgentRuntime returns"
    ),
    "kms:ListAliases": "Alias listing is account-wide during CMK setup",
    "s3:REST.PUT.OBJECT": "Vended log delivery PutObject uses unscoped auth",
}


def exec_policy_star_resource_actions() -> list[str]:
    """Actions OerfDevCfnExecPolicy allows on ``Resource`` ``*`` (statement ``U``)."""

    return list(_EXEC_STAR_ACTIONS)


def exec_policy_star_action_reasons() -> list[tuple[str, str]]:
    """(action pattern, rationale) pairs for statement ``U`` allows on ``*``."""

    out: list[tuple[str, str]] = []
    for action in _EXEC_STAR_ACTIONS:
        reason = _EXEC_STAR_REASONS.get(action)
        if reason is None:
            raise ValueError(f"add _EXEC_STAR_REASONS entry for {action!r}")
        out.append((action, reason))
    return out


# Additional execution allows on ``*`` (separate Sids from statement ``U``).
_EXEC_STAR_RESOURCE_EXTRA: tuple[tuple[str, str, str], ...] = (
    (
        "EC2D",
        "ec2:Describe*",
        (
            "Lambda and AgentCore handlers describe VPCs/subnets/security groups "
            "without a single dev ARN"
        ),
    ),
    (
        "KFH",
        "kinesis:*",
        "DynamoDB table Kinesis streaming destination uses a dev-named stream ARN",
    ),
    (
        "KFH",
        "firehose:TagDeliveryStream",
        "CloudWatch Logs subscription delivery may tag a dev-named Firehose delivery stream",
    ),
)


def exec_policy_resource_star_rows() -> list[tuple[str, str, str]]:
    """(statement Sid, action pattern, rationale) for every Allow on Resource ``*``."""

    rows = [("U", action, reason) for action, reason in exec_policy_star_action_reasons()]
    rows.extend(_EXEC_STAR_RESOURCE_EXTRA)
    return rows


# Excluded from the name deny. The guardrail ARN deny fences these except
# CreateGuardrail, which stays on aws:RequestTag. ApplyGuardrail is
# workload-only, so it is not in the execution allow.
# Do not replace with ``bedrock:*Guardrail*``: it would exempt future
# account-level guardrail-check APIs on ``*`` that stay outside ``NotResource``.
_GUARDRAIL_EXEMPT_ACTIONS = [
    "bedrock:ApplyGuardrail",
    "bedrock:CreateGuardrailVersion",
    "bedrock:DeleteGuardrail",
    "bedrock:GetGuardrail",
    "bedrock:ListGuardrails",
    "bedrock:*agResource",
    "bedrock:UpdateGuardrail",
]
# ``bedrock:*Guardrail*`` except CreateGuardrail, plus tag and untag.
# One statement cannot combine Action and NotAction, so CreateGuardrail is
# omitted here instead of subtracted. Account-level actions whose names
# contain Guardrail (enforced configuration, InvokeGuardrailChecks) authorize
# on ``*`` and stay under NoOutsideDev: a NotResource list cannot exempt ``*``.
_GUARDRAIL_ARN_ACTIONS = [
    "bedrock:ApplyGuardrail",
    "bedrock:CreateGuardrailVersion",
    "bedrock:DeleteGuardrail",
    "bedrock:GetGuardrail",
    "bedrock:ListTagsForResource",
    "bedrock:*agResource",
    "bedrock:UpdateGuardrail",
]


def _outside_dev_not_actions() -> list[str]:
    """Stable union for NOD ``NotAction`` without per-entry doc strings."""

    merged = list(
        dict.fromkeys(
            [
                *STAR_ACTION_REASONS,
                *_GUARDRAIL_EXEMPT_ACTIONS,
                *_OUTSIDE_DEV_NOT_ACTION_EXTRA,
            ]
        )
    )
    return _compact_action_patterns(merged)


def service_reference_iam_actions() -> list[str]:
    """Every ``service`` / ``prefix:Action`` pair in ``service-reference.json``."""

    from infra.service_reference_probes import _load_service_reference_blob

    blob = _load_service_reference_blob()
    actions: list[str] = []
    for stem, doc in blob.items():
        if not isinstance(doc, dict):
            continue
        for entry in doc.get("Actions", []):
            name = entry.get("Name")
            if not isinstance(name, str) or not name:
                continue
            if stem == "s3":
                actions.append(f"s3:{name}")
            else:
                actions.append(f"{stem}:{name}")
    return actions


def notaction_exemption_patterns() -> list[str]:
    """All ``NotAction`` lists (NOD union and main-topic NMT)."""

    return [*_outside_dev_not_actions(), *_MAIN_TOPIC_ALLOWED]


def _service_reference_iam_actions() -> list[str]:
    return service_reference_iam_actions()


def _compact_action_patterns(patterns: list[str]) -> list[str]:
    """Drop NotAction entries another entry already subsumes (service-reference safe)."""

    actions = _service_reference_iam_actions()
    keep: list[str] = []
    for index, pattern in enumerate(patterns):
        matched = [action for action in actions if _glob(pattern, action)]
        if not matched:
            keep.append(pattern)
            continue
        subsumed = False
        for other_index, other in enumerate(patterns):
            if other_index == index:
                continue
            if all(_glob(other, action) for action in matched):
                subsumed = True
                break
        if not subsumed:
            keep.append(pattern)
    return keep


GUARDRAIL_ARN_CONTEXT = "dev:guardrailArn"
_GUARDRAIL_ARN_RE = re.compile(
    r"^arn:aws:bedrock:(?P<region>[a-z0-9-]+):(?P<account>\d{12}):guardrail/(?P<guardrail_id>[0-9a-z]+)$"
)
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
    "cognito-idp.amazonaws.com",
    "dynamodb.amazonaws.com",
    "events.amazonaws.com",
    "lambda.amazonaws.com",
    "logs.amazonaws.com",
    "sns.amazonaws.com",
    "states.amazonaws.com",
]

_COMPLETE_ARN = re.compile(r"^arn:[^:\s]+:[^:\s]*:[^:\s]*:[^:\s]*:.+")


def policy_arn(account: str, name: str) -> str:
    """Managed policy ARN in this account. The name is fixed."""

    return f"arn:aws:iam::{account}:policy/{name}"


def workload_permissions_boundary_import(account: str) -> dict[str, Any]:
    """CDK shape for ``PermissionsBoundary`` on roles (partition token)."""

    return {
        "Fn::Join": [
            "",
            [
                "arn:",
                {"Ref": "AWS::Partition"},
                f":iam::{account}:policy/{BOUNDARY_POLICY_NAME}",
            ],
        ]
    }


def is_workload_permissions_boundary(value: object, account: str) -> bool:
    """True if ``value`` is the imported OerfDevWorkloadBoundary reference."""

    if value == policy_arn(account, BOUNDARY_POLICY_NAME):
        return True
    if isinstance(value, dict) and value == workload_permissions_boundary_import(account):
        return True
    return False


def nonwhitespace_size(document: dict[str, Any]) -> int:
    """IAM managed-policy quota is 6,144 characters excluding whitespace."""

    return len(json.dumps(document, separators=(",", ":")))


def execution_policy(account: str, region: str, guardrail_arn: str) -> dict[str, Any]:
    """Dev-scoped allows (``DN``, ``DS3``, roles, …) for the CFN execution role."""

    boundary = policy_arn(account, BOUNDARY_POLICY_NAME)
    document = {
        "Version": "2012-10-17",
        "Statement": [
            _allow("DN", _NAMED_SERVICE_ACTIONS, _service_dev_patterns(account)),
            _allow(
                "DS3",
                "s3:*",
                [
                    "arn:aws:s3:::*-dev-*",
                    "arn:aws:s3:::*-dev-*/*",
                    "arn:aws:s3:::*oerfdev*",
                    "arn:aws:s3:::*oerfdev*/*",
                ],
            ),
            _allow("AR", ["s3:Get*", "s3:List*"], _asset_bucket_arns(account, region)),
            _allow("MT", _MAIN_TOPIC_ALLOWED, _main_topic(account, region)),
            _allow(
                "BS",
                ["ssm:GetParameter", "ssm:GetParameters"],
                _bootstrap_ssm_parameter_arn(account, region),
            ),
            _allow(
                "KFH",
                ["kinesis:*", "firehose:TagDeliveryStream"],
                [
                    f"arn:aws:kinesis:*:{account}:*-dev-*",
                    f"arn:aws:firehose:*:{account}:deliverystream/*-dev-*",
                ],
            ),
            _allow(
                "CRB",
                _ROLE_CREATE_ACTIONS,
                _role_pattern(account),
                condition={"StringEquals": {_BOUNDARY_KEY: boundary}},
            ),
            _allow(
                "MRB",
                _ROLE_MUTATION_ACTIONS,
                _role_pattern(account),
                condition={"StringEqualsIfExists": {_BOUNDARY_KEY: boundary}},
            ),
            _allow(
                "DRR",
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
                "PR",
                ["iam:PassRole"],
                _role_pattern(account),
                condition={"StringEquals": {"iam:PassedToService": _PASS_TO_SERVICES}},
            ),
        ],
    }
    _check_size(document, EXEC_POLICY_NAME)
    return document


def execution_policy2(account: str, region: str, guardrail_arn: str) -> dict[str, Any]:
    """Star-resource, KMS, guardrail tag, and SLR allows for the CFN execution role."""

    document = {
        "Version": "2012-10-17",
        "Statement": [
            _allow(
                "U",
                _EXEC_STAR_ACTIONS,
                "*",
            ),
            _allow(
                "EC2D",
                "ec2:Describe*",
                "*",
            ),
            _kms_allow(account, region),
            _kms_via_service_allow(account, region),
            _lambda_kms_allow(account, region),
            _allow(
                "SLR",
                ["iam:CreateServiceLinkedRole"],
                f"arn:aws:iam::{account}:role/aws-service-role/*",
                condition={"StringLike": {"iam:AWSServiceName": _SERVICE_LINKED}},
            ),
            *_tag_denies(),
        ],
    }
    _check_size(document, EXEC_POLICY2_NAME)
    return document


def execution_deny_policy(account: str, guardrail_arn: str) -> dict[str, Any]:
    """Deny half of the execution role. Attached beside the two allow policies.

    Dev allows are split across ``execution_policy`` and ``execution_policy2``. A
    deny in any attached identity policy still wins. ``NPE`` protects the
    boundary and every ``OerfDevCfnExec*`` managed policy.
    """

    document = {
        "Version": "2012-10-17",
        "Statement": [
            *_exec_denies(account),
            _guardrail_arn_deny(account, guardrail_arn),
        ],
    }
    _check_size(document, EXEC_DENY_POLICY_NAME)
    return document


def boundary_policy(account: str, region: str, guardrail_arn: str) -> dict[str, Any]:
    """Permissions boundary for dev roles and the execution role."""

    document = {
        "Version": "2012-10-17",
        "Statement": [
            _allow(None, "*", "*"),
            _outside_dev_deny(account, region),
            {
                "Effect": "Deny",
                "NotAction": _MAIN_TOPIC_ALLOWED,
                "Resource": _main_topic(account, region),
            },
            *_tag_denies(),
            _guardrail_arn_deny(account, guardrail_arn),
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
    *,
    guardrail_arn: str | None = None,
) -> bool:
    """True when the workload boundary does not deny this call."""

    arn = guardrail_arn or evaluation_guardrail_arn(account, region)
    return boundary_permits_from_statements(
        boundary_policy(account, region, arn)["Statement"],
        action,
        resource,
        context,
    )


def boundary_permits_from_statements(
    statements: list[dict[str, Any]],
    action: str,
    resource: str,
    context: dict[str, str] | None = None,
) -> bool:
    """True when these boundary statements do not deny this call."""

    return not _any_deny(statements, action, resource, context or {})


def intersection_allows(
    action: str,
    resource: str,
    account: str,
    region: str,
    context: dict[str, str] | None = None,
    *,
    guardrail_arn: str | None = None,
) -> bool:
    """True when the execution policy and the boundary both allow the call."""

    current = context or {}
    arn = guardrail_arn or evaluation_guardrail_arn(account, region)
    identities = [
        execution_policy(account, region, arn)["Statement"],
        execution_policy2(account, region, arn)["Statement"],
        execution_deny_policy(account, arn)["Statement"],
    ]
    boundary = boundary_policy(account, region, arn)["Statement"]
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
    "kinesis",
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
    stage name, plus the Bedrock guardrail actions. Those are fenced by the
    dev guardrail ARN, not by a name pattern. ``NotResource`` holds dev names
    and the shared ARNs. A bare ``*`` is not a NotResource entry: in IAM that
    would match every resource.
    """

    return {
        "Sid": "NOD",
        "Effect": "Deny",
        "NotAction": _outside_dev_not_actions(),
        "NotResource": _not_resources(account, region),
    }


def _not_resources(account: str, region: str) -> list[str]:
    """Dev names plus shared ARNs the name styles do not cover."""

    return [
        *dev_name_patterns(account),
        _main_topic(account, region),
        _arn("events", region, account, f"rule/{_SHARED_EVENTS_RULE}"),
        _arn("execute-api", region, account, "*"),
        "arn:aws:bedrock:*::foundation-model/*",
        "arn:aws:bedrock:*:*:guardrail-profile/*",
        _arn("bedrock-agentcore", region, account, "workload-identity-directory/*"),
        _arn("social-messaging", region, account, "phone-number-id/*"),
        _bootstrap_ssm_parameter_arn(account, region),
    ]


def parse_guardrail_arn(value: object, *, account: str, region: str) -> str:
    """Return the dev guardrail ARN, or raise when it does not match this stack.

    Required when ``ci:deployRoleStack`` is true. The form is
    ``arn:aws:bedrock:<region>:<account>:guardrail/<id>`` using this stack's
    region and account. ``OerfCiStack`` does not create the guardrail.
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            "CDK context 'dev:guardrailArn' is required when "
            "ci:deployRoleStack=true. Pass the dev guardrail ARN "
            "arn:aws:bedrock:<region>:<account>:guardrail/<id>. "
            "This stack does not create the guardrail."
        )
    arn = value.strip()
    match = _GUARDRAIL_ARN_RE.match(arn)
    if (
        match is None
        or match.group("account") != account
        or match.group("region") != region
    ):
        raise ValueError(
            "CDK context 'dev:guardrailArn' must be "
            f"arn:aws:bedrock:{region}:{account}:guardrail/<id> "
            f"(lowercase id). Got {arn!r}."
        )
    return arn


def evaluation_guardrail_arn(account: str, region: str) -> str:
    """Placeholder used by in-process checks that are not about a real guardrail.

    Profile ARNs are exempt by pattern. A concrete guardrail ARN is denied
    unless the caller passes ``guardrail_arn``.
    """

    return f"arn:aws:bedrock:{region}:{account}:guardrail/evalguardrail"


def _guardrail_not_resources(account: str, guardrail_arn: str) -> list[str]:
    """Dev guardrail, its versions, and this account's cross-region profiles."""

    return [
        guardrail_arn,
        f"{guardrail_arn}:*",
        f"arn:aws:bedrock:*:{account}:guardrail-profile/*",
    ]


def _guardrail_arn_deny(account: str, guardrail_arn: str) -> dict[str, Any]:
    """Deny guardrail calls that are not the dev guardrail or a profile.

    ``GetGuardrail`` and ``ApplyGuardrail`` do not send ``aws:ResourceTag``
    on the real call, so a tag condition denies the dev guardrail. The
    service authorization reference does list ``aws:RequestTag`` for
    ``CreateGuardrail`` on ``guardrail-profile`` (the cross-region profile
    this stack sets). That action stays on ``ReqTag``.
    ``ListGuardrails`` is omitted: IAM authorizes it on ``*``, so ``NotResource``
    cannot fence it.     ``ListTagsForResource`` is listed here: harmless read but must not reach
    the main guardrail ARN.
    """

    return {
        "Sid": "GA",
        "Effect": "Deny",
        "Action": _GUARDRAIL_ARN_ACTIONS,
        "NotResource": _guardrail_not_resources(account, guardrail_arn),
    }


def _tag_denies() -> list[dict[str, Any]]:
    """Deny Cognito, API Gateway and CloudFront without the dev tag.

    Existing resources use ``aws:ResourceTag``. Create calls use ``aws:RequestTag``,
    and only on the create ARN, so a child create under an already tagged API is
    judged by the parent resource tag. ``StringNotEqualsIfExists`` denies a missing
    key. ``AdminCreateUser`` is denied outright: it supports only the resource tag,
    so a copied dev tag would otherwise authorize user creation in any pool.

    ``cognito-idp:*``, ``apigatewayv2:*`` and ``cloudfront:*`` already include
    ``TagResource`` and ``UntagResource``. Cognito and CloudFront authorize
    those on the user pool and the distribution, and both support
    ``aws:ResourceTag``. API Gateway has no separate ``TagResource`` action;
    tagging a REST or HTTP API is ``apigateway:*`` on ``/tags/*``, which does
    not support ``aws:ResourceTag`` and is denied outright. Bedrock guardrail
    actions are not in this statement. ``CreateGuardrail`` remains: its
    reference row lists ``aws:RequestTag`` on ``guardrail-profile``.

    Reads whose reference row has no tag key, and whose resource is not one
    of the ARNs above, stay allowed. They are residuals: Cognito
    ``DescribeUserPoolDomain``, ``ListUserPools`` and ``GetUser``; API Gateway
    ``GET`` on ``/account``; CloudFront ``ListDistributions`` and
    ``CreateOriginAccessControl`` (update and delete of an origin access
    control are denied outright); Bedrock ``InvokeModel`` on a foundation
    model, which cannot carry a customer tag, and ``ListGuardrails`` on ``*``.
    ``ApplyGuardrail`` on ``guardrail-profile/*`` has no tag key and is exempted
    by the guardrail ARN deny.
    """

    missing_resource_tag = {"StringNotEqualsIfExists": {_RESOURCE_TAG: STAGE_TAG_VALUE}}
    missing_request_tag = {"StringNotEqualsIfExists": {_REQUEST_TAG: STAGE_TAG_VALUE}}
    return [
        {
            "Sid": "TD",
            "Effect": "Deny",
            "Action": [
                "cognito-idp:*",
                "apigateway:*",
                "apigatewayv2:*",
                "cloudfront:*",
            ],
            "Resource": [
                "arn:aws:cognito-idp:*:*:userpool/*",
                "arn:aws:apigateway:*::/restapis/*",
                "arn:aws:apigateway:*::/apis/*",
                "arn:aws:cloudfront::*:distribution/*",
            ],
            "Condition": missing_resource_tag,
        },
        {
            "Sid": "RT",
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
            "Effect": "Deny",
            "Action": "apigateway:POST",
            "Resource": [
                "arn:aws:apigateway:*::/restapis",
                "arn:aws:apigateway:*::/apis",
            ],
            "Condition": missing_request_tag,
        },
        {
            "Effect": "Deny",
            "Action": "cognito-idp:AdminCreateUser",
            "Resource": "*",
        },
    ]


def _arn(service: str, region: str, account: str, resource: str) -> str:
    return f"arn:aws:{service}:{region}:{account}:{resource}"


def bootstrap_ssm_parameter_arn(account: str, region: str) -> str:
    """SSM ARN for ``/cdk-bootstrap/oerfdev/*``. CloudFormation reads ``version``."""

    return _bootstrap_ssm_parameter_arn(account, region)


def _bootstrap_ssm_parameter_arn(account: str, region: str) -> str:
    return (
        f"arn:aws:ssm:{region}:{account}:parameter/cdk-bootstrap/"
        f"{_DEV_BOOTSTRAP_QUALIFIER}/*"
    )


def _exec_denies(account: str) -> list[dict[str, Any]]:
    """Denies the task requires on the execution policy itself.

    The boundary repeats these and adds the role-creation condition. The
    execution role carries that boundary, so a missing condition key still
    fails closed there.
    """

    return [item for item in _boundary_denies(account) if item["Sid"] in _EXEC_DENY_SIDS]


_EXEC_DENY_SIDS = frozenset(
    {
        "DAT",
        "NPE",
        "NU",
        "NPR",
        "NRB",
        "NCP",
        "NIOD",
        "NBA",
    }
)


def _boundary_denies(account: str) -> list[dict[str, Any]]:
    boundary = policy_arn(account, BOUNDARY_POLICY_NAME)
    protected = [
        f"arn:aws:iam::{account}:policy/{BOUNDARY_POLICY_NAME}",
        f"arn:aws:iam::{account}:policy/OerfDevCfnExec*",
    ]
    return [
        *_outright_denies(),
        {
            "Sid": "NPE",
            "Effect": "Deny",
            "Action": [
                "iam:CreatePolicy",
                "iam:DeletePolicy",
                "iam:*PolicyVersion",
            ],
            "Resource": protected,
        },
        {
            "Sid": "NU",
            "Effect": "Deny",
            "Action": [
                "iam:AddUserToGroup",
                "iam:AttachGroupPolicy",
                "iam:AttachUserPolicy",
                "iam:CreateAccessKey",
                "iam:CreateLoginProfile",
                "iam:CreateUser",
                "iam:DeleteGroupPolicy",
                "iam:Delete*PermissionsBoundary",
                "iam:DeleteUserPolicy",
                "iam:DetachGroupPolicy",
                "iam:DetachUserPolicy",
                "iam:PutGroupPolicy",
                "iam:PutUserPolicy",
                "iam:RemoveUserFromGroup",
                *_KMS_KEY_ADMIN_DENY_ACTIONS,
                "account:*",
                "organizations:*",
            ],
            "Resource": "*",
        },
        {
            "Sid": "NPR",
            "Effect": "Deny",
            "Action": "iam:*",
            "Resource": [
                f"arn:aws:iam::{account}:role/cdk-hnb659fds-*",
                f"arn:aws:iam::{account}:role/cdk-oerfdev-*",
                f"arn:aws:iam::{account}:role/OerfGitHubDeployRole*",
            ],
        },
        {
            "Sid": "NRB",
            "Effect": "Deny",
            "Action": _ROLE_CREATE_ACTIONS,
            "Resource": "*",
            "Condition": {"StringNotEqualsIfExists": {_BOUNDARY_KEY: boundary}},
        },
        {
            "Sid": "NCP",
            "Effect": "Deny",
            "Action": "iam:CreatePolicy",
            "Resource": [f"arn:aws:iam::{account}:policy/*", "*"],
        },
        {
            "Sid": "NIOD",
            "Effect": "Deny",
            "Action": list(
                dict.fromkeys(
                    [
                        "iam:CreateRole",
                        "iam:PutRole*",
                        "iam:AttachRolePolicy",
                        "iam:UpdateAssumeRolePolicy",
                        "iam:TagRole",
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
            "Sid": "NBA",
            "Effect": "Deny",
            "Action": ["iam:AttachRolePolicy", "iam:AttachUserPolicy"],
            "Resource": "*",
            "Condition": {"ArnEquals": {"iam:PolicyARN": _BROAD_MANAGED_POLICIES}},
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
            "Sid": "DAT",
            "Effect": "Deny",
            "Action": "apigateway:*",
            "Resource": "arn:aws:apigateway:*::/tags/*",
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
        "Sid": "K1",
        "Effect": "Allow",
        "Action": ["kms:Decrypt", "kms:DescribeKey"],
        "Resource": f"arn:aws:kms:{region}:{account}:key/*",
        "Condition": {
            "ForAnyValue:StringEquals": {"kms:ResourceAliases": _ASSET_KEY_ALIAS},
            "StringEquals": {"kms:ViaService": f"s3.{region}.amazonaws.com"},
        },
    }


def _lambda_kms_allow(account: str, region: str) -> dict[str, Any]:
    """Encrypt Lambda environment variables with the account AWS/Lambda key."""

    return {
        "Sid": "K2",
        "Effect": "Allow",
        "Action": ["kms:Encrypt", "kms:DescribeKey"],
        "Resource": f"arn:aws:kms:{region}:{account}:key/*",
        "Condition": {
            "ForAnyValue:StringEquals": {"kms:ResourceAliases": _LAMBDA_ENV_KEY_ALIAS},
            "StringEquals": {"kms:ViaService": f"lambda.{region}.amazonaws.com"},
        },
    }


_VIA_SERVICES_KMS = (
    "apigateway.{region}.amazonaws.com",
    "bedrock.{region}.amazonaws.com",
    "cognito-idp.{region}.amazonaws.com",
    "dynamodb.{region}.amazonaws.com",
    "events.{region}.amazonaws.com",
    "lambda.{region}.amazonaws.com",
    "sns.{region}.amazonaws.com",
    "states.{region}.amazonaws.com",
)


def _kms_via_service_allow(account: str, region: str) -> dict[str, Any]:
    """KMS data-plane calls CloudFormation handlers make via AWS services."""

    return {
        "Sid": "KV",
        "Effect": "Allow",
        "Action": "kms:*",
        "Resource": f"arn:aws:kms:{region}:{account}:key/*",
        "Condition": {
            "StringEquals": {
                "kms:ViaService": [s.format(region=region) for s in _VIA_SERVICES_KMS]
            }
        },
    }


def _allow(
    sid: str | None,
    action: str | list[str],
    resource: str | list[str],
    condition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    statement: dict[str, Any] = {
        "Effect": "Allow",
        "Action": action,
        "Resource": resource,
    }
    if sid:
        statement["Sid"] = sid
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
    """IAM ``*`` and ``?`` matching. ``*`` matches across ``/`` and ``:``."""

    if "*" not in pattern and "?" not in pattern:
        return pattern == value
    regex = "^"
    for char in pattern:
        if char == "*":
            regex += ".*"
        elif char == "?":
            regex += "."
        else:
            regex += re.escape(char)
    regex += "$"
    return re.match(regex, value) is not None
