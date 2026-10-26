"""Lambda handlers do not call logging.getLogger themselves.

The check is an AST scan of each handler's source, plus an import so the
module still loads. ``get_logger`` calls ``logging.getLogger`` inside
``oerf.platform.observability.logging``, so a runtime probe of who called
``getLogger`` cannot tell a handler from that helper. The shared logger
module is the one place the call belongs, and it is not in this list.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

_HANDLERS = (
    "oerf.platform.ingest.handler",
    "oerf.platform.inbound.handler",
    "oerf.platform.api.action_handler",
    "oerf.platform.api.ws_handler",
    "oerf.platform.api.ws_authorizer",
    "oerf.platform.api.stream_broadcaster",
    "oerf.platform.agent.runtime",
    "oerf.platform.agent.agentcore.app",
    "oerf.platform.agent.agentcore.advisory",
    "oerf.platform.evidence.policy",
    "oerf.platform.channels.router",
    "oerf.platform.evidence.vision",
    "oerf.platform.report.generator",
)


def _direct_get_logger_calls(tree: ast.AST) -> list[int]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "logging":
            for alias in node.names:
                if alias.name == "getLogger":
                    imported.add(alias.asname or alias.name)
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "getLogger"
            and isinstance(func.value, ast.Name)
            and func.value.id == "logging"
        ):
            hits.append(node.lineno)
        elif isinstance(func, ast.Name) and func.id in imported:
            hits.append(node.lineno)
    return hits


@pytest.mark.parametrize("module_name", _HANDLERS)
@pytest.mark.security
def test_handler_does_not_call_logging_getlogger(module_name: str) -> None:
    module = importlib.import_module(module_name)
    source = Path(module.__file__).read_text(encoding="utf-8")
    hits = _direct_get_logger_calls(ast.parse(source))
    assert hits == [], f"{module_name} calls logging.getLogger at lines {hits}"
