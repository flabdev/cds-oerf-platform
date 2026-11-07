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
    EXEC_POLICY2_NAME,
    EXEC_POLICY_NAME,
    STAGE_TAG_KEY,
    STAGE_TAG_VALUE,
    STAR_ACTION_REASONS,
    _glob,
    apply_to_stack,
    bootstrap_ssm_parameter_arn,
    dev_name_patterns,
    is_workload_permissions_boundary,
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
    APPLY_CHECKS,
    EXEC_CHECKS,
    FENCE_CHECKS,
    GUARDRAIL_EXEC_CHECKS,
    ROUTER_CHECKS,
    RUNTIME_CHECKS,
    SERVICE_UNTAGGED_ALLOWS,
    SERVICE_UNTAGGED_DENIES,
    STAGE_CHECKS,
    TAGGED_POLICY_CHECKS,
)

ACCOUNT = "000000000000"
REGION = "us-east-1"
DEV_GUARDRAIL = f"arn:aws:bedrock:{REGION}:{ACCOUNT}:guardrail/b7nun5huxbii"
MAIN_GUARDRAIL = f"arn:aws:bedrock:{REGION}:{ACCOUNT}:guardrail/mainrail0001"
_CI_CONTEXT = {
    "github:oidcProviderArn": (
        "arn:aws:iam::000000000000:oidc-provider/token.actions.githubusercontent.com"
    ),
    "dev:guardrailArn": DEV_GUARDRAIL,
}
REPO_ROOT = Path(__file__).resolve().parents[2]
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


def test_lambda_env_kms_encrypt_is_allowed_with_lambda_alias() -> None:
    from infra.dev_boundary import intersection_allows

    key = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/c6e35e71-9663-43c8-af88-c21ee1b42b0a"
    context = {
        "kms:ResourceAliases": "alias/aws/lambda",
        "kms:ViaService": f"lambda.{REGION}.amazonaws.com",
    }
    assert intersection_allows("kms:Encrypt", key, ACCOUNT, REGION, context)
    assert not intersection_allows("kms:Encrypt", key, ACCOUNT, REGION, {})


def test_bootstrap_ssm_version_is_allowed_and_main_qualifier_is_not() -> None:
    from infra.dev_boundary import boundary_permits, intersection_allows

    dev_version = (
        f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/cdk-bootstrap/oerfdev/version"
    )
    main_version = (
        f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/cdk-bootstrap/hnb659fds/version"
    )
    for action in ("ssm:GetParameter", "ssm:GetParameters"):
        assert intersection_allows(action, dev_version, ACCOUNT, REGION)
        assert boundary_permits(action, dev_version, ACCOUNT, REGION)
        assert not intersection_allows(action, main_version, ACCOUNT, REGION)
        assert not boundary_permits(action, main_version, ACCOUNT, REGION)
    assert bootstrap_ssm_parameter_arn(ACCOUNT, REGION).endswith("/oerfdev/*")


def test_short_wildcards_cover_the_same_star_actions() -> None:
    from infra.dev_boundary import boundary_permits

    stream = (
        f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:"
        "/aws/bedrock-agentcore/runtimes/example:*"
    )
    assert boundary_permits("logs:CreateLogStream", stream, ACCOUNT, REGION)
    assert boundary_permits("logs:PutLogEvents", stream, ACCOUNT, REGION)
    for action in (
        "lambda:CreateEventSourceMapping",
        "lambda:ListEventSourceMappings",
        "logs:CreateLogDelivery",
        "logs:ListLogDeliveries",
        "logs:PutResourcePolicy",
        "logs:DeleteResourcePolicy",
        "logs:DescribeResourcePolicies",
    ):
        assert boundary_permits(action, "*", ACCOUNT, REGION), action
    assert not boundary_permits(
        "lambda:DeleteFunction",
        f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:oerf-step-context",
        ACCOUNT,
        REGION,
    )


