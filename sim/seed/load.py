"""In-memory loading of deterministic tenant seed files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_seed(tenant_id: str, root: Path | None = None) -> dict[str, Any]:
    """Load one tenant's seed documents.

    The files are static JSON, so a second call returns the same values.
    """

    tenant_dir = _seed_root(root) / tenant_id
    if not tenant_dir.is_dir():
        raise FileNotFoundError(f"seed directory for tenant {tenant_id!r} does not exist")
    documents: dict[str, Any] = {}
    for path in sorted(tenant_dir.rglob("*.json")):
        key = path.relative_to(tenant_dir).as_posix().removesuffix(".json")
        documents[key] = json.loads(path.read_text(encoding="utf-8"))
    return documents


def _seed_root(root: Path | None) -> Path:
    if root is not None:
        return root
    return Path(__file__).resolve().parent
