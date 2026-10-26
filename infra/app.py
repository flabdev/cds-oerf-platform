"""CDK app entry point for the OERF data, compute, alarm, and optional API stacks."""

import os

import aws_cdk as cdk

from infra.stacks.api_stack import OerfApiStack
from infra.stacks.compute_stack import OerfComputeStack
from infra.stacks.data_stack import OerfDataStack
from infra.stacks.observability import OerfObservabilityStack, lambda_functions

# Synth succeeds with this account when no credentials and no context account
# are available. The rule below rejects a deploy of that placeholder.
SYNTH_PLACEHOLDER_ACCOUNT = "000000000000"
DEFAULT_REGION = "us-east-1"
PLACEHOLDER_DEPLOY_MESSAGE = (
    "Refusing to deploy with the synth placeholder account "
    "000000000000. Set context key 'account' or CDK_DEFAULT_ACCOUNT."
)


def _dashboard_enabled(app: cdk.App) -> bool:
    """The dashboard stack stays off unless context dashboard:enabled is true."""
    return app.node.try_get_context("dashboard:enabled") == "true"


def _reject_placeholder_deploy(stack: cdk.Stack) -> None:
    """Fail CloudFormation deploy when the stack account is the synth placeholder."""
    rule = cdk.CfnRule(stack, "RejectPlaceholderAccount")
    rule.add_assertion(
        cdk.Fn.condition_not(
            cdk.Fn.condition_equals(cdk.Aws.ACCOUNT_ID, SYNTH_PLACEHOLDER_ACCOUNT)
        ),
        PLACEHOLDER_DEPLOY_MESSAGE,
    )


def _resolve_account(app: cdk.App) -> str:
    """Context ``account`` wins over ``CDK_DEFAULT_ACCOUNT``.

    ``cdk synth -c account=000000000000`` stays on the placeholder even when
    the process has another account in the environment. Deploy of that
    placeholder is still rejected.
    """

    context_account = app.node.try_get_context("account")
    if isinstance(context_account, str) and context_account.strip():
        return context_account.strip()
    return os.environ.get("CDK_DEFAULT_ACCOUNT") or SYNTH_PLACEHOLDER_ACCOUNT


app = cdk.App()

account = _resolve_account(app)
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

stacks = [data_stack, compute_stack]
scopes = [data_stack, compute_stack]
if _dashboard_enabled(app):
    # Mudita owns api_stack.py. It stays out of the default synth until
    # dashboard:enabled=true. Actions use the compute inbound queue, and the
    # dashboard publishes that stack's WebSocket endpoint.
    api_stack = OerfApiStack(
        app,
        "OerfApiStack",
        data_stack=data_stack,
        inbound_queue=compute_stack.inbound_queue,
        websocket_url=compute_stack.ws_stage.url,
        env=cdk_env,
    )
    stacks.append(api_stack)
    scopes.append(api_stack)

stacks.append(
    OerfObservabilityStack(
        app,
        "OerfObservabilityStack",
        functions=lambda_functions(*scopes),
        dead_letter_queues=[
            compute_stack.inbound_dlq,
            compute_stack.ingest_dlq,
            compute_stack.stream_dlq,
        ],
        state_machines=[
            data_stack.cold_chain_state_machine,
            data_stack.engage_party_state_machine,
        ],
        channel_router_log_group=compute_stack.channel_router_fn.log_group,
        env=cdk_env,
    )
)

if account == SYNTH_PLACEHOLDER_ACCOUNT:
    for stack in stacks:
        _reject_placeholder_deploy(stack)

app.synth()
