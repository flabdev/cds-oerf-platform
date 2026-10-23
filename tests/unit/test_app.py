"""Tests for the CDK app entry point (``infra/app.py``).

The app module is executed in a subprocess so its module-level guard can be
exercised without re-synthesizing inside the pytest process. ``CDK_OUTDIR`` is
redirected to a temporary directory to keep the repository clean. These tests
never make real AWS calls (see guide section 24).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

ACCOUNT = "123456789012"
REGION = "us-east-1"


def run_app(outdir: Path, **env_overrides: str) -> subprocess.CompletedProcess:
    """Run ``python -m infra.app`` with a controlled environment."""
    env = {
        key: value for key, value in os.environ.items() if key != "CDK_DEFAULT_ACCOUNT"
    }
    env.update(
        {
            "CDK_OUTDIR": str(outdir),
            "CDK_DEFAULT_REGION": REGION,
            **env_overrides,
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


def stack_names(outdir: Path) -> set[str]:
    """CloudFormation stack ids in a synth output directory."""
    manifest = json.loads((outdir / "manifest.json").read_text())
    return {
        name
        for name, artifact in manifest["artifacts"].items()
        if artifact.get("type") == "aws:cloudformation:stack"
    }


def test_app_synthesizes_with_placeholder_account_when_unresolved(tmp_path):
    result = run_app(tmp_path)

    assert result.returncode == 0, result.stderr
    assert stack_names(tmp_path) == {
        "OerfDataStack",
        "OerfComputeStack",
        "OerfObservabilityStack",
    }
    message = "Refusing to deploy with the synth placeholder account 000000000000"
    for name in (
        "OerfDataStack.template.json",
        "OerfComputeStack.template.json",
        "OerfObservabilityStack.template.json",
    ):
        template = (tmp_path / name).read_text()
        assert message in template


def test_app_synthesizes_stack_when_account_is_resolved(tmp_path):
    result = run_app(tmp_path, CDK_DEFAULT_ACCOUNT=ACCOUNT)

    assert result.returncode == 0, result.stderr
    assert stack_names(tmp_path) == {
        "OerfDataStack",
        "OerfComputeStack",
        "OerfObservabilityStack",
    }
    template = (tmp_path / "OerfDataStack.template.json").read_text()
    assert ACCOUNT in template
    assert "Refusing to deploy with the synth placeholder account" not in template


def test_context_account_overrides_cdk_default_account(tmp_path):
    """``-c account=000000000000`` wins over an ambient CDK_DEFAULT_ACCOUNT."""

    result = run_app(
        tmp_path,
        CDK_DEFAULT_ACCOUNT=ACCOUNT,
        CDK_CONTEXT_JSON=json.dumps({"account": "000000000000"}),
    )

    assert result.returncode == 0, result.stderr
    template = (tmp_path / "OerfDataStack.template.json").read_text()
    assert "000000000000" in template
    assert ACCOUNT not in template
    assert "Refusing to deploy with the synth placeholder account 000000000000" in template


def test_real_context_account_is_not_the_placeholder(tmp_path):
    result = run_app(
        tmp_path,
        CDK_DEFAULT_ACCOUNT="000000000000",
        CDK_CONTEXT_JSON=json.dumps({"account": ACCOUNT}),
    )

    assert result.returncode == 0, result.stderr
    template = (tmp_path / "OerfDataStack.template.json").read_text()
    assert ACCOUNT in template
    assert "Refusing to deploy with the synth placeholder account" not in template


def test_waba_app_gives_each_lambda_one_errors_alarm(tmp_path):
    result = run_app(
        tmp_path,
        CDK_DEFAULT_ACCOUNT=ACCOUNT,
        CDK_CONTEXT_JSON=json.dumps({"whatsapp:wabaId": "waba-0000000000000000"}),
    )

    assert result.returncode == 0, result.stderr
    templates = {
        name: json.loads((tmp_path / f"{name}.template.json").read_text())
        for name in stack_names(tmp_path)
    }
    lambdas = {
        logical_id: resource
        for template in templates.values()
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == "AWS::Lambda::Function"
    }
    # CDK's legacy OIDC provider is a CustomResourceProvider, not a
    # lambda.Function, so the alarm walker does not see it.
    provider = [
        logical_id
        for logical_id in lambdas
        if logical_id.startswith("CustomAWSCDKOpenIdConnectProviderCustomResourceProviderHandler")
    ]
    assert provider == [
        "CustomAWSCDKOpenIdConnectProviderCustomResourceProviderHandlerF2C543E0"
    ]
    errors = [
        resource["Properties"]
        for resource in templates["OerfObservabilityStack"]["Resources"].values()
        if resource["Type"] == "AWS::CloudWatch::Alarm"
        and resource["Properties"].get("MetricName") == "Errors"
    ]
    assert len(errors) == len(lambdas) - 1
    dimensions = [_function_name(props) for props in errors]
    assert len(dimensions) == len(set(dimensions))
    framework = [
        logical_id
        for logical_id in templates["OerfObservabilityStack"]["Resources"]
        if "frameworkonEventErrors" in logical_id
    ]
    # Tenant-attribute, device-seed, and WABA providers each get one alarm.
    assert len(framework) == 3


def test_dashboard_flag_synthesizes_the_api_stack(tmp_path):
    result = run_app(
        tmp_path,
        CDK_DEFAULT_ACCOUNT=ACCOUNT,
        CDK_CONTEXT_JSON=json.dumps({"dashboard:enabled": "true"}),
    )

    assert result.returncode == 0, result.stderr
    assert stack_names(tmp_path) == {
        "OerfDataStack",
        "OerfComputeStack",
        "OerfApiStack",
        "OerfObservabilityStack",
    }
    templates = {
        name: json.loads((tmp_path / f"{name}.template.json").read_text())
        for name in stack_names(tmp_path)
    }
    sockets = [
        (name, logical)
        for name, template in templates.items()
        for logical, resource in template["Resources"].items()
        if resource["Type"] == "AWS::ApiGatewayV2::Api"
        and resource["Properties"].get("ProtocolType") == "WEBSOCKET"
    ]
    assert len(sockets) == 1
    assert sockets[0][0] == "OerfComputeStack"
    api = templates["OerfApiStack"]
    assert all(resource["Type"] != "AWS::SQS::Queue" for resource in api["Resources"].values())
    assert _dashboard_send_target(api, templates["OerfComputeStack"]) == "InboundQueue"
    origins = _allow_origins(api)
    assert origins
    assert all("*" not in str(origin) and "DomainName" in str(origin) for origin in origins)


def _function_name(alarm: dict) -> str:
    names = [
        dimension["Value"]
        for dimension in alarm.get("Dimensions", [])
        if dimension.get("Name") == "FunctionName"
    ]
    assert len(names) == 1
    return json.dumps(names[0], sort_keys=True)


def _dashboard_send_target(api: dict, compute: dict) -> str:
    """Logical queue the dashboard action role may send to."""
    action_role = None
    for resource in api["Resources"].values():
        if resource["Type"] != "AWS::Lambda::Function":
            continue
        variables = resource["Properties"].get("Environment", {}).get("Variables", {})
        if "INBOUND_QUEUE_URL" not in variables:
            continue
        action_role = resource["Properties"]["Role"]["Fn::GetAtt"][0]
    assert action_role
    actions: list[list[str]] = []
    export_name = None
    for resource in api["Resources"].values():
        if resource["Type"] != "AWS::IAM::Policy":
            continue
        roles = resource["Properties"].get("Roles", [])
        attached = any(
            isinstance(role, dict) and role.get("Ref") == action_role for role in roles
        )
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]:
            raw = statement.get("Action", [])
            names = [raw] if isinstance(raw, str) else list(raw)
            if any(name.startswith("sqs:") for name in names):
                assert attached, "sqs permission is not on the dashboard action role"
                actions.append(names)
                export_name = statement["Resource"]["Fn::ImportValue"]
    assert actions == [["sqs:SendMessage"]]
    assert isinstance(export_name, str)
    for output in compute["Outputs"].values():
        if output.get("Export", {}).get("Name") != export_name:
            continue
        target = output["Value"].get("Fn::GetAtt")
        assert target and target[1] == "Arn"
        logical = target[0]
        assert logical.startswith("InboundQueue")
        assert not logical.startswith("InboundDlq")
        return "InboundQueue"
    raise AssertionError(f"no compute export {export_name}")


def _allow_origins(template: dict) -> list:
    found: list = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    key == "method.response.header.Access-Control-Allow-Origin"
                    and not isinstance(value, bool)
                ):
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(template)
    return found