def test_policy_documents_fit_the_managed_policy_quota() -> None:
    from infra.dev_boundary import (
        boundary_policy,
        execution_deny_policy,
        execution_policy,
        execution_policy2,
    )

    exec_doc = execution_policy(ACCOUNT, REGION, DEV_GUARDRAIL)
    exec2_doc = execution_policy2(ACCOUNT, REGION, DEV_GUARDRAIL)
    deny_doc = execution_deny_policy(ACCOUNT, DEV_GUARDRAIL)
    boundary_doc = boundary_policy(ACCOUNT, REGION, DEV_GUARDRAIL)
    exec_size = nonwhitespace_size(exec_doc)
    exec2_size = nonwhitespace_size(exec2_doc)
    deny_size = nonwhitespace_size(deny_doc)
    boundary_size = nonwhitespace_size(boundary_doc)
    assert exec_size <= 6144
    assert exec2_size <= 6144
    assert deny_size <= 6144
    assert boundary_size <= 6144
    assert 6144 - exec_size >= 500, f"OerfDevCfnExecPolicy slack too low: {6144 - exec_size}"
    assert 6144 - exec2_size >= 500, f"OerfDevCfnExecPolicy2 slack too low: {6144 - exec2_size}"
    slack = 6144 - boundary_size
    assert slack >= 3, f"OerfDevWorkloadBoundary slack below 3 chars: {slack}"


def test_exec_policy_resource_star_rows_cover_u_ec2d_kfh() -> None:
    from infra.dev_boundary import exec_policy_resource_star_rows, exec_policy_star_resource_actions

    rows = exec_policy_resource_star_rows()
    u_actions = {action for sid, action, _ in rows if sid == "U"}
    assert u_actions == set(exec_policy_star_resource_actions())
    assert any(
        sid == "EC2D" and action == "ec2:Describe*" for sid, action, _ in rows
    )
    kfh = {(sid, action) for sid, action, _ in rows if sid == "KFH"}
    assert ("KFH", "kinesis:*") in kfh
    assert ("KFH", "firehose:TagDeliveryStream") in kfh


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
    app = cdk.App(context=_CI_CONTEXT)
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
    assert set(policies) == {
        EXEC_POLICY_NAME,
        EXEC_POLICY2_NAME,
        EXEC_DENY_POLICY_NAME,
        BOUNDARY_POLICY_NAME,
    }
    exec_doc = policies[EXEC_POLICY_NAME]["Properties"]["PolicyDocument"]
    exec2_doc = policies[EXEC_POLICY2_NAME]["Properties"]["PolicyDocument"]
    deny_doc = policies[EXEC_DENY_POLICY_NAME]["Properties"]["PolicyDocument"]
    boundary_doc = policies[BOUNDARY_POLICY_NAME]["Properties"]["PolicyDocument"]
    bootstrap = bootstrap_ssm_parameter_arn(ACCOUNT, REGION)
    assert "BS" in json.dumps(exec_doc)
    assert "KFH" in json.dumps(exec_doc)
    assert "U" in json.dumps(exec2_doc)
    assert bootstrap in json.dumps(boundary_doc)
    _assert_role_mutation_conditions(exec_doc)
    _assert_required_denies(deny_doc)
    _assert_required_denies(boundary_doc)
    _assert_tag_denies(exec2_doc)
    _assert_tag_denies(boundary_doc)
    _assert_tag_actions(exec2_doc)
    _assert_tag_actions(boundary_doc)
    _assert_guardrail_arn_deny(deny_doc)
    _assert_guardrail_arn_deny(boundary_doc)
    for document in (exec2_doc, boundary_doc):
        create = _statement(document, "RT")
        assert "bedrock:CreateGuardrail" in _as_list(create["Action"])
        assert "aws:RequestTag/oerf:stage" in json.dumps(create["Condition"])
    _assert_no_guardrail_wildcard_in_tag_denies(exec2_doc)
    _assert_no_guardrail_wildcard_in_tag_denies(deny_doc)
    _assert_no_guardrail_wildcard_in_tag_denies(boundary_doc)
    assert all(
        item.get("Sid") != "GA" for item in _statements(exec2_doc)
    )
    assert "bedrock:GetGuardrail" in json.dumps(exec2_doc)
    assert "bedrock:UpdateGuardrail" in json.dumps(exec2_doc)
    assert "bedrock:CreateGuardrailVersion" in json.dumps(exec2_doc)
    assert "bedrock:DeleteGuardrail" in json.dumps(exec2_doc)
    assert "bedrock:TagResource" in json.dumps(exec2_doc)
    assert "bedrock:UntagResource" in json.dumps(exec2_doc)
    assert "bedrock:ApplyGuardrail" not in json.dumps(exec2_doc)
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
        assert EXEC_POLICY2_NAME not in blob
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


