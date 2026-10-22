"""Lambda zips and the AgentCore image context ship runtime files only."""

import os
from pathlib import Path

import aws_cdk as cdk

from infra.constructs import agentcore_runtime
from infra.stacks.api_stack import OerfApiStack
from infra.stacks.compute_stack import OerfComputeStack
from infra.stacks.data_stack import OerfDataStack

_FORBIDDEN = {
    "docs",
    "tests",
    "web",
    "scripts",
    "infra",
    "task_exports",
    ".github",
    ".cursor",
    ".venv",
    "cdk.out",
    "node_modules",
}
_ACCOUNT = "123456789012"
_REGION = "us-east-1"


def test_runtime_asset_contains_oerf_and_omits_docs_and_tests(tmp_path: Path) -> None:
    app = cdk.App(outdir=str(tmp_path))
    env = cdk.Environment(account=_ACCOUNT, region=_REGION)
    data = OerfDataStack(app, "OerfDataStack", env=env)
    compute = OerfComputeStack(app, "OerfComputeStack", data_stack=data, env=env)
    OerfApiStack(
        app,
        "OerfApiStack",
        data_stack=data,
        inbound_queue=compute.inbound_queue,
        websocket_url="https://example.execute-api.us-east-1.amazonaws.com/prod",
        env=env,
    )
    app.synth()

    assets = [
        path
        for path in tmp_path.iterdir()
        if path.is_dir() and path.name.startswith("asset.")
    ]
    runtime = [path for path in assets if (path / "oerf").is_dir()]
    assert runtime, "synthesized assets did not include an oerf/ directory"
    for asset in runtime:
        names = {child.name for child in asset.iterdir()}
        assert "oerf" in names
        assert "domains" in names
        missing = _FORBIDDEN & names
        assert not missing, f"{asset.name} still contains {sorted(missing)}"
        assert not (asset / "docs").exists()
        assert not (asset / "tests").exists()
        assert not any(asset.rglob("__pycache__"))
        assert not any(asset.rglob("*.pyc"))
        leaked = _dot_or_env_files(asset)
        assert not leaked, f"{asset.name} still contains {leaked}"


def _dot_or_env_files(asset: Path) -> list[str]:
    """Return staged paths whose name starts with '.' or ends in '.env'."""

    leaked: list[str] = []
    for dirpath, dirnames, filenames in os.walk(asset):
        for name in (*dirnames, *filenames):
            if name.startswith(".") or name.endswith(".env"):
                leaked.append(str(Path(dirpath, name).relative_to(asset)))
    return leaked


def test_agentcore_image_context_is_only_the_handler() -> None:
    context = agentcore_runtime._IMAGE
    names = {path.name for path in context.iterdir() if path.name != "__pycache__"}
    assert "app.py" in names
    assert not (names & _FORBIDDEN)
    assert names <= {"app.py", "Dockerfile", ".dockerignore"}
    dockerfile = (context / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY app.py ." in dockerfile
    assert "COPY .." not in dockerfile
    ignore = (context / ".dockerignore").read_text(encoding="utf-8").split()
    assert "*" in ignore
    assert "!app.py" in ignore
