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


def _verify_ref(ref: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _github_pr_base_sha() -> str | None:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        return None
    try:
        event = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    sha = event.get("pull_request", {}).get("base", {}).get("sha")
    return sha if isinstance(sha, str) and sha else None


def _fetch_develop() -> None:
    subprocess.run(
        [
            "git",
            "fetch",
            "--no-tags",
            "origin",
            "develop:refs/remotes/origin/develop",
            "--depth=1",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )


def _develop_ref() -> str:
    """The deployed tree. ``origin/develop`` is the comparison the guard uses."""

    override = os.environ.get("OERF_EXPORT_BASE_REF", "").strip()
    if override and _verify_ref(override):
        return override

    for ref in ("origin/develop", "develop"):
        if _verify_ref(ref):
            return ref

    _fetch_develop()
    for ref in ("origin/develop", "develop"):
        if _verify_ref(ref):
            return ref

    base_sha = _github_pr_base_sha()
    if base_sha:
        subprocess.run(
            ["git", "fetch", "--no-tags", "origin", base_sha, "--depth=1"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
        )
        if _verify_ref(base_sha):
            return base_sha

    raise AssertionError(
        "origin/develop is not available for the export comparison "
        "(fetch develop or set OERF_EXPORT_BASE_REF)"
    )


def _synth_ref(outdir: Path, context: dict[str, str], ref: str) -> dict[str, dict]:
    """Synthesize ``ref`` into ``outdir`` without checking out the working tree."""

    source = outdir / "source"
    source.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "archive", ref],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    assert archive.returncode == 0, archive.stderr.decode()
    extracted = subprocess.run(
        ["tar", "-x", "-C", str(source)],
        input=archive.stdout,
        capture_output=True,
        check=False,
    )
    assert extracted.returncode == 0, extracted.stderr.decode()
    assembly = outdir / "assembly"
    assembly.mkdir()
    env = {key: value for key, value in os.environ.items() if key != "CDK_DEFAULT_ACCOUNT"}
    env.update(
        {
            "CDK_OUTDIR": str(assembly),
            "CDK_DEFAULT_REGION": REGION,
            "CDK_DEFAULT_ACCOUNT": ACCOUNT,
            "CDK_CONTEXT_JSON": json.dumps(context),
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "infra.app"],
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return _templates(assembly)


def test_removed_exports_are_not_imported_by_a_deployed_stack(tmp_path: Path) -> None:
    """An export dropped in this change must not still be imported.

    Compare the default main and dev synths with ``origin/develop``. Compute
    deploys before observability, so deleting an export that a deployed stack
    still imports fails the update (A0). The action-handler name export is
    the case this guards: it stays until F8.
    """

    ref = _develop_ref()
    for stage in ("main", "dev"):
        context = {"stage": stage, "account": ACCOUNT}
        current_dir = tmp_path / "current" / stage
        current_dir.mkdir(parents=True)
        current_run = _run(current_dir, context)
        assert current_run.returncode == 0, current_run.stderr
        current = _templates(current_dir)
        deployed = _synth_ref(tmp_path / "develop" / stage, context, ref)
        removed = _export_names(deployed) - _export_names(current)
        deployed_imports = _imports(deployed)
        current_imports = _imports(current)
        still_deployed = sorted(removed & deployed_imports)
        still_here = sorted(removed & current_imports)
        assert still_deployed == [], (
            "export removed while a deployed stack still imports it: "
            + ", ".join(still_deployed)
        )
        assert still_here == [], (
            "export removed while another stack in this change imports it: "
            + ", ".join(still_here)
        )
