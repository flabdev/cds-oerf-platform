"""Every execution start uses the name cancel rebuilds."""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3] / "oerf"
_INGEST = _ROOT / "platform" / "ingest" / "handler.py"
_CANCEL = _ROOT / "platform" / "inbound" / "handler.py"


def _calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "start_execution":
            found.append(node)
        elif isinstance(func, ast.Name) and func.id == "start_execution":
            found.append(node)
    return found


def _assigned_from_execution_name(tree: ast.AST, name: str) -> bool:
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        if value is None:
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "_execution_name"
        ):
            return True
    return False


def _enclosing_function(
    tree: ast.AST, target: ast.AST
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(child is target for child in ast.walk(node)):
            return node
    return None


def test_only_ingest_starts_an_execution_and_cancel_rebuilds_that_name() -> None:
    calls = [(path, call) for path in _ROOT.rglob("*.py") for call in _calls(path)]

    assert len(calls) == 1
    path, _call = calls[0]
    assert path == _INGEST

    tree = ast.parse(_INGEST.read_text(encoding="utf-8"))
    start_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == "start_execution")
            or (isinstance(node.func, ast.Name) and node.func.id == "start_execution")
        )
    ]
    assert len(start_calls) == 1
    name_arg = next(keyword.value for keyword in start_calls[0].keywords if keyword.arg == "name")
    assert isinstance(name_arg, ast.Name)
    assert name_arg.id == "name"
    starter = _enclosing_function(tree, start_calls[0])
    assert starter is not None
    assert starter.name == "start"
    assert any(arg.arg == "name" for arg in starter.args.kwonlyargs)

    starts = [
        keyword.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "start"
        for keyword in node.keywords
        if keyword.arg == "name"
    ]
    assert len(starts) == 1
    assert isinstance(starts[0], ast.Name)
    assert _assigned_from_execution_name(tree, starts[0].id)
    cancel = _CANCEL.read_text(encoding="utf-8")
    assert "from oerf.platform.ingest.handler import _execution_name" in cancel