def test_dev_templates_only_resolve_bootstrap_version_from_ssm(dev_off) -> None:
    """CloudFormation reads bootstrap version as the execution role during deploy."""

    expected = "/cdk-bootstrap/oerfdev/version"
    for name, template in dev_off.items():
        if not name.endswith("-dev") or name == "OerfCiStack-dev":
            continue
        for param in template.get("Parameters", {}).values():
            if param.get("Type") != "AWS::SSM::Parameter::Value<String>":
                continue
            assert param.get("Default") == expected, name
        blob = json.dumps(template)
        assert "resolve:ssm:" not in blob, name
        assert "/cdk-bootstrap/hnb659fds/" not in blob, name


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


def test_dev_guardrail_arn_is_required_and_must_match_the_stack() -> None:
    from infra.dev_boundary import parse_guardrail_arn

    assert parse_guardrail_arn(DEV_GUARDRAIL, account=ACCOUNT, region=REGION) == DEV_GUARDRAIL
    with pytest.raises(ValueError, match="dev:guardrailArn"):
        parse_guardrail_arn(None, account=ACCOUNT, region=REGION)
    with pytest.raises(ValueError, match="dev:guardrailArn"):
        parse_guardrail_arn("   ", account=ACCOUNT, region=REGION)
    other_account = f"arn:aws:bedrock:{REGION}:111111111111:guardrail/b7nun5huxbii"
    with pytest.raises(ValueError, match="guardrail"):
        parse_guardrail_arn(other_account, account=ACCOUNT, region=REGION)
    versioned = f"{DEV_GUARDRAIL}:1"
    with pytest.raises(ValueError, match="guardrail"):
        parse_guardrail_arn(versioned, account=ACCOUNT, region=REGION)


def test_guardrail_calls_follow_the_arn_without_a_tag() -> None:
    from infra.dev_boundary import boundary_permits, intersection_allows

    profile = (
        f"arn:aws:bedrock:{REGION}:{ACCOUNT}:guardrail-profile/us.guardrail.v1:0"
    )
    west = f"arn:aws:bedrock:us-west-2:{ACCOUNT}:guardrail-profile/us.guardrail.v1:0"
    other_profile = (
        "arn:aws:bedrock:us-east-1:111111111111:guardrail-profile/us.guardrail.v1:0"
    )
    kwargs = {"guardrail_arn": DEV_GUARDRAIL}
    for action in (
        "bedrock:GetGuardrail",
        "bedrock:UpdateGuardrail",
        "bedrock:CreateGuardrailVersion",
        "bedrock:DeleteGuardrail",
        "bedrock:TagResource",
        "bedrock:UntagResource",
    ):
        assert intersection_allows(action, DEV_GUARDRAIL, ACCOUNT, REGION, **kwargs)
        assert intersection_allows(action, f"{DEV_GUARDRAIL}:1", ACCOUNT, REGION, **kwargs)
        assert not intersection_allows(action, MAIN_GUARDRAIL, ACCOUNT, REGION, **kwargs)
    assert boundary_permits(
        "bedrock:ApplyGuardrail", DEV_GUARDRAIL, ACCOUNT, REGION, **kwargs
    )
    assert boundary_permits("bedrock:ApplyGuardrail", profile, ACCOUNT, REGION, **kwargs)
    assert boundary_permits("bedrock:ApplyGuardrail", west, ACCOUNT, REGION, **kwargs)
    assert not boundary_permits(
        "bedrock:ApplyGuardrail", MAIN_GUARDRAIL, ACCOUNT, REGION, **kwargs
    )
    assert not boundary_permits(
        "bedrock:ApplyGuardrail", other_profile, ACCOUNT, REGION, **kwargs
    )
    assert boundary_permits("bedrock:ListGuardrails", "*", ACCOUNT, REGION, **kwargs)
    assert intersection_allows("bedrock:ListGuardrails", "*", ACCOUNT, REGION, **kwargs)
    assert not intersection_allows("bedrock:CreateGuardrail", "*", ACCOUNT, REGION, **kwargs)
    assert intersection_allows(
        "bedrock:CreateGuardrail",
        "*",
        ACCOUNT,
        REGION,
        {"aws:RequestTag/oerf:stage": "dev"},
        **kwargs,
    )


