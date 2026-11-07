"""Read-only check that the dev execution role and channel router stay inside the stage.

Uses only ``iam:SimulatePrincipalPolicy`` and ``iam:SimulateCustomPolicy``.
Run it after re-bootstrapping the dev toolkit and again after a dev deploy
or any change to the dev execution policies or ``OerfDevWorkloadBoundary``.

Untagged cases pass no ``aws:ResourceTag`` or ``aws:RequestTag``. That is what
Bedrock guardrail reads do, and what a service may do even when the
authorization reference lists the key. The ``tagged-policy`` cases are
separate: they check the tag conditions when the key is present.

    python -m scripts.check_dev_boundary \\
        --profile Fission-OERF --region us-east-1 --account 906048429638 \\
        --router-role-arn arn:aws:iam::906048429638:role/OerfComputeStack-dev-... \\
        --phone-number-id phone-number-id-... \\
        --inbound-topic-arn arn:aws:sns:us-east-1:906048429638:OerfComputeStack-InboundTopic... \\
        --user-pool-arn arn:aws:cognito-idp:us-east-1:906048429638:userpool/us-east-1_... \\
        --dev-user-pool-arn arn:aws:cognito-idp:us-east-1:906048429638:userpool/us-east-1_... \\
        --distribution-arn arn:aws:cloudfront::906048429638:distribution/E... \\
        --dev-distribution-arn arn:aws:cloudfront::906048429638:distribution/E... \\
        --rest-api-id abc123 --dev-rest-api-id def456 \\
        --http-api-id abc123 --dev-http-api-id def456 \\
        --ws-api-id abc123 --dev-ws-api-id def456 \\
        --caller-role-arn arn:aws:iam::906048429638:role/oerf-dev-agent-caller \\
        --dev-runtime-arn arn:aws:bedrock-agentcore:us-east-1:906048429638:runtime/oerf_dev_agent \\
        --main-runtime-arn arn:aws:bedrock-agentcore:us-east-1:906048429638:runtime/oerf_agent \\
        --dev-guardrail-arn arn:aws:bedrock:us-east-1:906048429638:guardrail/b7nun5huxbii \\
        --main-guardrail-arn arn:aws:bedrock:us-east-1:906048429638:guardrail/MAIN \\
        --analyzer-role-arn arn:aws:iam::906048429638:role/... \\
        --interpret-role-arn arn:aws:iam::906048429638:role/oerf-dev-agent-runtime
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

from infra.cfn_handler_permission_tests import (
    attach_compatible_resource_arn,
    bedrock_guardrail_has_kms_key_arn,
    collect_handler_list_permissions,
    handler_permission_checks,
    handler_permission_dir,
    synth_dev_application_templates,
)
from infra.constructs.social_messaging_arns import phone_number_arn
from infra.dev_boundary import (
    BOUNDARY_POLICY_NAME,
    boundary_policy,
    execution_deny_policy,
    execution_policy,
    execution_policy2,
    intersection_allows,
    parse_guardrail_arn,
    policy_arn,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

_RESOURCE_TAG = "aws:ResourceTag/oerf:stage"
_REQUEST_TAG = "aws:RequestTag/oerf:stage"
_DEV_TAG = {_RESOURCE_TAG: "dev"}
_DEV_REQUEST_TAG = {_REQUEST_TAG: "dev"}

# action, resource template, expect allowed, pass iam:PermissionsBoundary
EXEC_CHECKS: list[tuple[str, str, bool, bool]] = [
    (
        "lambda:CreateFunction",
        "arn:aws:lambda:{region}:{account}:function:oerf-dev-x",
        True,
        False,
    ),
    (
        "lambda:CreateFunction",
        "arn:aws:lambda:{region}:{account}:function:oerf-step-context",
        False,
        False,
    ),
    (
        "dynamodb:DeleteTable",
        "arn:aws:dynamodb:{region}:{account}:table/OerfDataStack-dev-IncidentTable",
        True,
        False,
    ),
    (
        "dynamodb:DeleteTable",
        "arn:aws:dynamodb:{region}:{account}:table/OerfDataStack-IncidentTable",
        False,
        False,
    ),
    (
        "iam:CreateRole",
        "arn:aws:iam::{account}:role/OerfDataStack-dev-Example",
        False,
        False,
    ),
    (
        "iam:CreateRole",
        "arn:aws:iam::{account}:role/OerfDataStack-dev-Example",
        True,
        True,
    ),
    (
        "iam:PutRolePolicy",
        "arn:aws:iam::{account}:role/OerfGitHubDeployRole",
        False,
        False,
    ),
    (
        "iam:DeleteRolePermissionsBoundary",
        "arn:aws:iam::{account}:role/OerfDataStack-dev-Example",
        False,
        False,
    ),
    (
        "iam:CreatePolicyVersion",
        "arn:aws:iam::{account}:policy/" + BOUNDARY_POLICY_NAME,
        False,
        False,
    ),
    ("iam:CreateUser", "arn:aws:iam::{account}:user/example", False, False),
    (
        "sns:Subscribe",
        "arn:aws:sns:{region}:{account}:OerfComputeStack-InboundTopicCHECK",
        True,
        False,
    ),
    (
        "sns:Publish",
        "arn:aws:sns:{region}:{account}:OerfComputeStack-InboundTopicCHECK",
        False,
        False,
    ),
    (
        "ssm:GetParameters",
        "arn:aws:ssm:{region}:{account}:parameter/cdk-bootstrap/oerfdev/version",
        True,
        False,
    ),
    (
        "ssm:GetParameters",
        "arn:aws:ssm:{region}:{account}:parameter/cdk-bootstrap/hnb659fds/version",
        False,
        False,
    ),
]

# action, resource template, expect allowed by the boundary
ROUTER_CHECKS: list[tuple[str, str, bool]] = [
    (
        "social-messaging:SendWhatsAppMessage",
        "arn:aws:social-messaging:{region}:{account}:phone-number-id/{phone}",
        True,
    ),
    (
        "dynamodb:PutItem",
        "arn:aws:dynamodb:{region}:{account}:table/OerfDataStack-IncidentTable",
        False,
    ),
    (
        "lambda:InvokeFunction",
        "arn:aws:lambda:{region}:{account}:function:oerf-step-context",
        False,
    ),
]

# action, resource template, expect allowed. No tag context.
# Run against the execution role and the channel-router role.
STAGE_CHECKS: list[tuple[str, str, bool]] = [
    (
        "lambda:UpdateFunctionCode",
        "arn:aws:lambda:{region}:{account}:function:oerf-device-seed",
        False,
    ),
    ("cognito-idp:AdminCreateUser", "{user_pool_arn}", False),
    ("cognito-idp:AdminCreateUser", "{dev_user_pool_arn}", False),
    (
        "kms:ScheduleKeyDeletion",
        "arn:aws:kms:{region}:{account}:key/d3fdd4e1-1b8e-4997-8949-0b4bb6022099",
        False,
    ),
    (
        "kms:PutKeyPolicy",
        "arn:aws:kms:{region}:{account}:key/d3fdd4e1-1b8e-4997-8949-0b4bb6022099",
        False,
    ),
]

# Exec role, no tag context. The dev guardrail is allowed by ARN.
GUARDRAIL_EXEC_CHECKS: list[tuple[str, str, bool]] = [
    ("bedrock:GetGuardrail", "{dev_guardrail_arn}", True),
    ("bedrock:UpdateGuardrail", "{dev_guardrail_arn}", True),
    ("bedrock:CreateGuardrailVersion", "{dev_guardrail_arn}", True),
    ("bedrock:DeleteGuardrail", "{dev_guardrail_arn}", True),
    ("bedrock:DeleteGuardrail", "{dev_guardrail_arn}:1", True),
    ("bedrock:ListTagsForResource", "{dev_guardrail_arn}", True),
    ("bedrock:TagResource", "{dev_guardrail_arn}", True),
    ("bedrock:UntagResource", "{dev_guardrail_arn}", True),
    ("bedrock:GetGuardrail", "{main_guardrail_arn}", False),
    ("bedrock:ListTagsForResource", "{main_guardrail_arn}", False),
    ("bedrock:UpdateGuardrail", "{main_guardrail_arn}", False),
    ("bedrock:TagResource", "{main_guardrail_arn}", False),
    ("bedrock:UntagResource", "{main_guardrail_arn}", False),
    ("bedrock:CreateGuardrail", "*", False),
]

# Evidence analyzer and the Interpret path (AgentCore execution role).
# No tag context. Also run against the boundary with Allow *.
# ApplyGuardrail authorizes on guardrail/* without a version suffix; version
# is a request field. IAM simulation with ``...:guardrail/id:1`` is not a
# real resource ARN (see Bedrock guardrails-deploy and guardrail* in the
# service authorization reference).
APPLY_CHECKS: list[tuple[str, str, bool]] = [
    ("bedrock:ApplyGuardrail", "{dev_guardrail_arn}", True),
    ("bedrock:ApplyGuardrail", "{main_guardrail_arn}", False),
    ("bedrock:ApplyGuardrail", "{profile_arn}", True),
    ("bedrock:ApplyGuardrail", "{profile_west_arn}", True),
]

_POOL_READS_AND_WRITES = (
    "cognito-idp:DescribeUserPool",
    "cognito-idp:DescribeUserPoolClient",
    "cognito-idp:UpdateUserPool",
    "cognito-idp:DeleteUserPool",
    "cognito-idp:TagResource",
    "cognito-idp:UntagResource",
    "cognito-idp:AddCustomAttributes",
    "cognito-idp:CreateUserPoolClient",
    "cognito-idp:CreateUserPoolDomain",
    "cognito-idp:CreateManagedLoginBranding",
    "cognito-idp:DescribeManagedLoginBranding",
    "cognito-idp:DescribeManagedLoginBrandingByClient",
    "cognito-idp:UpdateManagedLoginBranding",
    "cognito-idp:DeleteManagedLoginBranding",
    "cognito-idp:GetUICustomization",
    "cognito-idp:SetUICustomization",
)
_DISTRIBUTION_CALLS = (
    "cloudfront:GetDistribution",
    "cloudfront:GetDistributionConfig",
    "cloudfront:UpdateDistribution",
    "cloudfront:DeleteDistribution",
    "cloudfront:CreateInvalidation",
    "cloudfront:GetInvalidation",
    "cloudfront:TagResource",
    "cloudfront:UntagResource",
)
_API_VERBS = ("GET", "PATCH", "PUT", "DELETE")
_API_TEMPLATES = (
    "arn:aws:apigateway:{region}::/restapis/{dev_rest_api_id}",
    "arn:aws:apigateway:{region}::/restapis/{rest_api_id}",
    "arn:aws:apigateway:{region}::/apis/{dev_http_api_id}",
    "arn:aws:apigateway:{region}::/apis/{http_api_id}",
    "arn:aws:apigateway:{region}::/apis/{dev_ws_api_id}",
    "arn:aws:apigateway:{region}::/apis/{ws_api_id}",
)


def _service_untagged_denies() -> list[tuple[str, str, bool]]:
    """Cognito, API Gateway and CloudFront with no tag. Both stages fail closed."""

    rows: list[tuple[str, str, bool]] = []
    for action in _POOL_READS_AND_WRITES:
        rows.append((action, "{dev_user_pool_arn}", False))
        rows.append((action, "{user_pool_arn}", False))
    for verb in _API_VERBS:
        for template in _API_TEMPLATES:
            rows.append((f"apigateway:{verb}", template, False))
    for action in _DISTRIBUTION_CALLS:
        rows.append((action, "{dev_distribution_arn}", False))
        rows.append((action, "{distribution_arn}", False))
    rows.extend(
        [
            ("cognito-idp:CreateUserPool", "*", False),
            ("cloudfront:CreateDistribution", "*", False),
            ("apigateway:POST", "arn:aws:apigateway:{region}::/restapis", False),
            ("apigateway:POST", "arn:aws:apigateway:{region}::/apis", False),
            ("apigatewayv2:CreateApi", "*", False),
            ("apigatewayv2:ImportApi", "*", False),
            ("apigatewayv2:ReimportApi", "*", False),
        ]
    )
    return rows


# No tag context. Tag-conditioned actions are denied on the dev id and the main id.
SERVICE_UNTAGGED_DENIES: list[tuple[str, str, bool]] = _service_untagged_denies()

# Reads the reference does not tag-gate, plus CloudFront create of an origin
# access control. Allowed with no tag context.
SERVICE_UNTAGGED_ALLOWS: list[tuple[str, str, bool]] = [
    ("cognito-idp:DescribeUserPoolDomain", "*", True),
    ("cognito-idp:ListUserPools", "*", True),
    ("cognito-idp:GetUser", "*", True),
    ("apigateway:GET", "arn:aws:apigateway:{region}::/account", True),
    ("cloudfront:ListDistributions", "*", True),
    ("cloudfront:CreateOriginAccessControl", "*", True),
]

# Policy logic only. These pass a tag the live call may not send.
# action, resource template, expect allowed, context
TAGGED_POLICY_CHECKS: list[tuple[str, str, bool, dict[str, str]]] = [
    ("bedrock:CreateGuardrail", "*", True, _DEV_REQUEST_TAG),
    ("cognito-idp:CreateUserPool", "*", True, _DEV_REQUEST_TAG),
    ("cloudfront:CreateDistribution", "*", True, _DEV_REQUEST_TAG),
    ("apigateway:POST", "arn:aws:apigateway:{region}::/restapis", True, _DEV_REQUEST_TAG),
    ("apigateway:POST", "arn:aws:apigateway:{region}::/apis", True, _DEV_REQUEST_TAG),
    ("apigatewayv2:CreateApi", "*", True, _DEV_REQUEST_TAG),
    ("cognito-idp:DescribeUserPool", "{dev_user_pool_arn}", True, _DEV_TAG),
    ("cognito-idp:AddCustomAttributes", "{dev_user_pool_arn}", True, _DEV_TAG),
    ("cognito-idp:DescribeUserPool", "{dev_user_pool_arn}", False, {_RESOURCE_TAG: "main"}),
    ("cognito-idp:AdminCreateUser", "{user_pool_arn}", False, _DEV_TAG),
    ("cognito-idp:AdminCreateUser", "{dev_user_pool_arn}", False, _DEV_TAG),
    ("apigateway:GET", "arn:aws:apigateway:{region}::/restapis/{dev_rest_api_id}", True, _DEV_TAG),
    (
        "apigateway:PATCH",
        "arn:aws:apigateway:{region}::/restapis/{dev_rest_api_id}",
        True,
        _DEV_TAG,
    ),
    ("apigateway:PUT", "arn:aws:apigateway:{region}::/apis/{dev_http_api_id}", True, _DEV_TAG),
    ("apigateway:DELETE", "arn:aws:apigateway:{region}::/apis/{dev_ws_api_id}", True, _DEV_TAG),
    ("cloudfront:GetDistribution", "{dev_distribution_arn}", True, _DEV_TAG),
    ("cloudfront:UpdateDistribution", "{dev_distribution_arn}", True, _DEV_TAG),
    ("cloudfront:CreateInvalidation", "{dev_distribution_arn}", True, _DEV_TAG),
    ("cloudfront:GetInvalidation", "{dev_distribution_arn}", True, _DEV_TAG),
    # The fence is the tag. A simulated dev tag on the main distribution matches.
    ("cloudfront:UpdateDistribution", "{distribution_arn}", True, _DEV_TAG),
]

# action, resource template, expect allowed. No tag context: the resource is untagged.
# Run against the execution role and the channel-router role.
FENCE_CHECKS: list[tuple[str, str, bool]] = [
    (
        "apigateway:PUT",
        "arn:aws:apigateway:{region}::/tags/arn:aws:apigateway:{region}::/restapis/{rest_api_id}",
        False,
    ),
    ("cognito-idp:TagResource", "{user_pool_arn}", False),
    ("iam:PutUserPolicy", "arn:aws:iam::{account}:user/oerf-dev-check", False),
    ("iam:AttachGroupPolicy", "arn:aws:iam::{account}:group/oerf-dev-check", False),
    ("ec2:CreateNetworkInterface", "*", False),
]

# action, resource template, expect allowed by the caller role and the boundary.
RUNTIME_CHECKS: list[tuple[str, str, bool]] = [
    ("bedrock-agentcore:InvokeAgentRuntime", "{dev_runtime_arn}", True),
    ("bedrock-agentcore:InvokeAgentRuntime", "{main_runtime_arn}", False),
]

_ALLOW_ALL = {
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}],
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the dev stage boundary. Read-only.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--router-role-arn")
    parser.add_argument("--phone-number-id")
    parser.add_argument(
        "--inbound-topic-arn",
        help="Main OerfComputeStack inbound topic. Subscribe is allowed; Publish is not.",
    )
    parser.add_argument(
        "--user-pool-arn",
        help="Main user pool ARN. Calls are denied with no tag, and AdminCreateUser always.",
    )
    parser.add_argument(
        "--dev-user-pool-arn",
        help="Dev user pool ARN. Tag-gated calls are denied when the tag is absent.",
    )
    parser.add_argument(
        "--distribution-arn",
        help="Main CloudFront distribution ARN.",
    )
    parser.add_argument(
        "--dev-distribution-arn",
        help="Dev CloudFront distribution ARN.",
    )
    parser.add_argument(
        "--rest-api-id",
        help="Main REST API id. PUT on its /tags path is denied.",
    )
    parser.add_argument("--dev-rest-api-id", help="Dev REST API id.")
    parser.add_argument("--http-api-id", help="Main HTTP API id.")
    parser.add_argument("--dev-http-api-id", help="Dev HTTP API id.")
    parser.add_argument("--ws-api-id", help="Main WebSocket API id.")
    parser.add_argument("--dev-ws-api-id", help="Dev WebSocket API id.")
    parser.add_argument(
        "--caller-role-arn",
        help="Dev agent caller role. InvokeAgentRuntime is allowed on the dev runtime only.",
    )
    parser.add_argument(
        "--dev-runtime-arn",
        help="AgentRuntimeArn output of OerfComputeStack-dev.",
    )
    parser.add_argument(
        "--main-runtime-arn",
        help="AgentRuntimeArn output of OerfComputeStack. InvokeAgentRuntime is denied.",
    )
    parser.add_argument(
        "--dev-guardrail-arn",
        required=True,
        help="Dev guardrail ARN. Get, update, and apply are allowed with no resource tag.",
    )
    parser.add_argument(
        "--main-guardrail-arn",
        help="Main guardrail ARN. The same calls are denied.",
    )
    parser.add_argument(
        "--analyzer-role-arn",
        help="Evidence analyzer role. ApplyGuardrail is allowed on the dev guardrail only.",
    )
    parser.add_argument(
        "--interpret-role-arn",
        help=(
            "AgentCore runtime execution role (oerf-dev-agent-runtime). "
            "Interpret invokes that runtime, which calls ApplyGuardrail."
        ),
    )
    parser.add_argument(
        "--handler-permissions",
        nargs="?",
        const=handler_permission_dir(_REPO_ROOT),
        default=None,
        type=Path,
        help=(
            "Only run IAM simulations for CloudFormation handler permissions "
            "fixture (default: infra/bootstrap/iam-fixtures/cfn-handler-permissions.json). "
            "Requires the same exec role ARN and policies as the unit test. "
            "Other checks are skipped."
        ),
    )
    parser.add_argument(
        "--handler-bedrock-guardrail-kms-key",
        action="store_true",
        help=(
            "Assume AWS::Bedrock::Guardrail has KmsKeyArn set; simulate Bedrock "
            "handler kms:* actions (default: omit; matches dev template without CMK)."
        ),
    )
    parser.add_argument(
        "--handler-repo-only",
        action="store_true",
        help=(
            "With --handler-permissions, skip live-exec simulations against the "
            "deployed cdk-oerfdev-cfn-exec role; evaluate repo policies locally only."
        ),
    )
    args = parser.parse_args(argv)
    try:
        args.dev_guardrail_arn = parse_guardrail_arn(
            args.dev_guardrail_arn, account=args.account, region=args.region
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.handler_permissions is not None and args.main_guardrail_arn is None:
        args.main_guardrail_arn = (
            f"arn:aws:bedrock:{args.region}:{args.account}:guardrail/maincheckplace1"
        )
    if args.main_guardrail_arn is not None:
        try:
            args.main_guardrail_arn = parse_guardrail_arn(
                args.main_guardrail_arn,
                account=args.account,
                region=args.region,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.handler_permissions is None:
        missing = [
            name
            for name, value in [
                ("--router-role-arn", args.router_role_arn),
                ("--phone-number-id", args.phone_number_id),
                ("--inbound-topic-arn", args.inbound_topic_arn),
                ("--user-pool-arn", args.user_pool_arn),
                ("--dev-user-pool-arn", args.dev_user_pool_arn),
                ("--distribution-arn", args.distribution_arn),
                ("--dev-distribution-arn", args.dev_distribution_arn),
                ("--rest-api-id", args.rest_api_id),
                ("--dev-rest-api-id", args.dev_rest_api_id),
                ("--http-api-id", args.http_api_id),
                ("--dev-http-api-id", args.dev_http_api_id),
                ("--ws-api-id", args.ws_api_id),
                ("--dev-ws-api-id", args.dev_ws_api_id),
                ("--caller-role-arn", args.caller_role_arn),
                ("--dev-runtime-arn", args.dev_runtime_arn),
                ("--main-runtime-arn", args.main_runtime_arn),
                ("--main-guardrail-arn", args.main_guardrail_arn),
                ("--analyzer-role-arn", args.analyzer_role_arn),
                ("--interpret-role-arn", args.interpret_role_arn),
            ]
            if value is None
        ]
        if missing:
            print(
                "Missing required arguments: " + ", ".join(missing),
                file=sys.stderr,
            )
            return 2

    if args.dev_guardrail_arn == args.main_guardrail_arn:
        print("dev and main guardrail ARNs must differ", file=sys.stderr)
        return 2
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    client = session.client("iam")
    if args.handler_permissions is not None:
        return _run_handler_permissions(client, args)

    failures = _run(client, args)
    if failures:
        print(f"{failures} FAIL")
        return 1
    print("ALL PASS")
    return 0


def _run_handler_permissions(client: Any, args: argparse.Namespace) -> int:
    """Simulate every handler permission against the real execution role."""

    exec_role = (
        f"arn:aws:iam::{args.account}:role/"
        f"cdk-oerfdev-cfn-exec-role-{args.account}-{args.region}"
    )
    handler_dir = args.handler_permissions.resolve()
    failures = 0
    dev_templates = synth_dev_application_templates(args.account, args.region)
    kms_key_arn_set = args.handler_bedrock_guardrail_kms_key or any(
        bedrock_guardrail_has_kms_key_arn(template) for template in dev_templates
    )
    rows, handler_skipped = handler_permission_checks(
        account=args.account,
        region=args.region,
        guardrail_arn=args.dev_guardrail_arn,
        handler_dir=handler_dir,
        kms_key_arn_set=kms_key_arn_set,
        dev_templates=dev_templates,
    )
    print(
        f"handler-permissions from {handler_dir} ({len(rows)} simulations, "
        f"{len(handler_skipped)} skipped)"
    )
    for cfn_type, action, reason in sorted(
        handler_skipped, key=lambda t: (t[0], t[1], t[2])
    ):
        print(f"SKIP {cfn_type} {action} {reason}")
    list_only = collect_handler_list_permissions(handler_dir)
    if list_only:
        print(
            f"LIST (informational, not simulated; {len(list_only)} CloudFormation "
            "list-handler permissions)"
        )
        for cfn_type, action in sorted(list_only):
            print(f"LIST {cfn_type} {action}")
    for cfn_type, action, resource, expect_allow, context in rows:
        resource_sim = attach_compatible_resource_arn(
            resource, args.account, args.region
        )
        label_suffix = f" [{cfn_type}]"
        if not args.handler_repo_only:
            failures += _expect(
                "untagged live-exec" + label_suffix,
                action,
                resource_sim,
                expect_allow,
                _principal(client, exec_role, action, resource_sim, context),
            )
        failures += _expect(
            "untagged repo-exec" + label_suffix,
            action,
            resource_sim,
            expect_allow,
            _repo_exec_decision(
                action,
                resource_sim,
                context,
                account=args.account,
                region=args.region,
                guardrail_arn=args.dev_guardrail_arn,
            ),
        )
    if failures:
        print(f"{failures} FAIL")
        return 1
    print("ALL PASS (handler-permissions)")
    return 0


def _run(client: Any, args: argparse.Namespace) -> int:
    boundary = policy_arn(args.account, BOUNDARY_POLICY_NAME)
    exec_role = (
        f"arn:aws:iam::{args.account}:role/"
        f"cdk-oerfdev-cfn-exec-role-{args.account}-{args.region}"
    )
    identity = [
        json.dumps(
            execution_policy(args.account, args.region, args.dev_guardrail_arn)
        ),
        json.dumps(
            execution_policy2(args.account, args.region, args.dev_guardrail_arn)
        ),
        json.dumps(execution_deny_policy(args.account, args.dev_guardrail_arn)),
    ]
    boundary_doc = json.dumps(
        boundary_policy(args.account, args.region, args.dev_guardrail_arn)
    )
    phone = phone_number_arn(args.region, args.account, args.phone_number_id)
    values = _values(args, boundary, phone)
    failures = 0
    for action, template, expect_allow, needs_boundary in EXEC_CHECKS:
        resource = _resource(template, args, boundary, phone)
        if action.startswith("sns:"):
            resource = args.inbound_topic_arn
        context = {"iam:PermissionsBoundary": boundary} if needs_boundary else {}
        failures += _exec_pair(
            client,
            exec_role,
            identity,
            boundary_doc,
            "untagged",
            action,
            resource,
            expect_allow,
            context,
        )
    allow_all = [json.dumps(_ALLOW_ALL)]
    for action, template, expect_allow in ROUTER_CHECKS:
        resource = template.format(
            account=args.account,
            region=args.region,
            phone=phone.rsplit("/", 1)[-1],
        )
        failures += _expect(
            "untagged live-router",
            action,
            resource,
            expect_allow,
            _principal(client, args.router_role_arn, action, resource, {}),
        )
        failures += _expect(
            "untagged boundary",
            action,
            resource,
            expect_allow,
            _custom(client, allow_all, boundary_doc, action, resource, {}),
        )
    for action, template, expect_allow in STAGE_CHECKS:
        resource = template.format(**values)
        failures += _quad(
            client,
            exec_role,
            args.router_role_arn,
            identity,
            allow_all,
            boundary_doc,
            "untagged",
            action,
            resource,
            expect_allow,
            {},
        )
    for action, template, expect_allow in FENCE_CHECKS:
        resource = template.format(**values)
        failures += _quad(
            client,
            exec_role,
            args.router_role_arn,
            identity,
            allow_all,
            boundary_doc,
            "untagged",
            action,
            resource,
            expect_allow,
            {},
        )
    for action, template, expect_allow in GUARDRAIL_EXEC_CHECKS:
        resource = template.format(**values)
        failures += _exec_pair(
            client,
            exec_role,
            identity,
            boundary_doc,
            "untagged",
            action,
            resource,
            expect_allow,
            {},
        )
    for action, template, expect_allow in APPLY_CHECKS:
        resource = template.format(**values)
        for label, role in (
            ("untagged live-analyzer", args.analyzer_role_arn),
            ("untagged live-interpret", args.interpret_role_arn),
        ):
            failures += _expect(
                label,
                action,
                resource,
                expect_allow,
                _principal(client, role, action, resource, {}),
            )
        failures += _expect(
            "untagged boundary",
            action,
            resource,
            expect_allow,
            _custom(client, allow_all, boundary_doc, action, resource, {}),
        )
    for action, template, expect_allow in SERVICE_UNTAGGED_DENIES:
        resource = template.format(**values)
        failures += _quad(
            client,
            exec_role,
            args.router_role_arn,
            identity,
            allow_all,
            boundary_doc,
            "untagged",
            action,
            resource,
            expect_allow,
            {},
        )
    for action, template, expect_allow in SERVICE_UNTAGGED_ALLOWS:
        resource = template.format(**values)
        failures += _exec_pair(
            client,
            exec_role,
            identity,
            boundary_doc,
            "untagged",
            action,
            resource,
            expect_allow,
            {},
        )
        failures += _expect(
            "untagged boundary",
            action,
            resource,
            expect_allow,
            _custom(client, allow_all, boundary_doc, action, resource, {}),
        )
    print("tagged-policy cases (tag context the live call may not send)")
    for action, template, expect_allow, context in TAGGED_POLICY_CHECKS:
        resource = template.format(**values)
        failures += _exec_pair(
            client,
            exec_role,
            identity,
            boundary_doc,
            "tagged-policy",
            action,
            resource,
            expect_allow,
            context,
        )
        failures += _expect(
            "tagged-policy boundary",
            action,
            resource,
            expect_allow,
            _custom(client, allow_all, boundary_doc, action, resource, context),
        )
        if not expect_allow:
            failures += _expect(
                "tagged-policy live-router",
                action,
                resource,
                expect_allow,
                _principal(client, args.router_role_arn, action, resource, context),
            )
    for action, template, expect_allow in RUNTIME_CHECKS:
        resource = template.format(
            dev_runtime_arn=args.dev_runtime_arn,
            main_runtime_arn=args.main_runtime_arn,
        )
        failures += _expect(
            "untagged live-caller",
            action,
            resource,
            expect_allow,
            _principal(client, args.caller_role_arn, action, resource, {}),
        )
        failures += _expect(
            "untagged boundary",
            action,
            resource,
            expect_allow,
            _custom(client, allow_all, boundary_doc, action, resource, {}),
        )
    return failures


def _values(args: argparse.Namespace, boundary: str, phone: str) -> dict[str, str]:
    return {
        "account": args.account,
        "region": args.region,
        "boundary": boundary,
        "phone": phone.rsplit("/", 1)[-1],
        "user_pool_arn": args.user_pool_arn,
        "dev_user_pool_arn": args.dev_user_pool_arn,
        "distribution_arn": args.distribution_arn,
        "dev_distribution_arn": args.dev_distribution_arn,
        "rest_api_id": args.rest_api_id,
        "dev_rest_api_id": args.dev_rest_api_id,
        "http_api_id": args.http_api_id,
        "dev_http_api_id": args.dev_http_api_id,
        "ws_api_id": args.ws_api_id,
        "dev_ws_api_id": args.dev_ws_api_id,
        "dev_guardrail_arn": args.dev_guardrail_arn,
        "main_guardrail_arn": args.main_guardrail_arn,
        "profile_arn": _profile_arn(args.region, args.account),
        "profile_west_arn": _profile_arn("us-west-2", args.account),
        "dev_runtime_arn": args.dev_runtime_arn,
        "main_runtime_arn": args.main_runtime_arn,
    }


def _profile_arn(region: str, account: str) -> str:
    path = Path(__file__).resolve().parents[1] / "infra" / "config" / "guardrail.json"
    profile_id = json.loads(path.read_text(encoding="utf-8"))["guardrail_profile_identifier"]
    return f"arn:aws:bedrock:{region}:{account}:guardrail-profile/{profile_id}"


def _exec_pair(
    client: Any,
    exec_role: str,
    identity: list[str],
    boundary_doc: str,
    label: str,
    action: str,
    resource: str,
    expect_allow: bool,
    context: dict[str, str],
) -> int:
    failures = _expect(
        f"{label} live-exec",
        action,
        resource,
        expect_allow,
        _principal(client, exec_role, action, resource, context),
    )
    failures += _expect(
        f"{label} repo-exec",
        action,
        resource,
        expect_allow,
        _custom(client, identity, boundary_doc, action, resource, context),
    )
    return failures


def _quad(
    client: Any,
    exec_role: str,
    router_role: str,
    identity: list[str],
    allow_all: list[str],
    boundary_doc: str,
    label: str,
    action: str,
    resource: str,
    expect_allow: bool,
    context: dict[str, str],
) -> int:
    failures = _exec_pair(
        client,
        exec_role,
        identity,
        boundary_doc,
        label,
        action,
        resource,
        expect_allow,
        context,
    )
    failures += _expect(
        f"{label} live-router",
        action,
        resource,
        expect_allow,
        _principal(client, router_role, action, resource, context),
    )
    failures += _expect(
        f"{label} boundary",
        action,
        resource,
        expect_allow,
        _custom(client, allow_all, boundary_doc, action, resource, context),
    )
    return failures


def _resource(template: str, args: argparse.Namespace, boundary: str, phone: str) -> str:
    return template.format(
        account=args.account,
        region=args.region,
        boundary=boundary,
        phone=phone.rsplit("/", 1)[-1],
    )


def _expect(label: str, action: str, resource: str, expect_allow: bool, decision: str) -> int:
    allowed = decision == "allowed"
    ok = allowed is expect_allow
    status = "PASS" if ok else "FAIL"
    want = "allowed" if expect_allow else "denied"
    print(f"{status} {label} {action} {resource} {want} ({decision})")
    return 0 if ok else 1


def _repo_exec_decision(
    action: str,
    resource: str,
    context: dict[str, str],
    *,
    account: str,
    region: str,
    guardrail_arn: str,
) -> str:
    """Local policy evaluation; matches unit ``intersection_allows`` checks."""

    if intersection_allows(
        action,
        resource,
        account,
        region,
        context,
        guardrail_arn=guardrail_arn,
    ):
        return "allowed"
    return "implicitDeny"


def _principal(
    client: Any,
    role_arn: str,
    action: str,
    resource: str,
    context: dict[str, str],
) -> str:
    try:
        result = client.simulate_principal_policy(
            PolicySourceArn=role_arn,
            ActionNames=[action],
            ResourceArns=[resource],
            **_context(context),
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"ExpiredToken", "ExpiredTokenException", "RequestExpired"}:
            print(
                "AWS credentials expired during live-exec simulation; refresh the "
                "profile (for example `aws sso login`) or pass --handler-repo-only "
                "to check repo policies without IAM API calls.",
                file=sys.stderr,
            )
            raise SystemExit(2) from exc
        raise
    return result["EvaluationResults"][0]["EvalDecision"]


def _custom(
    client: Any,
    identity: list[str],
    boundary_doc: str,
    action: str,
    resource: str,
    context: dict[str, str],
) -> str:
    result = client.simulate_custom_policy(
        PolicyInputList=identity,
        PermissionsBoundaryPolicyInputList=[boundary_doc],
        ActionNames=[action],
        ResourceArns=[resource],
        **_context(context),
    )
    return result["EvaluationResults"][0]["EvalDecision"]


def _context(context: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    if not context:
        return {}
    return {
        "ContextEntries": [
            {
                "ContextKeyName": key,
                "ContextKeyValues": [value],
                "ContextKeyType": "string",
            }
            for key, value in context.items()
        ]
    }


if __name__ == "__main__":
    sys.exit(main())
