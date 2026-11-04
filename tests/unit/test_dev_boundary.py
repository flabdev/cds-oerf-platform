"""Dev execution policy and workload permissions boundary. No AWS calls."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infra.dev_boundary import (
    _ROLE_MUTATIONS,
    BOUNDARY_POLICY_NAME,
    EXEC_DENY_POLICY_NAME,
    EXEC_POLICY_NAME,
    STAGE_TAG_KEY,
    STAGE_TAG_VALUE,
    STAR_ACTION_REASONS,
    _glob,
    apply_to_stack,
    dev_name_patterns,
    nonwhitespace_size,
    runtime_violations,
)
from infra.naming import Stage
from infra.stacks.api_stack import OerfApiStack
from infra.stacks.ci_stack import OerfCiStack
from infra.stacks.compute_stack import OerfComputeStack
from infra.stacks.data_stack import OerfDataStack
from infra.stacks.observability import OerfObservabilityStack, lambda_functions
from scripts.check_dev_boundary import (
    EXEC_CHECKS,
    FENCE_CHECKS,
    ROUTER_CHECKS,
    RUNTIME_CHECKS,
    STAGE_CHECKS,
)

ACCOUNT = "000000000000"
REGION = "us-east-1"
REPO_ROOT = Path(__file__).resolve().parents[2]
BOUNDARY_ARN = f"arn:aws:iam::{ACCOUNT}:policy/{BOUNDARY_POLICY_NAME}"
PHONE = "phone-number-id-0000000000"
TOPIC = f"arn:aws:sns:{REGION}:{ACCOUNT}:OerfComputeStack-InboundTopic89167089-AbCd"
_FORBIDDEN_MANAGED = ("AdministratorAccess", "PowerUserAccess", "IAMFullAccess")
_FORBIDDEN_TYPES = ("AWS::IAM::User", "AWS::IAM::AccessKey", "AWS::IAM::Group")
_DEV_FLAGS = {
    "account": ACCOUNT,
    "stage": "dev",
    "agentcore:enabled": "true",
    "dashboard:enabled": "true",
    "auth:managedLogin": "true",
    "whatsapp:phoneNumberId": PHONE,
    "whatsapp:sharedInboundTopicArn": TOPIC,
}
_DEV_OFF = {**_DEV_FLAGS, "channels:live": "false", "demo:seedDevices": "true"}
_DEV_ON = {**_DEV_FLAGS, "channels:live": "true"}
_DEV_MIN = {"account": ACCOUNT, "stage": "dev", "channels:live": "false"}
_MAIN_A = {
    "account": ACCOUNT,
    "stage": "main",
    "agentcore:enabled": "true",
    "dashboard:enabled": "true",
    "channels:live": "false",
    "whatsapp:wabaId": "waba-0000000000",
    "whatsapp:phoneNumberId": PHONE,
    "alarms:email": "alarms@example.com",
}
_MAIN_B = {
    "account": ACCOUNT,
    "stage": "main",
    "agentcore:enabled": "false",
    "dashboard:enabled": "false",
    "channels:live": "false",
}
_MAIN_SEED = {**_MAIN_A, "demo:seedDevices": "true"}
_TAGGABLE = (
    "AWS::Lambda::Function",
    "AWS::IAM::Role",
    "AWS::DynamoDB::Table",
    "AWS::S3::Bucket",
    "AWS::SQS::Queue",
    "AWS::SNS::Topic",
    "AWS::Logs::LogGroup",
    "AWS::StepFunctions::StateMachine",
    "AWS::Cognito::UserPool",
    "AWS::Events::EventBus",
    "AWS::Events::Rule",
    "AWS::CloudWatch::Alarm",
    "AWS::CloudWatch::Dashboard",
    "AWS::ApiGateway::RestApi",
    "AWS::ApiGatewayV2::Api",
    "AWS::CloudFront::Distribution",
    "AWS::Bedrock::Guardrail",
    "AWS::SSM::Parameter",
)


def _run(outdir: Path, context: dict[str, str]) -> dict[str, dict]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "CDK_DEFAULT_ACCOUNT",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_PROFILE",
            "AWS_DEFAULT_PROFILE",
        }
    }
    env.update(
        {
            "CDK_OUTDIR": str(outdir),
            "CDK_DEFAULT_ACCOUNT": ACCOUNT,
            "CDK_DEFAULT_REGION": REGION,
            "AWS_EC2_METADATA_DISABLED": "true",
            "CDK_CONTEXT_JSON": json.dumps(context),
            "JSII_RUNTIME_PACKAGE_CACHE_ROOT": str(REPO_ROOT / ".jsii-cache"),
        }
    )
    completed = subprocess.run(
        [sys.executable, "-m", "infra.app"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    found = {}
    for path in outdir.glob("*.template.json"):
        found[path.name.removesuffix(".template.json")] = json.loads(path.read_text())
    return found


@pytest.fixture(scope="module")
def dev_off(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    return _run(tmp_path_factory.mktemp("dev-off"), _DEV_OFF)


@pytest.fixture(scope="module")
def dev_on(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    return _run(tmp_path_factory.mktemp("dev-on"), _DEV_ON)


@pytest.fixture(scope="module")
def dev_min(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    return _run(tmp_path_factory.mktemp("dev-min"), _DEV_MIN)


@pytest.fixture(scope="module")
def main_a(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    return _run(tmp_path_factory.mktemp("main-a"), _MAIN_A)


@pytest.fixture(scope="module")
def main_b(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    return _run(tmp_path_factory.mktemp("main-b"), _MAIN_B)


@pytest.fixture(scope="module")
def main_seed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    return _run(tmp_path_factory.mktemp("main-seed"), _MAIN_SEED)


def test_policy_documents_fit_the_managed_policy_quota() -> None:
    from infra.dev_boundary import boundary_policy, execution_deny_policy, execution_policy

    assert nonwhitespace_size(execution_policy(ACCOUNT, REGION)) <= 6144
    assert nonwhitespace_size(execution_deny_policy(ACCOUNT)) <= 6144
    assert nonwhitespace_size(boundary_policy(ACCOUNT, REGION)) <= 6144


def test_every_dev_role_has_the_workload_boundary(dev_off, dev_on, dev_min) -> None:
    seen = 0
    for templates in (dev_off, dev_on, dev_min):
        names = [name for name in templates if name.endswith("-dev") and name != "OerfCiStack-dev"]
        assert names
        for name in names:
            for resource in templates[name]["Resources"].values():
                if resource["Type"] != "AWS::IAM::Role":
                    continue
                seen += 1
                assert _is_boundary_arn(resource["Properties"].get("PermissionsBoundary")), name
    assert seen > 0


def test_desk_publish_roles_have_the_boundary(tmp_path, monkeypatch) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("export {}", encoding="utf-8")
    monkeypatch.setattr("infra.stacks.api_stack.DESK_DIST", dist)
    templates = _app_templates(
        tmp_path / "out",
        {
            **_DEV_OFF,
            "desk:publish": "true",
            "demo:seedDevices": "false",
        },
        desk_dist=dist,
    )
    deployment_roles = []
    for template in templates.values():
        for logical_id, resource in template["Resources"].items():
            if resource["Type"] != "AWS::IAM::Role":
                continue
            assert _is_boundary_arn(resource["Properties"].get("PermissionsBoundary"))
            if "BucketDeployment" in logical_id:
                deployment_roles.append(logical_id)
    assert deployment_roles


def test_dev_templates_have_no_admin_identity(dev_off, dev_on, dev_min) -> None:
    for templates in (dev_off, dev_on, dev_min):
        for name, template in templates.items():
            if name == "OerfCiStack-dev":
                continue
            assert not name.endswith("-dev") or _app_template_is_narrow(template)


def test_runtime_actions_match_the_boundary(dev_off, dev_on, dev_min) -> None:
    for templates in (dev_off, dev_on):
        for name, template in templates.items():
            if not name.endswith("-dev") or name == "OerfCiStack-dev":
                continue
            assert runtime_violations(template, ACCOUNT, REGION) == []
    gaps = [
        item
        for template in dev_min.values()
        for item in runtime_violations(template, ACCOUNT, REGION)
    ]
    assert gaps
    for item in gaps:
        assert "social-messaging:" in item
        assert item.endswith(" *")


def test_a_future_main_name_fails_the_boundary_check() -> None:
    template = {
        "Resources": {
            "Bad": {
                "Type": "AWS::IAM::Policy",
                "Properties": {
                    "PolicyDocument": {
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": "lambda:InvokeFunction",
                                "Resource": (
                                    f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:oerf-step-context"
                                ),
                            }
                        ]
                    }
                },
            }
        }
    }
    assert runtime_violations(template, ACCOUNT, REGION)


def test_unscoped_star_action_is_allowed_and_a_scoped_star_is_not() -> None:
    allowed = _star_template("xray:PutTraceSegments")
    blocked = _star_template("lambda:InvokeFunction")
    assert runtime_violations(allowed, ACCOUNT, REGION) == []
    assert runtime_violations(blocked, ACCOUNT, REGION)


def test_ci_stack_policies_and_github_role_untouched() -> None:
    app = cdk.App(
        context={
            "github:oidcProviderArn": (
                "arn:aws:iam::000000000000:oidc-provider/"
                "token.actions.githubusercontent.com"
            )
        }
    )
    stack = OerfCiStack(
        app,
        "OerfCiStack-dev",
        stage=Stage("dev"),
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )
    template = Template.from_stack(stack).to_json()
    policies = {
        resource["Properties"]["ManagedPolicyName"]: resource
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::IAM::ManagedPolicy"
    }
    assert set(policies) == {EXEC_POLICY_NAME, EXEC_DENY_POLICY_NAME, BOUNDARY_POLICY_NAME}
    exec_doc = policies[EXEC_POLICY_NAME]["Properties"]["PolicyDocument"]
    deny_doc = policies[EXEC_DENY_POLICY_NAME]["Properties"]["PolicyDocument"]
    boundary_doc = policies[BOUNDARY_POLICY_NAME]["Properties"]["PolicyDocument"]
    _assert_role_mutation_conditions(exec_doc)
    _assert_required_denies(deny_doc)
    _assert_required_denies(boundary_doc)
    _assert_tag_denies(exec_doc)
    _assert_tag_denies(boundary_doc)
    _assert_tag_actions(exec_doc)
    _assert_tag_actions(boundary_doc)
    _assert_outright_denies(deny_doc)
    _assert_outright_denies(boundary_doc)
    _assert_boundary_covers_services(boundary_doc)
    github = [
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::IAM::Role"
    ]
    assert len(github) == 1
    assert "PermissionsBoundary" not in github[0]["Properties"]
    assert github[0]["Properties"]["RoleName"] == "OerfGitHubDeployRole-dev"


def test_main_templates_have_no_permissions_boundary(main_a, main_b) -> None:
    for templates in (main_a, main_b):
        blob = json.dumps(templates)
        assert "PermissionsBoundary" not in blob
        assert BOUNDARY_POLICY_NAME not in blob
        assert EXEC_POLICY_NAME not in blob
        assert EXEC_DENY_POLICY_NAME not in blob
        assert STAGE_TAG_KEY not in blob


def test_dev_resources_carry_the_stage_tag(dev_off, dev_on, dev_min) -> None:
    seen = set()
    for templates in (dev_off, dev_on, dev_min):
        for name, template in templates.items():
            if not name.endswith("-dev") or name == "OerfCiStack-dev":
                continue
            for logical_id, resource in template["Resources"].items():
                if resource["Type"] not in _TAGGABLE:
                    continue
                seen.add(resource["Type"])
                assert _has_stage_tag(resource), f"{name} {logical_id}"
    assert "AWS::Lambda::Function" in seen
    assert "AWS::Cognito::UserPool" in seen
    assert "AWS::CloudFront::Distribution" in seen
    assert "AWS::Bedrock::Guardrail" in seen
    created = {"AWS::ApiGateway::RestApi": False, "AWS::ApiGatewayV2::Api": False}
    for template in dev_off.values():
        for resource in template["Resources"].values():
            if resource["Type"] in created and _has_stage_tag(resource.get("Properties", {})):
                created[resource["Type"]] = True
    assert created == {"AWS::ApiGateway::RestApi": True, "AWS::ApiGatewayV2::Api": True}


def test_main_names_do_not_match_dev_patterns(main_a, main_b, main_seed) -> None:
    patterns = dev_name_patterns(ACCOUNT)
    seed_arn = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:oerf-device-seed"
    log_arn = f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/oerf-device-seed"
    assert not any(_glob(pattern, seed_arn) for pattern in patterns)
    assert not any(_glob(pattern, log_arn) for pattern in patterns)
    found_seed = False
    for templates in (main_a, main_b, main_seed):
        for name, template in templates.items():
            for value in _strings(template):
                if value == "oerf-device-seed":
                    found_seed = True
                if value.startswith("arn:"):
                    matched = [pattern for pattern in patterns if _glob(pattern, value)]
                    assert not matched, f"{name} {value} matched {matched}"
            for value in _physical_names(template):
                for candidate in _name_arns(value):
                    matched = [pattern for pattern in patterns if _glob(pattern, candidate)]
                    assert not matched, f"{name} {value} matched {matched}"
    assert found_seed


def test_offline_check_cases_match_the_documents() -> None:
    from infra.dev_boundary import boundary_permits, intersection_allows

    boundary = f"arn:aws:iam::{ACCOUNT}:policy/{BOUNDARY_POLICY_NAME}"
    context = {"iam:PermissionsBoundary": boundary}
    for action, resource, expect_allow, needs_boundary in EXEC_CHECKS:
        current = context if needs_boundary else {}
        concrete = resource.format(account=ACCOUNT, region=REGION, boundary=boundary)
        got = intersection_allows(action, concrete, ACCOUNT, REGION, current)
        assert got is expect_allow, action
    for action, resource, expect_allow in ROUTER_CHECKS:
        concrete = resource.format(account=ACCOUNT, region=REGION, phone="0000000000")
        assert boundary_permits(action, concrete, ACCOUNT, REGION) is expect_allow
    pool = f"arn:aws:cognito-idp:{REGION}:{ACCOUNT}:userpool/us-east-1_MAIN"
    distribution = f"arn:aws:cloudfront::{ACCOUNT}:distribution/EMAIN"
    for action, resource, expect_allow, kind in STAGE_CHECKS:
        concrete = resource.format(
            account=ACCOUNT,
            region=REGION,
            user_pool_arn=pool,
            distribution_arn=distribution,
        )
        context = {"aws:ResourceTag/oerf:stage": "dev"} if kind == "resource-tag" else {}
        assert intersection_allows(action, concrete, ACCOUNT, REGION, context) is expect_allow
        assert boundary_permits(action, concrete, ACCOUNT, REGION, context) is expect_allow
    for action, resource, expect_allow in FENCE_CHECKS:
        concrete = resource.format(
            account=ACCOUNT,
            region=REGION,
            rest_api_id="mainapi",
            user_pool_arn=pool,
        )
        assert intersection_allows(action, concrete, ACCOUNT, REGION) is expect_allow
        assert boundary_permits(action, concrete, ACCOUNT, REGION) is expect_allow
    dev_runtime = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/oerf_dev_agent"
    main_runtime = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/oerf_agent"
    for action, resource, expect_allow in RUNTIME_CHECKS:
        concrete = resource.format(dev_runtime_arn=dev_runtime, main_runtime_arn=main_runtime)
        assert intersection_allows(action, concrete, ACCOUNT, REGION) is expect_allow
        assert boundary_permits(action, concrete, ACCOUNT, REGION) is expect_allow
    assert intersection_allows("cloudfront:CreateOriginAccessControl", "*", ACCOUNT, REGION)
    assert boundary_permits("cloudfront:CreateOriginAccessControl", "*", ACCOUNT, REGION)
    assert not intersection_allows(
        "cloudfront:DeleteOriginAccessControl", "*", ACCOUNT, REGION
    )


def _is_boundary_arn(value: object) -> bool:
    """CDK renders an imported policy ARN with the partition token."""

    expected = {
        "Fn::Join": [
            "",
            [
                "arn:",
                {"Ref": "AWS::Partition"},
                f":iam::{ACCOUNT}:policy/{BOUNDARY_POLICY_NAME}",
            ],
        ]
    }
    return value == BOUNDARY_ARN or value == expected


def _app_template_is_narrow(template: dict) -> bool:
    blob = json.dumps(template)
    for name in _FORBIDDEN_MANAGED:
        assert name not in blob
    for kind in _FORBIDDEN_TYPES:
        assert kind not in blob
    for resource in template["Resources"].values():
        if resource["Type"] != "AWS::IAM::Policy":
            continue
        for statement in _statements(resource["Properties"]["PolicyDocument"]):
            actions = _as_list(statement.get("Action"))
            resources = _as_list(statement.get("Resource"))
            assert not (actions == ["*"] and resources == ["*"])
    return True


def _assert_role_mutation_conditions(document: dict) -> None:
    mutations = set(_ROLE_MUTATIONS)
    seen = set()
    for statement in _statements(document):
        if statement.get("Effect", "Allow") != "Allow":
            continue
        actions = set(_as_list(statement.get("Action")))
        overlap = actions & mutations
        if not overlap:
            continue
        seen |= overlap
        assert "iam:PermissionsBoundary" in json.dumps(statement.get("Condition"))
    assert seen == mutations


def _assert_required_denies(document: dict) -> None:
    blob = json.dumps(document)
    for action in (
        "iam:DeleteRolePermissionsBoundary",
        "iam:CreatePolicyVersion",
        "iam:DeletePolicy",
        "iam:SetDefaultPolicyVersion",
        "iam:CreateUser",
        "iam:CreateAccessKey",
        "iam:CreateLoginProfile",
        "organizations:*",
        "account:*",
    ):
        assert action in blob
    assert "OerfGitHubDeployRole" in blob
    assert "cdk-hnb659fds-" in blob
    assert "cdk-oerfdev-" in blob
    assert "AdministratorAccess" in blob


def _statement(document: dict, sid: str) -> dict:
    for statement in _statements(document):
        if statement.get("Sid") == sid:
            return statement
    raise AssertionError(sid)


def _assert_tag_actions(document: dict) -> None:
    statement = _statement(document, "DenyExistingUnlessDevTag")
    actions = set(_as_list(statement["Action"]))
    assert {"cognito-idp:*", "apigatewayv2:*", "cloudfront:*"} <= actions
    assert {"bedrock:TagResource", "bedrock:UntagResource"} <= actions
    assert "aws:ResourceTag/oerf:stage" in json.dumps(statement["Condition"])


def _assert_outright_denies(document: dict) -> None:
    tags = _statement(document, "DenyApiGatewayTags")
    assert tags["Effect"] == "Deny"
    assert "apigateway:*" in _as_list(tags["Action"])
    assert "arn:aws:apigateway:*::/tags/*" in _as_list(tags["Resource"])
    assert "Condition" not in tags
    origin = _statement(document, "DenyOriginAccessControlWrite")
    assert origin["Effect"] == "Deny"
    assert set(_as_list(origin["Action"])) == {
        "cloudfront:UpdateOriginAccessControl",
        "cloudfront:DeleteOriginAccessControl",
    }
    assert "Condition" not in origin


def _assert_tag_denies(document: dict) -> None:
    blob = json.dumps(document)
    assert "DenyExistingUnlessDevTag" in blob
    assert "DenyCreateUnlessDevTag" in blob
    assert "DenyAdminCreateUser" in blob
    assert "aws:ResourceTag/oerf:stage" in blob
    assert "aws:RequestTag/oerf:stage" in blob
    for service in ("cognito-idp", "apigatewayv2", "cloudfront", "bedrock"):
        assert service in blob
    assert "apigateway:" in blob


def _has_stage_tag(resource: dict) -> bool:
    found = False

    def walk(node: object) -> None:
        nonlocal found
        if isinstance(node, dict):
            key = node.get("Key", node.get("key"))
            value = node.get("Value", node.get("value"))
            if key == STAGE_TAG_KEY and value == STAGE_TAG_VALUE:
                found = True
            if node.get(STAGE_TAG_KEY) == STAGE_TAG_VALUE:
                found = True
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(resource)
    return found


_PHYSICAL_NAME_KEYS = frozenset(
    {
        "AlarmName",
        "ApiName",
        "BucketName",
        "DashboardName",
        "Domain",
        "EventBusName",
        "FunctionName",
        "LogGroupName",
        "Name",
        "ParameterName",
        "PolicyName",
        "QueueName",
        "RepositoryName",
        "RestApiName",
        "RoleName",
        "RuleName",
        "StateMachineName",
        "StreamName",
        "TableName",
        "TopicName",
        "UserPoolClientName",
        "UserPoolName",
    }
)


def _physical_names(node: object) -> list[str]:
    found: list[str] = []

    def walk(item: object) -> None:
        if isinstance(item, dict):
            for key, value in item.items():
                if key in _PHYSICAL_NAME_KEYS and isinstance(value, str):
                    found.append(value)
                walk(value)
        elif isinstance(item, list):
            for value in item:
                walk(value)

    walk(node)
    return found


def _name_arns(name: str) -> list[str]:
    parameter = name if name.startswith("/") else f"/{name}"
    return [
        f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{name}",
        f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:{name}",
        f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/{name}",
        f"arn:aws:iam::{ACCOUNT}:role/{name}",
        f"arn:aws:s3:::{name}",
        f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{name}",
        f"arn:aws:sqs:{REGION}:{ACCOUNT}:{name}",
        f"arn:aws:sns:{REGION}:{ACCOUNT}:{name}",
        f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:{name}",
        f"arn:aws:events:{REGION}:{ACCOUNT}:event-bus/{name}",
        f"arn:aws:events:{REGION}:{ACCOUNT}:rule/{name}",
        f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:{name}",
        f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter{parameter}",
        f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/{name}",
        f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{name}",
    ]


def _strings(node: object) -> list[str]:
    found: list[str] = []

    def walk(item: object) -> None:
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, dict):
            for key, value in item.items():
                if isinstance(key, str):
                    found.append(key)
                walk(value)
        elif isinstance(item, list):
            for value in item:
                walk(value)

    walk(node)
    return found


def _assert_boundary_covers_services(document: dict) -> None:
    blob = json.dumps(document)
    for service in (
        "lambda",
        "dynamodb",
        "s3",
        "sqs",
        "sns",
        "states",
        "events",
        "logs",
        "ssm",
        "execute-api",
        "bedrock",
        "bedrock-agentcore",
        "social-messaging",
        "cloudwatch",
        "ecr",
        "cognito-idp",
        "apigateway",
        "cloudfront",
    ):
        assert service in blob
    assert "NoOutsideDev" in blob
    for action in STAR_ACTION_REASONS:
        assert action in blob


def _statements(document: dict) -> list[dict]:
    raw = document.get("Statement", [])
    if isinstance(raw, dict):
        return [raw]
    return list(raw)


def _as_list(value: object) -> list:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _star_template(action: str) -> dict:
    return {
        "Resources": {
            "One": {
                "Type": "AWS::IAM::Policy",
                "Properties": {
                    "PolicyDocument": {
                        "Statement": [
                            {"Effect": "Allow", "Action": action, "Resource": "*"}
                        ]
                    }
                },
            }
        }
    }


def _app_templates(outdir: Path, context: dict[str, str], desk_dist: Path) -> dict[str, dict]:
    """In-process synth so desk:publish can read a temporary build."""

    app = cdk.App(context=context, outdir=str(outdir))
    stage = Stage("dev")
    env = cdk.Environment(account=ACCOUNT, region=REGION)
    synthesizer = cdk.DefaultStackSynthesizer(qualifier=stage.dev_bootstrap_qualifier())
    data = OerfDataStack(
        app, "OerfDataStack-dev", stage=stage, env=env, synthesizer=synthesizer
    )
    compute = OerfComputeStack(
        app,
        "OerfComputeStack-dev",
        data_stack=data,
        stage=stage,
        env=env,
        synthesizer=synthesizer,
    )
    api = OerfApiStack(
        app,
        "OerfApiStack-dev",
        data_stack=data,
        inbound_queue=compute.inbound_queue,
        websocket_url=compute.ws_stage.url,
        stage=stage,
        env=env,
        synthesizer=synthesizer,
    )
    observability = OerfObservabilityStack(
        app,
        "OerfObservabilityStack-dev",
        functions=lambda_functions(data, compute, api),
        dead_letter_queues=[compute.inbound_dlq, compute.ingest_dlq, compute.stream_dlq],
        state_machines=[data.cold_chain_state_machine, data.engage_party_state_machine],
        channel_router_log_group=compute.channel_router_fn.log_group,
        stage=stage,
        env=env,
        synthesizer=synthesizer,
    )
    for stack in (data, compute, api, observability):
        apply_to_stack(stack)
    app.synth()
    found = {}
    for path in outdir.glob("*.template.json"):
        found[path.name.removesuffix(".template.json")] = json.loads(path.read_text())
    assert desk_dist.is_dir()
    return found
