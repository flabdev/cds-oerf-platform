"""GitHub deploy role for the dev stage.

Created only when stage is dev and context ``ci:deployRoleStack`` is true.
Ananda deploys this stack, and the dev toolkit stack, from a laptop. The dev
bootstrap deploy role denies CloudFormation on ``OerfCiStack-dev`` and denies
writes on ``CDKToolkit-dev``.
"""

from aws_cdk import CfnOutput, Duration, Stack, aws_iam as iam
from constructs import Construct

from infra.dev_boundary import (
    BOUNDARY_POLICY_NAME,
    EXEC_DENY_POLICY_NAME,
    EXEC_POLICY_NAME,
    GUARDRAIL_ARN_CONTEXT,
    boundary_policy,
    execution_deny_policy,
    execution_policy,
    parse_guardrail_arn,
)
from infra.naming import Stage, qualify_dev_names

_GITHUB_DOMAIN = "token.actions.githubusercontent.com"
_DEV_SUBJECTS = [
    "repo:flabdev/cds-oerf-platform:environment:dev",
    "repo:flabdev@25605151/cds-oerf-platform@1408373494:environment:dev",
]
_OIDC_CONTEXT = "github:oidcProviderArn"
_MISSING_ARN = (
    "CDK context 'github:oidcProviderArn' is required when "
    "ci:deployRoleStack=true. Pass the ARN of the existing GitHub OIDC "
    "provider. This stack does not create a provider."
)


class OerfCiStack(Stack):
    """Dev GitHub role that can assume only the oerfdev bootstrap roles."""

    def __init__(self, scope: Construct, construct_id: str, *, stage: Stage, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if stage.is_main:
            raise ValueError("OerfCiStack is created only for stage=dev")
        provider_arn = _oidc_provider_arn(self)
        provider = iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(
            self,
            "GithubOIDCProvider",
            provider_arn,
        )
        role = iam.Role(
            self,
            "GitHubDeployRole",
            role_name="OerfGitHubDeployRole-dev",
            max_session_duration=Duration.hours(1),
            assumed_by=iam.OpenIdConnectPrincipal(
                provider,
                conditions={
                    "StringEquals": {
                        f"{_GITHUB_DOMAIN}:aud": "sts.amazonaws.com",
                        f"{_GITHUB_DOMAIN}:sub": _DEV_SUBJECTS,
                    },
                },
            ),
            description="Assumed by GitHub Actions in the dev environment to deploy the dev stage",
        )
        # Four qualifier roles, not a wildcard. The execution role trusts only
        # CloudFormation, so it is not in this list. sts:TagSession is required
        # because this CLI assumes bootstrap roles with session tags.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="AssumeCdkBootstrapRoles",
                actions=["sts:AssumeRole", "sts:TagSession"],
                resources=[
                    _bootstrap_role_arn(self, stage, name) for name in _BOOTSTRAP_ROLES
                ],
            )
        )
        _dev_boundary_policies(self)
        CfnOutput(self, "GitHubDeployRoleArn", value=role.role_arn)
        qualify_dev_names(self)


def _dev_boundary_policies(stack: Stack) -> None:
    """Fixed-name policies the dev bootstrap attaches. Not applied to this stack's role."""

    guardrail_arn = parse_guardrail_arn(
        stack.node.try_get_context(GUARDRAIL_ARN_CONTEXT),
        account=stack.account,
        region=stack.region,
    )
    exec_policy = iam.CfnManagedPolicy(
        stack,
        "DevCfnExecPolicy",
        managed_policy_name=EXEC_POLICY_NAME,
        description="CloudFormation execution allows for the oerfdev dev stage",
        policy_document=execution_policy(stack.account, stack.region, guardrail_arn),
    )
    exec_denies = iam.CfnManagedPolicy(
        stack,
        "DevCfnExecDenies",
        managed_policy_name=EXEC_DENY_POLICY_NAME,
        description="CloudFormation execution denies for the oerfdev dev stage",
        policy_document=execution_deny_policy(stack.account, guardrail_arn),
    )
    boundary = iam.CfnManagedPolicy(
        stack,
        "DevWorkloadBoundary",
        managed_policy_name=BOUNDARY_POLICY_NAME,
        description="Permissions boundary for dev workload roles and the execution role",
        policy_document=boundary_policy(stack.account, stack.region, guardrail_arn),
    )
    CfnOutput(stack, "DevCfnExecPolicyArn", value=exec_policy.ref)
    CfnOutput(stack, "DevCfnExecDeniesArn", value=exec_denies.ref)
    CfnOutput(stack, "DevWorkloadBoundaryArn", value=boundary.ref)


_BOOTSTRAP_ROLES = (
    "deploy-role",
    "file-publishing-role",
    "image-publishing-role",
    "lookup-role",
)


def _oidc_provider_arn(scope: Construct) -> str:
    raw = scope.node.try_get_context(_OIDC_CONTEXT)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(_MISSING_ARN)
    return raw.strip()


def _bootstrap_role_arn(stack: Stack, stage: Stage, name: str) -> str:
    qualifier = stage.dev_bootstrap_qualifier()
    return (
        f"arn:aws:iam::{stack.account}:role/"
        f"cdk-{qualifier}-{name}-{stack.account}-{stack.region}"
    )
