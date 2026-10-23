"""The AgentCore image ships the runtime's whole import closure.

The unit suite runs from the repository, so a module that resolves locally can
still be missing from the image. The guard copies only what the root
``.dockerignore`` allow-list admits, applies the Dockerfile's empty agent
package init when the recipe creates one, and imports
``oerf.platform.agent.agentcore.app`` from that directory alone.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_IMAGE = _ROOT / "oerf" / "platform" / "agent" / "agentcore"
_DOCKERFILE = _IMAGE / "Dockerfile"
_ENTRY_MODULE = "oerf.platform.agent.agentcore.app"
_ENTRY = _ROOT / "oerf" / "platform" / "agent" / "agentcore" / "app.py"
#: The build context is the repository root, so Docker reads the ignore file
#: there and never one beside a Dockerfile that lives deeper in the tree.
_IGNORE = _ROOT / ".dockerignore"
_AGENT_INIT = "/app/oerf/platform/agent/__init__.py"

_COPY = re.compile(r"^COPY\s+(\S+)\s+(\S+)\s*$", re.MULTILINE)
_CMD = re.compile(r"^CMD\s+\[(.*)\]\s*$", re.MULTILINE)


@dataclass(frozen=True)
class _Rule:
    """One dockerignore pattern, compiled the way Moby matches it."""

    regexp: re.Pattern[str]
    dirs: tuple[str, ...]
    negative: bool


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


def _clean(pattern: str) -> str:
    """Drop a trailing slash and collapse repeated separators."""

    cleaned = pattern.replace("\\", "/")
    while "//" in cleaned:
        cleaned = cleaned.replace("//", "/")
    if len(cleaned) > 1:
        cleaned = cleaned.rstrip("/")
    return cleaned


def _compile(pattern: str) -> re.Pattern[str]:
    """Turn one dockerignore pattern into an anchored expression."""

    pieces = ["^"]
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 1
                if index + 1 < len(pattern) and pattern[index + 1] == "/":
                    index += 1
                pieces.append(".*" if index + 1 >= len(pattern) else "(?:.*/)?")
            else:
                pieces.append("[^/]*")
        elif char == "?":
            pieces.append("[^/]")
        elif char == "\\" and index + 1 < len(pattern):
            index += 1
            pieces.append(re.escape(pattern[index]))
        else:
            pieces.append(re.escape(char))
        index += 1
    pieces.append("$")
    return re.compile("".join(pieces))


def _rules(text: str) -> list[_Rule]:
    """The ignore file as ordered match rules. A leading ``!`` re-includes."""

    parsed: list[_Rule] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negative = line.startswith("!")
        body = _clean(line[1:].strip() if negative else line)
        if not body:
            continue
        parsed.append(_Rule(_compile(body), tuple(body.split("/")), negative))
    return parsed


def _ignored(relative: str, rules: list[_Rule]) -> bool:
    """True when the last matching dockerignore rule excludes ``relative``."""

    parent = relative.rsplit("/", 1)[0] if "/" in relative else ""
    parent_dirs = parent.split("/") if parent else []
    matched = False
    for rule in rules:
        hit = rule.regexp.match(relative) is not None
        if not hit and parent:
            if "**" in rule.dirs:
                minimum = len([part for part in rule.dirs if part != "**"])
                for length in range(minimum, len(parent_dirs) + 1):
                    candidate = "/".join(parent_dirs[:length])
                    if rule.regexp.match(candidate):
                        hit = True
                        break
            elif len(rule.dirs) <= len(parent_dirs):
                candidate = "/".join(parent_dirs[: len(rule.dirs)])
                hit = rule.regexp.match(candidate) is not None
        if hit:
            matched = not rule.negative
    return matched


def _stage_allow_list(destination: Path) -> None:
    """Copy admitted files, then the empty agent init when the recipe creates one."""

    rules = _rules(_IGNORE.read_text(encoding="utf-8"))
    inclusions = [rule for rule in rules if rule.negative]
    for dirpath, dirnames, filenames in os.walk(_ROOT):
        current = Path(dirpath)
        relative_dir = current.relative_to(_ROOT).as_posix()
        if relative_dir == ".":
            relative_dir = ""
        kept: list[str] = []
        for name in dirnames:
            relative = f"{relative_dir}/{name}" if relative_dir else name
            if _ignored(relative, rules) and not _could_reinclude(relative, inclusions):
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            relative = f"{relative_dir}/{name}" if relative_dir else name
            if _ignored(relative, rules):
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(current / name, target)
    if _writes_empty_agent_init(_DOCKERFILE.read_text(encoding="utf-8")):
        init = destination / "oerf" / "platform" / "agent" / "__init__.py"
        init.parent.mkdir(parents=True, exist_ok=True)
        init.write_text("", encoding="utf-8")


def _could_reinclude(relative: str, inclusions: list[_Rule]) -> bool:
    """True when a later exception can admit this directory or something inside it."""

    prefix = f"{relative}/"
    for rule in inclusions:
        pattern = "/".join(rule.dirs)
        if pattern == relative or pattern.startswith(prefix) or relative.startswith(f"{pattern}/"):
            return True
    return False


def _writes_empty_agent_init(dockerfile: str) -> bool:
    """True when the recipe creates ``/app/oerf/platform/agent/__init__.py`` itself."""

    return any(
        line.startswith("RUN") and _AGENT_INIT in line for line in dockerfile.splitlines()
    )


def test_the_runtime_entry_point_is_the_image_command() -> None:
    command = _CMD.search(_DOCKERFILE.read_text(encoding="utf-8"))

    assert command is not None, "the image has no CMD"
    assert json.loads(f"[{command.group(1)}]") == ["python", "-m", _ENTRY_MODULE]
    assert _ENTRY.is_file()


def test_every_module_the_runtime_reaches_is_copied(tmp_path: Path) -> None:
    staged = tmp_path / "context"
    _stage_allow_list(staged)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(staged)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", "import oerf.platform.agent.agentcore.app"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_the_packaged_configuration_the_resolver_reads_is_copied() -> None:
    copied = _copied()

    assert [path for path in copied if path.parts[0] == "domains"]
    assert [path for path in copied if path.parts[:2] == ("sim", "seed")]


@pytest.mark.security
def test_the_build_context_ignore_file_keeps_bytecode_out() -> None:
    lines = [line.strip() for line in _IGNORE.read_text(encoding="utf-8").splitlines()]

    assert "**/__pycache__" in lines
    assert "**/*.pyc" in lines
