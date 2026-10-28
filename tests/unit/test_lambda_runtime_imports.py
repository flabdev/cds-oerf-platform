"""Each Python Lambda imports from the asset it is deployed with.

The Lambda runtime provides the standard library and boto3. The subprocess
starts with ``-S`` so an editable install cannot satisfy ``import oerf``,
sets ``PYTHONPATH`` to that asset alone, loads boto3, then drops every path
that is not the asset or the standard library. A Linux wheel cannot be
executed on macOS; that failure is accepted when the binary file is present.
A missing pure-Python package still fails the import.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import aws_cdk as cdk
import boto3

from infra.runtime_asset import LAMBDA_PLATFORM, VENDORED_REQUIREMENTS, WHEELS
from infra.stacks.api_stack import OerfApiStack
from infra.stacks.compute_stack import OerfComputeStack
from infra.stacks.data_stack import OerfDataStack
from infra.stacks.observability import OerfObservabilityStack, lambda_functions

_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_ROOT = Path(__file__).resolve().parents[2]
_PURE_PACKAGES = (
    "pydantic",
    "annotated_types",
    "typing_inspection",
    "jsonschema",
    "jsonschema_specifications",
    "referencing",
    "attrs",
    "attr",
)
_REQUIRED_HANDLERS = (
    "workflow_stub.handler",
    "pre_token.handler",
    "tenant_attribute.handler",
    "oerf.platform.channels.devices.seed_handler",
    "waba_destination.on_event",
)
_IMPORT = """
import sys
from pathlib import Path
repo, asset, module, vendor = sys.argv[1:]
sys.path.insert(0, vendor)
import boto3
import boto3.dynamodb.types
import botocore
import botocore.exceptions
base = str(Path(sys.base_prefix).resolve())
kept = []
for entry in sys.path:
    if not entry or entry == asset:
        continue
    resolved = str(Path(entry).resolve())
    in_stdlib = resolved.startswith(base) and "site-packages" not in resolved
    if in_stdlib and not resolved.startswith(repo):
        kept.append(entry)
sys.path = [asset, *kept]
import importlib
importlib.import_module(module)
"""


def test_every_pinned_package_has_a_hash() -> None:
    names = [item.split("==", 1)[0] for item in VENDORED_REQUIREMENTS]
    recorded = [name for name, _version, _filename, _digest in WHEELS]

    assert recorded == names
    for name, _version, filename, digest in WHEELS:
        assert filename
        assert len(digest) == 64
        assert digest == digest.lower()
        if name in {"pydantic-core", "rpds-py"}:
            assert "cp311" in filename
            assert "manylinux2014_x86_64" in filename
        else:
            assert filename.endswith("-py3-none-any.whl")


def test_vendored_pins_match_pyproject_ranges() -> None:
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    pins = dict(item.split("==") for item in VENDORED_REQUIREMENTS)

    assert "pydantic>=2" in text
    assert "jsonschema>=4" in text
    assert _version(pins["pydantic"]) >= (2,)
    assert _version(pins["jsonschema"]) >= (4,)
    assert LAMBDA_PLATFORM == "manylinux2014_x86_64"


def test_each_python_lambda_imports_from_its_asset(tmp_path: Path) -> None:
    app = cdk.App(
        outdir=str(tmp_path),
        context={
            "whatsapp:wabaId": "waba-0000000000000000",
            "demo:seedDevices": "true",
        },
    )
    env = cdk.Environment(account=_ACCOUNT, region=_REGION)
    data = OerfDataStack(app, "OerfDataStack", env=env)
    compute = OerfComputeStack(app, "OerfComputeStack", data_stack=data, env=env)
    api = OerfApiStack(
        app,
        "OerfApiStack",
        data_stack=data,
        inbound_queue=compute.inbound_queue,
        websocket_url="https://example.execute-api.us-east-1.amazonaws.com/prod",
        env=env,
    )
    OerfObservabilityStack(
        app,
        "OerfObservabilityStack",
        functions=lambda_functions(data, compute, api),
        dead_letter_queues=[compute.inbound_dlq, compute.ingest_dlq, compute.stream_dlq],
        state_machines=[data.cold_chain_state_machine, data.engage_party_state_machine],
        channel_router_log_group=compute.channel_router_fn.log_group,
        env=env,
    )
    app.synth()

    functions = _python_functions(tmp_path)
    assert functions, "synth produced no Python Lambda functions"
    handlers = {handler for handler, _asset in functions}
    missing = [name for name in _REQUIRED_HANDLERS if name not in handlers]
    assert not missing, missing
    shared = [
        asset
        for _handler, asset in functions
        if (asset / "oerf").is_dir() and (asset / "pydantic").is_dir()
    ]
    assert shared, "the shared runtime asset is missing vendored pydantic"
    for asset in dict.fromkeys(shared):
        _assert_vendored(asset)

    seen: set[tuple[str, Path]] = set()
    for handler, asset in functions:
        module = handler.rsplit(".", 1)[0]
        key = (module, asset)
        if key in seen:
            continue
        seen.add(key)
        _import_module(module, asset, tmp_path)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _python_functions(outdir: Path) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for template in outdir.rglob("*.template.json"):
        resources = json.loads(template.read_text(encoding="utf-8"))["Resources"]
        for resource in resources.values():
            if resource.get("Type") != "AWS::Lambda::Function":
                continue
            props = resource["Properties"]
            runtime = str(props.get("Runtime", ""))
            if not runtime.startswith("python"):
                continue
            architectures = props.get("Architectures")
            assert architectures in (None, ["x86_64"]), architectures
            handler = str(props["Handler"])
            key = str(props["Code"]["S3Key"])
            asset = outdir / f"asset.{key.removesuffix('.zip')}"
            assert asset.is_dir(), handler
            found.append((handler, asset))
    return found


def _assert_vendored(asset: Path) -> None:
    for name in _PURE_PACKAGES:
        assert (asset / name).is_dir(), name
    assert (asset / "typing_extensions.py").is_file()
    cores = list((asset / "pydantic_core").glob("_pydantic_core*.so"))
    assert cores, "pydantic_core binary is missing from the runtime asset"
    assert any("cpython-311" in path.name and "x86_64" in path.name for path in cores)
    assert list((asset / "rpds").glob("*.so")), "rpds binary is missing"
    assert not list(asset.rglob("*.dist-info"))
    assert not (asset / "bin").exists()
    assert not list(asset.rglob("__pycache__"))


def _import_module(module: str, asset: Path, work: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(asset)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    vendor = Path(boto3.__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-S", "-c", _IMPORT, str(_ROOT), str(asset), module, str(vendor)],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return
    binary = _platform_binary_error(result.stderr)
    cores = list((asset / "pydantic_core").glob("_pydantic_core*.so"))
    if binary and cores:
        return
    raise AssertionError(f"import {module} failed:\n{result.stderr}")


def _platform_binary_error(stderr: str) -> bool:
    """True when the import died loading a Linux extension on this machine."""

    lowered = stderr.lower()
    if any(
        token in lowered
        for token in ("mach-o", "incompatible architecture", "wrong elf", "invalid elf")
    ):
        return True
    last = lowered.strip().splitlines()[-1] if lowered.strip() else ""
    if last.startswith("modulenotfounderror"):
        missing_extension = "_pydantic_core" in last or "named 'rpds" in last
        return missing_extension
    return "_pydantic_core" in lowered or ("rpds" in last and "importerror" in last)
