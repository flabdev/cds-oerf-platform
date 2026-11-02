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
