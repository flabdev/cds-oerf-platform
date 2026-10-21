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
    assert stack_names(tmp_path) == {"OerfDataStack", "OerfComputeStack"}
    message = "Refusing to deploy with the synth placeholder account 000000000000"
    for name in ("OerfDataStack.template.json", "OerfComputeStack.template.json"):
        template = (tmp_path / name).read_text()
        assert message in template


def test_app_synthesizes_stack_when_account_is_resolved(tmp_path):
    result = run_app(tmp_path, CDK_DEFAULT_ACCOUNT=ACCOUNT)

    assert result.returncode == 0, result.stderr
    assert stack_names(tmp_path) == {"OerfDataStack", "OerfComputeStack"}


def test_dashboard_flag_synthesizes_the_api_stack(tmp_path):
    result = run_app(
        tmp_path,
        CDK_DEFAULT_ACCOUNT=ACCOUNT,
        CDK_CONTEXT_JSON=json.dumps({"dashboard:enabled": "true"}),
    )

    assert result.returncode == 0, result.stderr
    assert stack_names(tmp_path) == {"OerfDataStack", "OerfComputeStack", "OerfApiStack"}

