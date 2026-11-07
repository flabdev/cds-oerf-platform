"""CloudFormation handler permissions are allowed by dev deploy policies."""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Template

from infra.cfn_handler_permission_tests import (
    _DEV_APP_TEMPLATE_TYPES,
    CUSTOM_RESOURCE_CFN_TYPE,
    bedrock_guardrail_has_kms_key_arn,
    check_handler_permissions,
    iter_handler_actions,
    load_handler_files,
)
from infra.dev_boundary import apply_to_stack
from infra.naming import Stage
from infra.stacks.api_stack import OerfApiStack
from infra.stacks.compute_stack import OerfComputeStack
from infra.stacks.data_stack import OerfDataStack
from infra.stacks.observability import OerfObservabilityStack, lambda_functions
from tests.unit.test_dev_boundary import DEV_GUARDRAIL

ACCOUNT = "000000000000"
REGION = "us-east-1"


def test_dev_templates_match_handler_fixture_types() -> None:
    """Fail when a new CFN resource type is added without a handler JSON."""

    merged = {
        "dashboard:enabled": "true",
        "agentcore:enabled": "true",
        "demo:seedDevices": "true",
    }
    app = cdk.App(context=merged)
    env = cdk.Environment(account=ACCOUNT, region=REGION)
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
    synth_types = set()
    for stack in (data, compute, api, obs):
        template = Template.from_stack(stack).to_json()
        for resource in template.get("Resources", {}).values():
            synth_types.add(resource["Type"])

    assert synth_types == _DEV_APP_TEMPLATE_TYPES, (
        "Update infra/cfn_handler_permission_tests._DEV_APP_TEMPLATE_TYPES and add "
        f"infra/bootstrap/iam-fixtures/cfn-handler-permissions.json for new types. "
        f"only_in_fixture={sorted(_DEV_APP_TEMPLATE_TYPES - synth_types)} "
        f"only_in_synth={sorted(synth_types - _DEV_APP_TEMPLATE_TYPES)}"
    )


def test_handler_json_exists_for_every_dev_template_type() -> None:
    fixture = (
        Path(__file__).resolve().parents[2]
        / "infra"
        / "bootstrap"
        / "iam-fixtures"
        / "cfn-handler-permissions.json"
    )
    documents = load_handler_files(fixture, types=_DEV_APP_TEMPLATE_TYPES)
    missing = sorted(_DEV_APP_TEMPLATE_TYPES - set(documents))
    assert not missing, f"Missing handler entry for: {missing}"


def test_handler_files_cover_at_least_one_action_or_are_custom_resource_empty() -> None:
    directory = (
        Path(__file__).resolve().parents[2]
        / "infra"
        / "bootstrap"
        / "iam-fixtures"
        / "cfn-handler-permissions.json"
    )
    documents = load_handler_files(directory, types=_DEV_APP_TEMPLATE_TYPES)
    for cfn_type, data in documents.items():
        actions = iter_handler_actions(data)
        if cfn_type == "AWS::CloudFormation::CustomResource":
            assert actions == [], cfn_type
        else:
            assert actions, f"{cfn_type} has no handler actions"


def test_dev_policies_allow_all_handler_permissions() -> None:
    merged = {
        "dashboard:enabled": "true",
        "agentcore:enabled": "true",
        "demo:seedDevices": "true",
    }
    app = cdk.App(context=merged)
    env = cdk.Environment(account=ACCOUNT, region=REGION)
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
    for stack in (data, compute, api, obs):
        apply_to_stack(stack)
    compute_template = Template.from_stack(compute).to_json()
    dev_templates = [Template.from_stack(s).to_json() for s in (data, compute, api, obs)]
    assert not bedrock_guardrail_has_kms_key_arn(compute_template), (
        "Dev guardrail must omit KmsKeyArn so customer-key KMS handler calls stay N/A"
    )

    result = check_handler_permissions(
        account=ACCOUNT,
        region=REGION,
        guardrail_arn=DEV_GUARDRAIL,
        kms_key_arn_set=bedrock_guardrail_has_kms_key_arn(compute_template),
        dev_templates=dev_templates,
    )
    assert result.failures == [], "Handler permission gaps:\n" + "\n".join(result.failures)
    assert any(
        item[0] == CUSTOM_RESOURCE_CFN_TYPE for item in result.skipped
    ), "expected CustomResource listed as not checked"
    assert any(
        row[2] == "not applicable: KmsKeyArn unset" for row in result.skipped
    ), "expected Bedrock handler kms:* skipped when KmsKeyArn is omitted"
    assert (
        "AWS::IAM::Role",
        "iam:DeleteRolePermissionsBoundary",
        "not applicable: every dev role sets PermissionsBoundary",
    ) in result.skipped
