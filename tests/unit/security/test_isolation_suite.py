"""Named tenant-isolation suite (OERF-307 / E5)."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.security
def test_security_marker_is_registered() -> None:
    ini = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "security: security regression tests, no AWS" in ini


@pytest.mark.security
def test_threat_model_and_findings_exist() -> None:
    threat = (ROOT / "docs" / "security" / "threat-model.md").read_text(encoding="utf-8")
    findings = (ROOT / "docs" / "security" / "findings.md").read_text(encoding="utf-8")
    assert "custom:tenant_id" in threat
    assert "pytest -q -m security" in threat
    assert "SEC-01" in findings
    assert "SEC-12" in findings