def test_offline_check_cases_match_the_documents() -> None:
    from infra.dev_boundary import boundary_permits, intersection_allows

    boundary = f"arn:aws:iam::{ACCOUNT}:policy/{BOUNDARY_POLICY_NAME}"
    context = {"iam:PermissionsBoundary": boundary}
    kwargs = {"guardrail_arn": DEV_GUARDRAIL}
    values = _check_values()
    for action, resource, expect_allow, needs_boundary in EXEC_CHECKS:
        current = context if needs_boundary else {}
        concrete = resource.format(account=ACCOUNT, region=REGION, boundary=boundary)
        got = intersection_allows(action, concrete, ACCOUNT, REGION, current, **kwargs)
        assert got is expect_allow, action
    for action, resource, expect_allow in ROUTER_CHECKS:
        concrete = resource.format(account=ACCOUNT, region=REGION, phone="0000000000")
        assert boundary_permits(action, concrete, ACCOUNT, REGION, **kwargs) is expect_allow
    for action, resource, expect_allow in (
        *STAGE_CHECKS,
        *FENCE_CHECKS,
        *GUARDRAIL_EXEC_CHECKS,
        *SERVICE_UNTAGGED_DENIES,
        *SERVICE_UNTAGGED_ALLOWS,
    ):
        concrete = resource.format(**values)
        assert (
            intersection_allows(action, concrete, ACCOUNT, REGION, **kwargs) is expect_allow
        ), action
        assert (
            boundary_permits(action, concrete, ACCOUNT, REGION, **kwargs) is expect_allow
        ), action
    for action, resource, expect_allow in APPLY_CHECKS:
        concrete = resource.format(**values)
        assert (
            boundary_permits(action, concrete, ACCOUNT, REGION, **kwargs) is expect_allow
        ), action
    for action, resource, expect_allow, tag_context in TAGGED_POLICY_CHECKS:
        concrete = resource.format(**values)
        assert (
            intersection_allows(
                action, concrete, ACCOUNT, REGION, tag_context, **kwargs
            )
            is expect_allow
        ), action
        assert (
            boundary_permits(action, concrete, ACCOUNT, REGION, tag_context, **kwargs)
            is expect_allow
        ), action
    for action, resource, expect_allow in RUNTIME_CHECKS:
        concrete = resource.format(**values)
        assert (
            intersection_allows(action, concrete, ACCOUNT, REGION, **kwargs) is expect_allow
        )
        assert boundary_permits(action, concrete, ACCOUNT, REGION, **kwargs) is expect_allow
    assert intersection_allows(
        "cloudfront:CreateOriginAccessControl", "*", ACCOUNT, REGION, **kwargs
    )
    assert boundary_permits(
        "cloudfront:CreateOriginAccessControl", "*", ACCOUNT, REGION, **kwargs
    )
    assert intersection_allows(
        "cloudfront:DeleteOriginAccessControl", "*", ACCOUNT, REGION, **kwargs
    )


