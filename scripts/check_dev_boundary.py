"""Read-only check that the dev execution role and channel router stay inside the stage.

Uses only ``iam:SimulatePrincipalPolicy`` and ``iam:SimulateCustomPolicy``.
Run it after re-bootstrapping the dev toolkit and again after a dev deploy
or any change to the dev execution policies or ``OerfDevWorkloadBoundary``.

    python -m scripts.check_dev_boundary \\
        --profile Fission-OERF --region us-east-1 --account 906048429638 \\
        --router-role-arn arn:aws:iam::906048429638:role/OerfComputeStack-dev-... \\
        --phone-number-id phone-number-id-... \\
        --inbound-topic-arn arn:aws:sns:us-east-1:906048429638:OerfComputeStack-InboundTopic... \\
        --user-pool-arn arn:aws:cognito-idp:us-east-1:906048429638:userpool/us-east-1_... \\
        --distribution-arn arn:aws:cloudfront::906048429638:distribution/E... \\
        --rest-api-id abc123 \\
        --caller-role-arn arn:aws:iam::906048429638:role/oerf-dev-agent-caller \\
        --dev-runtime-arn arn:aws:bedrock-agentcore:us-east-1:906048429638:runtime/oerf_dev_agent \\
        --main-runtime-arn arn:aws:bedrock-agentcore:us-east-1:906048429638:runtime/oerf_agent
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import boto3

from infra.constructs.social_messaging_arns import phone_number_arn
from infra.dev_boundary import (
    BOUNDARY_POLICY_NAME,
    boundary_policy,
    execution_deny_policy,
    execution_policy,
    policy_arn,
)

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

# action, resource template, expect allowed, context kind (None or "resource-tag")
# Run against the execution role and the channel-router role.
STAGE_CHECKS: list[tuple[str, str, bool, str | None]] = [
    (
        "lambda:UpdateFunctionCode",
        "arn:aws:lambda:{region}:{account}:function:oerf-device-seed",
        False,
        None,
    ),
    ("cognito-idp:AdminCreateUser", "{user_pool_arn}", False, None),
    ("cognito-idp:AdminCreateUser", "{user_pool_arn}", False, "resource-tag"),
    ("cloudfront:UpdateDistribution", "{distribution_arn}", False, None),
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
    (
        "cloudfront:UpdateOriginAccessControl",
        "arn:aws:cloudfront::{account}:origin-access-control/MAIN",
        False,
    ),
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
    parser.add_argument("--router-role-arn", required=True)
    parser.add_argument("--phone-number-id", required=True)
    parser.add_argument(
        "--inbound-topic-arn",
        required=True,
        help="Main OerfComputeStack inbound topic. Subscribe is allowed; Publish is not.",
    )
    parser.add_argument(
        "--user-pool-arn",
        required=True,
        help="Main user pool ARN. AdminCreateUser is denied with and without the dev tag.",
    )
    parser.add_argument(
        "--distribution-arn",
        required=True,
        help="Main CloudFront distribution ARN. UpdateDistribution is denied.",
    )
    parser.add_argument(
        "--rest-api-id",
        required=True,
        help="Main REST API id. PUT on its /tags path is denied.",
    )
    parser.add_argument(
        "--caller-role-arn",
        required=True,
        help="Dev agent caller role. InvokeAgentRuntime is allowed on the dev runtime only.",
    )
    parser.add_argument(
        "--dev-runtime-arn",
        required=True,
        help="AgentRuntimeArn output of OerfComputeStack-dev.",
    )
    parser.add_argument(
        "--main-runtime-arn",
        required=True,
        help="AgentRuntimeArn output of OerfComputeStack. InvokeAgentRuntime is denied.",
    )
    args = parser.parse_args(argv)
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    client = session.client("iam")
    failures = _run(client, args)
    if failures:
        print(f"{failures} FAIL")
        return 1
    print("ALL PASS")
    return 0


def _run(client: Any, args: argparse.Namespace) -> int:
    boundary = policy_arn(args.account, BOUNDARY_POLICY_NAME)
    exec_role = (
        f"arn:aws:iam::{args.account}:role/"
        f"cdk-oerfdev-cfn-exec-role-{args.account}-{args.region}"
    )
    identity = [
        json.dumps(execution_policy(args.account, args.region)),
        json.dumps(execution_deny_policy(args.account)),
    ]
    boundary_doc = json.dumps(boundary_policy(args.account, args.region))
    phone = phone_number_arn(args.region, args.account, args.phone_number_id)
    failures = 0
    for action, template, expect_allow, needs_boundary in EXEC_CHECKS:
        resource = _resource(template, args, boundary, phone)
        if action.startswith("sns:"):
            resource = args.inbound_topic_arn
        context = {"iam:PermissionsBoundary": boundary} if needs_boundary else {}
        failures += _expect(
            "live-exec",
            action,
            resource,
            expect_allow,
            _principal(client, exec_role, action, resource, context),
        )
        failures += _expect(
            "repo-exec",
            action,
            resource,
            expect_allow,
            _custom(client, identity, boundary_doc, action, resource, context),
        )
    allow_all = [json.dumps(_ALLOW_ALL)]
    for action, template, expect_allow in ROUTER_CHECKS:
        resource = template.format(
            account=args.account,
            region=args.region,
            phone=phone.rsplit("/", 1)[-1],
        )
        failures += _expect(
            "live-router",
            action,
            resource,
            expect_allow,
            _principal(client, args.router_role_arn, action, resource, {}),
        )
        failures += _expect(
            "boundary",
            action,
            resource,
            expect_allow,
            _custom(client, allow_all, boundary_doc, action, resource, {}),
        )
    for action, template, expect_allow, kind in STAGE_CHECKS:
        resource = template.format(
            account=args.account,
            region=args.region,
            user_pool_arn=args.user_pool_arn,
            distribution_arn=args.distribution_arn,
        )
        context = (
            {"aws:ResourceTag/oerf:stage": "dev"} if kind == "resource-tag" else {}
        )
        failures += _expect(
            "live-exec",
            action,
            resource,
            expect_allow,
            _principal(client, exec_role, action, resource, context),
        )
        failures += _expect(
            "repo-exec",
            action,
            resource,
            expect_allow,
            _custom(client, identity, boundary_doc, action, resource, context),
        )
        failures += _expect(
            "live-router",
            action,
            resource,
            expect_allow,
            _principal(client, args.router_role_arn, action, resource, context),
        )
        failures += _expect(
            "boundary",
            action,
            resource,
            expect_allow,
            _custom(client, allow_all, boundary_doc, action, resource, context),
        )
    for action, template, expect_allow in FENCE_CHECKS:
        resource = template.format(
            account=args.account,
            region=args.region,
            rest_api_id=args.rest_api_id,
            user_pool_arn=args.user_pool_arn,
        )
        failures += _expect(
            "live-exec",
            action,
            resource,
            expect_allow,
            _principal(client, exec_role, action, resource, {}),
        )
        failures += _expect(
            "repo-exec",
            action,
            resource,
            expect_allow,
            _custom(client, identity, boundary_doc, action, resource, {}),
        )
        failures += _expect(
            "live-router",
            action,
            resource,
            expect_allow,
            _principal(client, args.router_role_arn, action, resource, {}),
        )
        failures += _expect(
            "boundary",
            action,
            resource,
            expect_allow,
            _custom(client, allow_all, boundary_doc, action, resource, {}),
        )
    for action, template, expect_allow in RUNTIME_CHECKS:
        resource = template.format(
            dev_runtime_arn=args.dev_runtime_arn,
            main_runtime_arn=args.main_runtime_arn,
        )
        failures += _expect(
            "live-caller",
            action,
            resource,
            expect_allow,
            _principal(client, args.caller_role_arn, action, resource, {}),
        )
        failures += _expect(
            "boundary",
            action,
            resource,
            expect_allow,
            _custom(client, allow_all, boundary_doc, action, resource, {}),
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


def _principal(
    client: Any,
    role_arn: str,
    action: str,
    resource: str,
    context: dict[str, str],
) -> str:
    result = client.simulate_principal_policy(
        PolicySourceArn=role_arn,
        ActionNames=[action],
        ResourceArns=[resource],
        **_context(context),
    )
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
