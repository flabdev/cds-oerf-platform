"""Lambda zips and the AgentCore image context ship runtime files only."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import aws_cdk as cdk
import pytest

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

pytestmark = pytest.mark.security


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
    assert names == {"app.py", "advisory.py", "decision.py", "Dockerfile"}
    assert not (names & _FORBIDDEN)
    dockerfile = (context / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY oerf/platform/agent/agentcore/app.py" in dockerfile
    assert "COPY oerf/platform/agent/agentcore/advisory.py" in dockerfile
    assert "COPY oerf/platform/agent/agentcore/decision.py" in dockerfile
    assert "COPY .." not in dockerfile
    ignore = (Path(__file__).resolve().parents[2] / ".dockerignore").read_text(
        encoding="utf-8"
    ).split()
    assert ignore[0] == "*"
    assert "!oerf/platform/agent/agentcore/" in ignore
    assert "**/__pycache__" in ignore
    assert "**/*.pyc" in ignore


def _docker_available() -> bool:
    """True when the docker client can reach a daemon."""

    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _docker_image_assets(outdir: Path) -> list[Path]:
    """Staged directories for every docker-image asset in the assembly."""

    found: list[Path] = []
    for manifest_path in outdir.glob("*.assets.json"):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        images = manifest.get("dockerImages", {})
        if not isinstance(images, dict):
            continue
        for image in images.values():
            source = image.get("source") if isinstance(image, dict) else None
            directory = source.get("directory") if isinstance(source, dict) else None
            if isinstance(directory, str):
                found.append(outdir / directory)
    return found


_IMAGE_FORBIDDEN = {"docs", "tests", "web", "infra", ".venv", "node_modules"}


def test_staged_agentcore_image_keeps_the_allow_list(tmp_path: Path) -> None:
    if not _docker_available():
        pytest.skip("Docker is unavailable")

    app = cdk.App(outdir=str(tmp_path), context={"agentcore:enabled": "true"})
    env = cdk.Environment(account=_ACCOUNT, region=_REGION)
    data = OerfDataStack(app, "OerfDataStack", env=env)
    OerfComputeStack(app, "OerfComputeStack", data_stack=data, env=env)
    app.synth()

    assets = _docker_image_assets(tmp_path)
    assert len(assets) == 1
    asset = assets[0]
    assert (asset / "oerf/platform/agent/agentcore/app.py").is_file()
    assert (asset / "domains").is_dir()
    assert (asset / "sim").is_dir()
    leaked_roots = [
        path.relative_to(asset).as_posix()
        for path in asset.rglob("*")
        if set(path.relative_to(asset).parts) & _IMAGE_FORBIDDEN
    ]
    assert not leaked_roots, f"image asset still contains {leaked_roots}"
    leaked = [
        name
        for name in _dot_or_env_files(asset)
        if Path(name).name != ".dockerignore"
    ]
    assert not leaked, f"image asset still contains {leaked}"
