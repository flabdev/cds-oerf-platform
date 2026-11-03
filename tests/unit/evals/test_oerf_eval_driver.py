"""The evaluation runner reports a reproducible deterministic baseline."""

import json
from pathlib import Path

from scripts.eval_driver_replies import DEFAULT_DATASET, main, score


def test_runner_scores_all_committed_rows_and_is_explicit_about_baseline() -> None:
    report = score()

    assert report["count"] == 42
    assert report["evaluator"] == "deterministic map_reply baseline (not a model evaluation)"
    assert report["accuracy"] == report["correct"] / 42
    assert sum(value["count"] for value in report["by_category"].values()) == 42
    assert report["correct"] == 0


def test_runner_can_be_invoked_with_json_report(capsys) -> None:
    assert main(["--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["dataset"] == str(DEFAULT_DATASET.relative_to(Path.cwd()))
    assert printed["count"] == 42


def test_fail_under_is_optional_and_reports_measured_baseline() -> None:
    assert main(["--fail-under", "0"]) == 0
    assert main(["--fail-under", "0.95"]) == 1
