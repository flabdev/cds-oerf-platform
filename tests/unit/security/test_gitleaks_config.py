"""The gitleaks config is pinned so an allowlist edit is a reviewed change."""

import hashlib
from pathlib import Path

# sha256 of .gitleaks.toml with newlines normalized to LF.
_CHECKSUM = "ebcf1f3fda38ca1912f8ec7a6dcce979fa42741ce309a5a9776532091b8b7e37"


def test_gitleaks_config_checksum_matches() -> None:
    raw = Path(".gitleaks.toml").read_bytes().replace(b"\r\n", b"\n")
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == _CHECKSUM
    text = raw.decode("utf-8")
    assert "tests[/\\\\]" in text
    assert ".jsii-cache" in text
