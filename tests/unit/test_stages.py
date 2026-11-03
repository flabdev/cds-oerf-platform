"""Stage is required, main keeps today's names, and dev does not collide.

Synthesis uses the placeholder account. It does not call AWS.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ACCOUNT = "000000000000"
REGION = "us-east-1"
MAIN_STACKS = {
    "OerfDataStack",
    "OerfComputeStack",
    "OerfApiStack",
    "OerfObservabilityStack",
}
# CloudFormation property names that are physical names. AgentRuntimeName is
# the AgentCore property; RuntimeName is included for the same check.
_NAME_KEYS = frozenset(
    {
        "FunctionName",
        "StateMachineName",
        "Name",
        "TableName",
        "BucketName",
        "TopicName",
        "QueueName",
        "LogGroupName",
        "RoleName",
        "UserPoolName",
        "Domain",
        "AlarmName",
        "DashboardName",
        "ParameterName",
        "RuntimeName",
        "AgentRuntimeName",
        "PolicyName",
    }
)
_SET_A = {
    "stage": "main",
    "account": ACCOUNT,
    "agentcore:enabled": "true",
    "dashboard:enabled": "true",
    "channels:live": "false",
    "whatsapp:wabaId": "waba-0000000000",
    "whatsapp:phoneNumberId": "phone-number-id-0000000000",
    "alarms:email": "alarms@example.com",
}


def _run(outdir: Path, context: dict[str, str]) -> subprocess.CompletedProcess[str]:
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
    return subprocess.run(
        [sys.executable, "-m", "infra.app"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _templates(outdir: Path) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for path in outdir.glob("*.template.json"):
        found[path.name.removesuffix(".template.json")] = json.loads(path.read_text())
    return found


def _names(template: dict) -> set[str]:
    found: set[str] = set()
    for resource in template.get("Resources", {}).values():
        props = resource.get("Properties") or {}
        if not isinstance(props, dict):
            continue
        for key, value in props.items():
            if key in _NAME_KEYS and isinstance(value, str):
                found.add(value)
    for output in template.get("Outputs", {}).values():
        export_name = (output.get("Export") or {}).get("Name")
        if isinstance(export_name, str):
            found.add(export_name)
    return found


def test_missing_stage_fails_synthesis(tmp_path: Path) -> None:
    result = _run(tmp_path, {"account": ACCOUNT})
    assert result.returncode != 0
    assert "stage" in (result.stderr + result.stdout)
    assert "main" in (result.stderr + result.stdout)


def test_invalid_stage_fails_synthesis(tmp_path: Path) -> None:
    result = _run(tmp_path, {"account": ACCOUNT, "stage": "prod"})
    assert result.returncode != 0
    assert "prod" in (result.stderr + result.stdout)


def test_dev_rejects_whatsapp_waba_id(tmp_path: Path) -> None:
    context = {key: value for key, value in _SET_A.items() if key != "stage"}
    context["stage"] = "dev"
    result = _run(tmp_path, context)
    assert result.returncode != 0
    assert "whatsapp:wabaId" in (result.stderr + result.stdout)
    assert "stage=dev" in (result.stderr + result.stdout)


_SHARED_INBOUND = f"arn:aws:sns:{REGION}:{ACCOUNT}:main-inbound"


def test_main_rejects_shared_inbound_topic(tmp_path: Path) -> None:
    context = dict(_SET_A)
    context["whatsapp:sharedInboundTopicArn"] = _SHARED_INBOUND
    result = _run(tmp_path, context)
    assert result.returncode != 0
    text = result.stderr + result.stdout
    assert "whatsapp:sharedInboundTopicArn" in text
    assert "stage=main" in text


def test_shared_inbound_topic_rejects_another_account_or_region(tmp_path: Path) -> None:
    wrong_account = _run(
        tmp_path / "account",
        {
            "account": ACCOUNT,
            "stage": "dev",
            "whatsapp:sharedInboundTopicArn": (
                f"arn:aws:sns:{REGION}:111111111111:main-inbound"
            ),
        },
    )
    wrong_region = _run(
        tmp_path / "region",
        {
            "account": ACCOUNT,
            "stage": "dev",
            "whatsapp:sharedInboundTopicArn": (
                f"arn:aws:sns:eu-west-1:{ACCOUNT}:main-inbound"
            ),
        },
    )
    not_sns = _run(
        tmp_path / "sqs",
        {
            "account": ACCOUNT,
            "stage": "dev",
            "whatsapp:sharedInboundTopicArn": f"arn:aws:sqs:{REGION}:{ACCOUNT}:main-inbound",
        },
    )
    for result in (wrong_account, wrong_region, not_sns):
        assert result.returncode != 0
        assert "whatsapp:sharedInboundTopicArn" in (result.stderr + result.stdout)


def test_main_and_dev_names_do_not_overlap_and_dev_skips_singletons(tmp_path: Path) -> None:
    main_dir = tmp_path / "main"
    dev_dir = tmp_path / "dev"
    main_context = dict(_SET_A)
    dev_context = {key: value for key, value in _SET_A.items() if key != "whatsapp:wabaId"}
    dev_context["stage"] = "dev"
    main_result = _run(main_dir, main_context)
    dev_result = _run(dev_dir, dev_context)
    assert main_result.returncode == 0, main_result.stderr or main_result.stdout
    assert dev_result.returncode == 0, dev_result.stderr or dev_result.stdout
    main_templates = _templates(main_dir)
    dev_templates = _templates(dev_dir)
    assert set(main_templates) == MAIN_STACKS
    assert set(dev_templates) == {f"{name}-dev" for name in MAIN_STACKS}

    main_names = set().union(*(_names(template) for template in main_templates.values()))
    dev_names = set().union(*(_names(template) for template in dev_templates.values()))
    overlap = main_names & dev_names
    assert overlap == set(), f"shared physical names: {sorted(overlap)}"

    dev_blob = json.dumps(dev_templates)
    assert "AWS::IAM::OIDCProvider" not in dev_blob
    assert "Custom::AWSCDKOpenIdConnectProvider" not in dev_blob
    assert "OerfGitHubDeployRole" not in dev_blob
    assert "AWS::XRay::TransactionSearchConfig" not in dev_blob
    assert "TransactionSearchXRayAccess" not in dev_blob
    assert "oerf-waba-event-destination" not in dev_blob
    assert "oerf-dev-waba-event-destination" not in dev_blob
    assert "WabaId" not in dev_blob
    assert "AWS::ApiGateway::Account" not in dev_blob
    assert "OerfCiStack-dev" not in dev_templates
    _assert_bootstrap_qualifier(main_templates, "hnb659fds")
    _assert_bootstrap_qualifier(dev_templates, "oerfdev")


_OIDC_ARN = "arn:aws:iam::000000000000:oidc-provider/token.actions.githubusercontent.com"
_DEV_SUBJECTS = [
    "repo:flabdev/cds-oerf-platform:environment:dev",
    "repo:flabdev@25605151/cds-oerf-platform@1408373494:environment:dev",
]
_TOOLKIT_WRITE_DENY = frozenset(
    {
        "cloudformation:CreateChangeSet",
        "cloudformation:ExecuteChangeSet",
        "cloudformation:DeleteChangeSet",
        "cloudformation:CreateStack",
        "cloudformation:UpdateStack",
        "cloudformation:DeleteStack",
        "cloudformation:SetStackPolicy",
        "cloudformation:UpdateTerminationProtection",
        "cloudformation:CreateStackRefactor",
        "cloudformation:ExecuteStackRefactor",
        "cloudformation:ContinueUpdateRollback",
        "cloudformation:CancelUpdateStack",
        "cloudformation:RollbackStack",
    }
)
_BOOTSTRAP_ROLE_NAMES = (
    "deploy-role",
    "file-publishing-role",
    "image-publishing-role",
    "lookup-role",
)
_ACCOUNT_WIDE_CFN = frozenset(
    {
        "cloudformation:DescribeStacks",
        "cloudformation:ListStacks",
        "cloudformation:ValidateTemplate",
        "cloudformation:GetTemplateSummary",
    }
)


def _assert_bootstrap_qualifier(templates: dict[str, dict], qualifier: str) -> None:
    marker = f"/cdk-bootstrap/{qualifier}/version"
    other = "hnb659fds" if qualifier == "oerfdev" else "oerfdev"
    for name, template in templates.items():
        default = template["Parameters"]["BootstrapVersion"]["Default"]
        assert default == marker, name
        blob = json.dumps(template)
        assert f"cdk-{other}-" not in blob, name


def test_dev_ci_role_stack_is_opt_in(tmp_path: Path) -> None:
    plain = _run(tmp_path / "plain", {"account": ACCOUNT, "stage": "dev"})
    assert plain.returncode == 0, plain.stderr or plain.stdout
    assert "OerfCiStack-dev" not in _templates(tmp_path / "plain")

    missing = _run(
        tmp_path / "missing",
        {"account": ACCOUNT, "stage": "dev", "ci:deployRoleStack": "true"},
    )
    assert missing.returncode != 0
    assert "github:oidcProviderArn" in (missing.stderr + missing.stdout)

    present = _run(
        tmp_path / "present",
        {
            "account": ACCOUNT,
            "stage": "dev",
            "ci:deployRoleStack": "true",
            "github:oidcProviderArn": _OIDC_ARN,
        },
    )
    assert present.returncode == 0, present.stderr or present.stdout
    templates = _templates(tmp_path / "present")
    assert "OerfCiStack-dev" in templates
    _assert_ci_role(templates["OerfCiStack-dev"])
    _assert_bootstrap_qualifier({"OerfCiStack-dev": templates["OerfCiStack-dev"]}, "oerfdev")


def _assert_ci_role(template: dict) -> None:
    roles = [
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::IAM::Role"
        and resource["Properties"].get("RoleName") == "OerfGitHubDeployRole-dev"
    ]
    assert len(roles) == 1
    role = roles[0]["Properties"]
    assert role["MaxSessionDuration"] == 3600
    statement = role["AssumeRolePolicyDocument"]["Statement"]
    assert len(statement) == 1
    trust = statement[0]
    assert trust["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert trust["Principal"]["Federated"] == _OIDC_ARN
    conditions = trust["Condition"]["StringEquals"]
    assert conditions["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    assert conditions["token.actions.githubusercontent.com:sub"] == _DEV_SUBJECTS
    assert list(trust["Condition"]) == ["StringEquals"]

    policies = [
        resource["Properties"]
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::IAM::Policy"
    ]
    assert len(policies) == 1
    grants = policies[0]["PolicyDocument"]["Statement"]
    assert len(grants) == 1
    actions = grants[0]["Action"]
    assert set(actions) == {"sts:AssumeRole", "sts:TagSession"}
    expected = {
        f"arn:aws:iam::{ACCOUNT}:role/cdk-oerfdev-{name}-{ACCOUNT}-{REGION}"
        for name in _BOOTSTRAP_ROLE_NAMES
    }
    assert set(grants[0]["Resource"]) == expected
    assert "Custom::AWSCDKOpenIdConnectProvider" not in json.dumps(template)
    assert "AWS::IAM::OIDCProvider" not in json.dumps(template)


def test_dev_bootstrap_limits_cloudformation_to_dev_stacks() -> None:
    template = _load_bootstrap_role()
    statements = template["Resources"]["DeploymentActionRole"]["Properties"]["Policies"][0][
        "PolicyDocument"
    ]["Statement"]
    by_sid = {item["Sid"]: item for item in statements if "Sid" in item}
    deploy = by_sid["DeployPermissions"]
    resources = [_sub_value(item) for item in deploy["Resource"]]
    assert any(item.endswith(":stack/*-dev/*") for item in resources)
    assert any(":changeSet/*/*-dev*" in item for item in resources)
    assert not any("CDKToolkit-dev" in item for item in resources)
    for action in deploy["Action"]:
        assert action.startswith("cloudformation:")

    reads = by_sid["CloudFormationAccountReads"]
    assert reads["Resource"] == "*"
    assert set(reads["Action"]) == _ACCOUNT_WIDE_CFN

    denied = by_sid["DenyOerfCiStack"]
    assert denied["Effect"] == "Deny"
    assert denied["Action"] == "cloudformation:*"
    denied_resources = [_sub_value(item) for item in denied["Resource"]]
    assert any(item.endswith(":stack/OerfCiStack-dev/*") for item in denied_resources)
    assert any(":changeSet/*/OerfCiStack-dev*" in item for item in denied_resources)
    toolkit = by_sid["DenyToolkitWrites"]
    assert toolkit["Effect"] == "Deny"
    assert set(toolkit["Action"]) == _TOOLKIT_WRITE_DENY
    toolkit_resources = [_sub_value(item) for item in toolkit["Resource"]]
    assert any(item.endswith(":stack/CDKToolkit-dev/*") for item in toolkit_resources)
    assert any(":changeSet/*/CDKToolkit-dev*" in item for item in toolkit_resources)

    scoped = [
        action
        for item in statements
        if item.get("Effect", "Allow") == "Allow"
        for action in _as_list(item.get("Action"))
        if action.startswith("cloudformation:") and action not in _ACCOUNT_WIDE_CFN
    ]
    assert scoped
    for item in statements:
        if item.get("Effect", "Allow") != "Allow":
            continue
        for action in _as_list(item.get("Action")):
            if not action.startswith("cloudformation:") or action in _ACCOUNT_WIDE_CFN:
                continue
            for resource in _as_list(item["Resource"]):
                text = _sub_value(resource)
                assert text != "*"
                assert ":stack/*-dev/*" in text or ":changeSet/" in text
                assert "CDKToolkit-dev" not in text

    blob = (REPO_ROOT / "infra" / "bootstrap" / "dev-bootstrap.yaml").read_text()
    assert "cdk-${Qualifier}-assets-${AWS::AccountId}-${AWS::Region}" in blob
    assert "cdk-${Qualifier}-container-assets-${AWS::AccountId}-${AWS::Region}" in blob
    assert "AWSCloudFormationReadOnlyAccess" not in blob
    pass_role = next(
        item for item in statements if _as_list(item.get("Action")) == ["iam:PassRole"]
    )
    assert _sub_value(pass_role["Resource"]) == "${CloudFormationExecutionRole.Arn}"


def _sub_value(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "Fn::Sub" in value:
        sub = value["Fn::Sub"]
        assert isinstance(sub, str)
        return sub
    raise AssertionError(value)


def _as_list(value: object) -> list:
    if isinstance(value, list):
        return value
    return [value]


def _load_bootstrap_role() -> dict:
    text = (REPO_ROOT / "infra" / "bootstrap" / "dev-bootstrap.yaml").read_text()
    start = text.index("  DeploymentActionRole:\n")
    end = text.index("  CloudFormationExecutionRole:\n")
    parsed = _parse_yaml_block(text[start:end])
    assert isinstance(parsed, dict)
    return {"Resources": parsed}


def _parse_yaml_block(text: str) -> object:
    """Parse the block-style bootstrap role. The file has no flow collections there."""

    lines = [
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    value, index = _parse_at(lines, 0, _indent(lines[0]))
    assert index == len(lines)
    return value


def _parse_at(lines: list[str], index: int, indent: int) -> tuple[object, int]:
    if index >= len(lines) or _indent(lines[index]) < indent:
        return None, index
    if lines[index].lstrip().startswith("- "):
        items: list[object] = []
        while (
            index < len(lines)
            and _indent(lines[index]) == indent
            and lines[index].lstrip().startswith("- ")
        ):
            content = lines[index].lstrip()[2:]
            index += 1
            item, index = _parse_list_item(content, lines, index, indent)
            items.append(item)
        return items, index
    mapping: dict[str, object] = {}
    while (
        index < len(lines)
        and _indent(lines[index]) == indent
        and not lines[index].lstrip().startswith("- ")
    ):
        key, rest = _split_key(lines[index].lstrip())
        index += 1
        value, index = _parse_value(rest, lines, index, indent + 2)
        mapping[key] = value
    return mapping, index


def _parse_list_item(
    content: str, lines: list[str], index: int, indent: int
) -> tuple[object, int]:
    child = indent + 2
    if ": " not in content and not content.endswith(":"):
        return _scalar(content), index
    key, rest = _split_key(content)
    value, index = _parse_value(rest, lines, index, child + 2)
    mapping = {key: value}
    while (
        index < len(lines)
        and _indent(lines[index]) == child
        and not lines[index].lstrip().startswith("- ")
    ):
        next_key, next_rest = _split_key(lines[index].lstrip())
        index += 1
        next_value, index = _parse_value(next_rest, lines, index, child + 2)
        mapping[next_key] = next_value
    return mapping, index


def _parse_value(
    rest: str, lines: list[str], index: int, child_indent: int
) -> tuple[object, int]:
    if rest == "":
        if index < len(lines) and _indent(lines[index]) >= child_indent:
            return _parse_at(lines, index, _indent(lines[index]))
        return None, index
    return _scalar(rest), index


def _split_key(text: str) -> tuple[str, str]:
    if ": " in text:
        key, rest = text.split(": ", 1)
    elif text.endswith(":"):
        key, rest = text[:-1], ""
    else:
        raise ValueError(text)
    return _scalar(key) if isinstance(_scalar(key), str) else key, rest.strip()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _scalar(text: str) -> object:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    return text
