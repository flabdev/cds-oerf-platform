"""The gitleaks config is pinned so an allowlist edit is a reviewed change."""

import hashlib
from pathlib import Path

# sha256 of .gitleaks.toml with newlines normalized to LF.
_CHECKSUM = "ee559cfee8542ee7f633b50926977b9cba1ba78a7e9099d58282acf228850ef0"


def test_gitleaks_config_checksum_matches() -> None:
    raw = Path(".gitleaks.toml").read_bytes().replace(b"\r\n", b"\n")
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == _CHECKSUM
    text = raw.decode("utf-8")
    assert "test_synth_secrets" in text
    assert "pr-32\\.diff" in text
    assert "sim[/\\\\]seed" not in text
    assert "docs[/\\\\]" not in text
    assert ".jsii-cache" in text