def _check_values() -> dict[str, str]:
    profile = f"arn:aws:bedrock:{REGION}:{ACCOUNT}:guardrail-profile/us.guardrail.v1:0"
    west = f"arn:aws:bedrock:us-west-2:{ACCOUNT}:guardrail-profile/us.guardrail.v1:0"
    return {
        "account": ACCOUNT,
        "region": REGION,
        "boundary": f"arn:aws:iam::{ACCOUNT}:policy/{BOUNDARY_POLICY_NAME}",
        "phone": "0000000000",
        "user_pool_arn": f"arn:aws:cognito-idp:{REGION}:{ACCOUNT}:userpool/us-east-1_MAIN",
        "dev_user_pool_arn": f"arn:aws:cognito-idp:{REGION}:{ACCOUNT}:userpool/us-east-1_DEV",
        "distribution_arn": f"arn:aws:cloudfront::{ACCOUNT}:distribution/EMAIN",
        "dev_distribution_arn": f"arn:aws:cloudfront::{ACCOUNT}:distribution/EDEV",
        "rest_api_id": "mainapi",
        "dev_rest_api_id": "devapi",
        "http_api_id": "mainhttp",
        "dev_http_api_id": "devhttp",
        "ws_api_id": "mainws",
        "dev_ws_api_id": "devws",
        "dev_guardrail_arn": DEV_GUARDRAIL,
        "main_guardrail_arn": MAIN_GUARDRAIL,
        "profile_arn": profile,
        "profile_west_arn": west,
        "dev_runtime_arn": (
            f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/oerf_dev_agent"
        ),
        "main_runtime_arn": (
            f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/oerf_agent"
        ),
    }


def _is_boundary_arn(value: object) -> bool:
    """CDK renders an imported policy ARN with the partition token."""

    return is_workload_permissions_boundary(value, ACCOUNT)


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
        "kms:Schedule*",
        "kms:CreateKey",
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
    statement = _statement(document, "TD")
    actions = set(_as_list(statement["Action"]))
    assert {"cognito-idp:*", "apigatewayv2:*", "cloudfront:*"} <= actions
    assert "bedrock:TagResource" not in actions
    assert "bedrock:UntagResource" not in actions
    assert "bedrock:*Guardrail*" not in actions
    assert "arn:aws:bedrock:*:*:guardrail/*" not in _as_list(statement["Resource"])
    assert "aws:ResourceTag/oerf:stage" in json.dumps(statement["Condition"])


def _assert_guardrail_arn_deny(document: dict) -> None:
    statement = _statement(document, "GA")
    assert statement["Effect"] == "Deny"
    assert "Condition" not in statement
    assert "Resource" not in statement
    actions = set(_as_list(statement["Action"]))
    assert "bedrock:CreateGuardrail" not in actions
    assert "bedrock:*Guardrail*" not in actions
    assert {
        "bedrock:GetGuardrail",
        "bedrock:UpdateGuardrail",
        "bedrock:ApplyGuardrail",
        "bedrock:CreateGuardrailVersion",
        "bedrock:DeleteGuardrail",
        "bedrock:ListTagsForResource",
        "bedrock:*agResource",
    } <= actions
    assert "bedrock:ListGuardrails" not in actions
    assert statement["NotResource"] == [
        DEV_GUARDRAIL,
        f"{DEV_GUARDRAIL}:*",
        f"arn:aws:bedrock:*:{ACCOUNT}:guardrail-profile/*",
    ]


def _assert_no_guardrail_wildcard_in_tag_denies(document: dict) -> None:
    for statement in _statements(document):
        condition = json.dumps(statement.get("Condition", {}))
        if "aws:ResourceTag/" not in condition and "aws:RequestTag/" not in condition:
            continue
        for action in _as_list(statement.get("Action")):
            assert action != "bedrock:*Guardrail*", statement.get("Sid")


def _assert_outright_denies(document: dict) -> None:
    tags = _statement(document, "DAT")
    assert tags["Effect"] == "Deny"
    assert "apigateway:*" in _as_list(tags["Action"])
    assert "arn:aws:apigateway:*::/tags/*" in _as_list(tags["Resource"])
    assert "Condition" not in tags


def _assert_tag_denies(document: dict) -> None:
    blob = json.dumps(document)
    assert "TD" in blob
    assert "RT" in blob
    assert "NAU" in blob
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
    assert "NOD" in blob
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
