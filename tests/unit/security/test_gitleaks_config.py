"""The gitleaks config is pinned so an allowlist edit is a reviewed change."""

import hashlib
from pathlib import Path

# sha256 of .gitleaks.toml. Update this in the same change as the config.
_CHECKSUM = "7c56b7f577547ea8ddc4003f48d912ae5aaf4f63a2af94c44d2ffc235d2796d0"


def test_gitleaks_config_checksum_matches() -> None:
    raw = Path(".gitleaks.toml").read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == _CHECKSUM
    text = raw.decode("utf-8")
    assert "tests[/\\\\]" in text
    assert ".jsii-cache" in text
