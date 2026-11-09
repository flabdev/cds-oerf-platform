"""Cross-stack imports stay the same when optional flags change.

A flag-gated Lambda used to be exported because the observability stack
alarmed it. Turning the flag off then deleted that export while the previous
observability template still imported it, and CloudFormation rolled the
producer stack back. These synths assert no import depends on
``demo:seedDevices``, ``whatsapp:wabaId``, or ``agentcore:enabled``.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ACCOUNT = "123456789012"
REGION = "us-east-1"

# Names produced by the automatic cross-stack reference before this change.
# export_value keeps them for one release. TODO(export-removal): delete the
# assertions that require these names once that call is removed.
_SEED_EXPORTS = (
    "ExportsOutputRefDeviceSeedFunction16F912CEECA8C4BC",
    "ExportsOutputRefDeviceSeedProviderframeworkonEvent6410A7B4F8986842",
)
_WABA_EXPORTS = (
    "ExportsOutputRefWabaEventDestinationFunctionACC362BE6A5A9E61",
    "ExportsOutputRefWabaEventDestinationProviderframeworkonEventB1FBE9AC47C51747",
)


def _run(outdir: Path, context: dict[str, str]) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key != "CDK_DEFAULT_ACCOUNT"}
    env.update(
        {
            "CDK_OUTDIR": str(outdir),
            "CDK_DEFAULT_REGION": REGION,
            "CDK_DEFAULT_ACCOUNT": ACCOUNT,
            "CDK_CONTEXT_JSON": json.dumps(context),
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


def _walk_imports(node: object, found: set[str]) -> None:
    if isinstance(node, dict):
        if set(node) == {"Fn::ImportValue"} and isinstance(node["Fn::ImportValue"], str):
            found.add(node["Fn::ImportValue"])
            return
        for value in node.values():
            _walk_imports(value, found)
    elif isinstance(node, list):
        for item in node:
            _walk_imports(item, found)


def _templates(outdir: Path) -> dict[str, dict]:
    return {
        path.name: json.loads(path.read_text())
        for path in outdir.glob("*.template.json")
    }


def _imports(templates: dict[str, dict]) -> set[str]:
    found: set[str] = set()
    for template in templates.values():
        _walk_imports(template, found)
    return found


def _export_names(templates: dict[str, dict]) -> set[str]:
    names: set[str] = set()
    for template in templates.values():
        for output in template.get("Outputs", {}).values():
            name = output.get("Export", {}).get("Name")
            if isinstance(name, str):
                names.add(name)
    return names


def _contexts(stage: str) -> list[dict[str, str]]:
    """Every seed, WABA, and AgentCore combination. Dev cannot set a WABA id."""

    waba_values = ("", "waba-0000000000000000") if stage == "main" else ("",)
    contexts = []
    for seed in ("false", "true"):
        for agentcore in ("false", "true"):
            for waba in waba_values:
                context = {
                    "stage": stage,
                    "account": ACCOUNT,
                    "demo:seedDevices": seed,
                    "agentcore:enabled": agentcore,
                }
                if waba:
                    context["whatsapp:wabaId"] = waba
                contexts.append(context)
    return contexts


def test_imported_exports_do_not_depend_on_optional_flags(tmp_path: Path) -> None:
    for stage in ("main", "dev"):
        imported: set[str] | None = None
        producer = "OerfComputeStack" if stage == "main" else "OerfComputeStack-dev"
        for context in _contexts(stage):
            label = "-".join(
                f"{key}-{value}" for key, value in sorted(context.items()) if key != "account"
            )
            outdir = tmp_path / stage / label
            outdir.mkdir(parents=True)
            result = _run(outdir, context)
            assert result.returncode == 0, result.stderr
            templates = _templates(outdir)
            current = _imports(templates)
            if imported is None:
                imported = current
            else:
                assert current == imported
            exports = _export_names(templates)
            seed_names = {f"{producer}:{suffix}" for suffix in _SEED_EXPORTS}
            waba_names = {f"{producer}:{suffix}" for suffix in _WABA_EXPORTS}
            if context["demo:seedDevices"] == "true":
                assert seed_names <= exports
            else:
                assert seed_names.isdisjoint(exports)
            assert seed_names.isdisjoint(current)
            if stage == "main" and context.get("whatsapp:wabaId"):
                assert waba_names <= exports
            else:
                assert waba_names.isdisjoint(exports)
            assert waba_names.isdisjoint(current)
            for name in current:
                assert "DeviceSeed" not in name
                assert "WabaEventDestination" not in name
                assert "DashboardQueryFunction" not in name
                assert "DashboardActionFunction" not in name
                assert "BucketDeployment" not in name
