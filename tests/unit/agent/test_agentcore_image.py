"""The AgentCore image ships the runtime's whole import closure.

The unit suite runs from the repository, so a module that resolves locally can
still be missing from the image: the recipe copies the runtime's imports
explicitly, with the repository root as the build context. This suite walks the
closure from ``oerf.platform.agent.agentcore.app`` and fails when a module the
runtime reaches is not copied, so a new import cannot silently break the image.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_IMAGE = _ROOT / "oerf" / "platform" / "agent" / "agentcore"
_DOCKERFILE = _IMAGE / "Dockerfile"
_ENTRY_MODULE = "oerf.platform.agent.agentcore.app"
_ENTRY = _ROOT / "oerf" / "platform" / "agent" / "agentcore" / "app.py"
#: The build context is the repository root, so Docker reads the ignore file
#: there and never one beside a Dockerfile that lives deeper in the tree.
_IGNORE = _ROOT / ".dockerignore"

_COPY = re.compile(r"^COPY\s+(\S+)\s+(\S+)\s*$", re.MULTILINE)
_CMD = re.compile(r"^CMD\s+\[(.*)\]\s*$", re.MULTILINE)


def _directives() -> list[tuple[str, str]]:
    """Every ``COPY`` source and destination in the image recipe."""

    return _COPY.findall(_DOCKERFILE.read_text(encoding="utf-8"))


def _copied() -> set[Path]:
    """The repository files the image receives, relative to the root."""

    copied: set[Path] = set()
    for source, _ in _directives():
        path = _ROOT / source.rstrip("/")
        if path.is_dir():
            copied.update(
                item.relative_to(_ROOT)
                for item in path.rglob("*")
                if item.is_file() and "__pycache__" not in item.parts
            )
        elif path.is_file():
            copied.add(path.relative_to(_ROOT))
    return copied


def _module_file(name: str) -> Path | None:
    """The file a dotted module name resolves to, or ``None``."""

    base = _ROOT.joinpath(*name.split("."))
    module = base.with_suffix(".py")
    if module.is_file():
        return module
    package = base / "__init__.py"
    return package if package.is_file() else None


def _imports(module: Path) -> set[str]:
    """The ``oerf`` names one file imports, with relative imports resolved."""

    tree = ast.parse(module.read_text(encoding="utf-8"))
    package = module.relative_to(_ROOT).with_suffix("").parts[:-1]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names if alias.name.startswith("oerf."))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                names.add(".".join((*base, node.module)) if node.module else ".".join(base))
            elif node.module and node.module.startswith("oerf."):
                names.add(node.module)
                names.update(
                    f"{node.module}.{alias.name}" for alias in node.names if alias.name != "*"
                )
    return names


def _closure(entry: Path) -> set[Path]:
    """Every repository module reachable from ``entry`` through ``oerf`` imports."""

    seen: set[Path] = set()
    pending = [entry]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        seen.update(_packages(current))
        for name in _imports(current):
            found = _module_file(name)
            if found is not None and found not in seen:
                pending.append(found)
    return seen


def _packages(module: Path) -> set[Path]:
    """The ``__init__.py`` of every package above ``module``."""

    found: set[Path] = set()
    for parent in module.parents:
        if parent == _ROOT:
            break
        init = parent / "__init__.py"
        if init.is_file():
            found.add(init)
    return found


def test_the_runtime_entry_point_is_the_image_command() -> None:
    command = _CMD.search(_DOCKERFILE.read_text(encoding="utf-8"))

    assert command is not None, "the image has no CMD"
    assert json.loads(f"[{command.group(1)}]") == ["python", "-m", _ENTRY_MODULE]
    assert _ENTRY.is_file()


def test_every_module_the_runtime_reaches_is_copied() -> None:
    missing = sorted(
        path.relative_to(_ROOT).as_posix()
        for path in _closure(_ENTRY)
        if path.relative_to(_ROOT) not in _copied()
    )

    assert missing == [], f"the image does not copy {missing}"


def test_the_packaged_configuration_the_resolver_reads_is_copied() -> None:
    copied = _copied()

    assert [path for path in copied if path.parts[0] == "domains"]
    assert [path for path in copied if path.parts[:2] == ("sim", "seed")]


def test_the_build_context_ignore_file_keeps_bytecode_out() -> None:
    lines = [line.strip() for line in _IGNORE.read_text(encoding="utf-8").splitlines()]

    assert "__pycache__" in lines
    assert "*.pyc" in lines
