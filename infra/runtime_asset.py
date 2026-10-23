"""Lambda zip contents for handlers that import the ``oerf`` package.

The asset root stays the repository root so ``import oerf`` does not shadow the
stdlib ``platform`` module. ``IgnoreMode.GIT`` makes the leading ``*`` an
allow-list: only ``oerf``, ``domains``, and ``sim`` are copied. ``sim`` stays
because ``FileConfigRepository`` reads that package at runtime through
``importlib.resources``. Every other root is dropped, including ``docs``,
``tests``, ``web``, ``scripts``, ``infra``, ``task_exports``, ``.github``,
``.cursor``, ``.venv``, ``cdk.out``, and ``node_modules``. Inside the three
packages, a basename that starts with ``.`` is dropped, as are ``*.env``,
``.demo-numbers*``, ``__pycache__``, and ``*.pyc``.

Synth then installs the third-party closure into that same directory. The
Lambda Python 3.11 runtime supplies the standard library and boto3, and does
not supply pydantic or jsonschema. Wheels are downloaded for
``manylinux2014_x86_64`` and CPython 3.11, which is the default Lambda
architecture: no function in the compute, data, or API stacks sets
``Architecture``. One shared zip cannot carry a second architecture.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import jsii
from aws_cdk import BundlingOptions, IgnoreMode, ILocalBundling, aws_lambda as lambda_

_ROOT = Path(__file__).resolve().parents[1]
_PACKAGES = ("oerf", "domains", "sim")

#: Default Lambda architecture. ``Architecture`` is unset on every function.
LAMBDA_PLATFORM = "manylinux2014_x86_64"
#: Matches ``lambda_.Runtime.PYTHON_3_11`` on every function.
LAMBDA_PYTHON = "3.11"

#: Pins that satisfy ``pyproject.toml`` (``pydantic>=2``, ``jsonschema>=4``)
#: plus the import closure those two pull in at these versions. ``attr`` is
#: shipped by the ``attrs`` distribution.
VENDORED_REQUIREMENTS = (
    "pydantic==2.14.1",
    "pydantic-core==2.50.1",
    "annotated-types==0.8.0",
    "typing-extensions==4.16.0",
    "typing-inspection==0.4.4",
    "jsonschema==4.26.0",
    "attrs==26.1.0",
    "jsonschema-specifications==2025.9.1",
    "referencing==0.37.0",
    "rpds-py==2026.9.1",
)

RUNTIME_ASSET_EXCLUDE = [
    "*",
    *[pattern for name in _PACKAGES for pattern in (f"!{name}", f"!{name}/**")],
    "**/.*",
    "**/.env",
    "**/*.env",
    "**/.env.*",
    "**/.demo-numbers*",
    "**/__pycache__",
    "**/*.pyc",
    "**/*.pyo",
]


def runtime_code_args() -> dict[str, object]:
    """Keyword arguments for ``Code.from_asset`` on the shared runtime zip."""

    return {
        "exclude": RUNTIME_ASSET_EXCLUDE,
        "ignore_mode": IgnoreMode.GIT,
        "bundling": BundlingOptions(
            image=lambda_.Runtime.PYTHON_3_11.bundling_image,
            command=[
                "python",
                "-c",
                "raise SystemExit('runtime dependencies are installed without Docker')",
            ],
            local=_BundleRuntime(),
        ),
    }


def stage_runtime_asset(output_dir: Path) -> None:
    """Copy the allow-listed sources and the vendored wheels into ``output_dir``."""

    output_dir.mkdir(parents=True, exist_ok=True)
    for name in _PACKAGES:
        shutil.copytree(
            _ROOT / name,
            output_dir / name,
            ignore=_ignore_source,
            dirs_exist_ok=True,
        )
    for child in _vendored_tree().iterdir():
        if child.name.startswith("."):
            continue
        target = output_dir / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target)


@jsii.implements(ILocalBundling)
class _BundleRuntime:
    """Install Linux wheels at synth time. Docker is not the bundler."""

    def try_bundle(self, output_dir: str, *, image: object, **_kwargs: object) -> bool:
        _ = image
        stage_runtime_asset(Path(output_dir))
        return True


def _ignore_source(_directory: str, names: list[str]) -> set[str]:
    """Drop dotfiles, env files, and bytecode. Matches ``RUNTIME_ASSET_EXCLUDE``."""

    ignored: set[str] = set()
    for name in names:
        if (
            name.startswith(".")
            or name == "__pycache__"
            or name.endswith((".pyc", ".pyo", ".env"))
            or ".demo-numbers" in name
        ):
            ignored.add(name)
    return ignored


def _cache_key() -> str:
    material = "\n".join((LAMBDA_PLATFORM, LAMBDA_PYTHON, *VENDORED_REQUIREMENTS))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _vendored_tree() -> Path:
    """Download the pinned wheels once per machine, then reuse the directory."""

    base = Path(tempfile.gettempdir()) / "oerf-lambda-vendor"
    cache = base / _cache_key()
    ready = (cache / "pydantic" / "__init__.py").is_file()
    if ready and (cache / "pydantic_core").is_dir():
        return cache
    partial = base / f"{_cache_key()}.partial"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True)
    _pip_install(partial)
    _prune_vendor(partial)
    if cache.exists():
        shutil.rmtree(cache)
    partial.rename(cache)
    return cache


def _pip_install(target: Path) -> None:
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-compile",
        "--only-binary=:all:",
        "--platform",
        LAMBDA_PLATFORM,
        "--implementation",
        "cp",
        "--python-version",
        LAMBDA_PYTHON,
        "--target",
        str(target),
        *VENDORED_REQUIREMENTS,
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"failed to vendor Lambda dependencies: {detail}")


def _prune_vendor(root: Path) -> None:
    """Drop installer metadata and bytecode. Keep importable packages."""

    removed: list[Path] = []
    for path in root.rglob("*"):
        name = path.name
        if path.is_dir() and (
            name == "__pycache__" or name.endswith(".dist-info") or name.endswith(".egg-info")
        ):
            removed.append(path)
        elif path.is_file() and name.endswith((".pyc", ".pyo")):
            removed.append(path)
    for path in removed:
        if not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    scripts = root / "bin"
    if scripts.exists():
        shutil.rmtree(scripts)
