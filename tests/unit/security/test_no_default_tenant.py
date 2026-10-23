"""Handlers must not invent a tenant when the caller omits one."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3] / "oerf"

# Assignment forms that write a stand-in tenant. Example payloads in docstrings
# ("tenant_id": "t1") are not assignments.
_PATTERNS = (
    re.compile(r"""\.get\(\s*['"]tenant_id['"]\s*,\s*['"][^'"]+['"]\s*\)"""),
    re.compile(r"""\bor\s+['"]T1['"]"""),
    re.compile(r"""\bor\s+['"]default['"]"""),
    re.compile(r"""\btenant_id\s*=\s*['"](?:T1|default)['"]"""),
)


def test_oerf_modules_do_not_assign_a_literal_default_tenant() -> None:
    hits: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if any(pattern.search(line) for pattern in _PATTERNS):
                hits.append(f"{path.relative_to(ROOT.parent)}:{number}: {line.strip()}")
    assert hits == [], (
        "A handler assigned a literal tenant. Read tenant from the token or the "
        "platform event and fail closed when it is missing:\n" + "\n".join(hits)
    )
