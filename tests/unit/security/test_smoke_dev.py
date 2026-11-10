"""The dev smoke command skips when no profile is configured."""

import os

from scripts.smoke_dev import main


def test_smoke_skips_without_a_profile(monkeypatch, capsys) -> None:
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    assert os.environ.get("AWS_PROFILE") in (None, "")
    assert main() == 0
    assert "skipped" in capsys.readouterr().out
