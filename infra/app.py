"""CDK app entry point for the OERF data and compute stacks."""

import os

import aws_cdk as cdk

from infra.stacks.compute_stack import OerfComputeStack
from infra.stacks.data_stack import OerfDataStack

# Synth succeeds with this account when no credentials and no context account
# are available. The rule below rejects a deploy of that placeholder.
SYNTH_PLACEHOLDER_ACCOUNT = "000000000000"
DEFAULT_REGION = "us-east-1"
PLACEHOLDER_DEPLOY_MESSAGE = (
    "Refusing to deploy with the synth placeholder account "
    "000000000000. Set CDK_DEFAULT_ACCOUNT or context key 'account'."
)


def _reject_placeholder_deploy(stack: cdk.Stack) -> None:
    """Fail CloudFormation deploy when the stack account is the synth placeholder."""
    rule = cdk.CfnRule(stack, "RejectPlaceholderAccount")
    rule.add_assertion(
        cdk.Fn.condition_not(
            cdk.Fn.condition_equals(cdk.Aws.ACCOUNT_ID, SYNTH_PLACEHOLDER_ACCOUNT)
        ),
        PLACEHOLDER_DEPLOY_MESSAGE,
    )


app = cdk.App()

account = (
    os.environ.get("CDK_DEFAULT_ACCOUNT")
    or app.node.try_get_context("account")
    or SYNTH_PLACEHOLDER_ACCOUNT
)
region = (
    os.environ.get("CDK_DEFAULT_REGION")
    or app.node.try_get_context("region")
    or DEFAULT_REGION
)

cdk_env = cdk.Environment(account=account, region=region)

data_stack = OerfDataStack(
    app,
    "OerfDataStack",
    env=cdk_env,
)

compute_stack = OerfComputeStack(
    app,
    "OerfComputeStack",
    data_stack=data_stack,
    env=cdk_env,
)

if account == SYNTH_PLACEHOLDER_ACCOUNT:
    _reject_placeholder_deploy(data_stack)
    _reject_placeholder_deploy(compute_stack)

app.synth()
