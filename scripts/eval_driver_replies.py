"""Run the credential-free deterministic reply baseline against the OERF corpus.

This evaluates ``interpret.map_reply`` only; it is not an AgentCore/Bedrock
model benchmark. Run from the repository root with ``python -m scripts.eval_driver_replies``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from oerf.platform.cadence.interpret import map_reply

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "tests" / "ai-evals" / "driver_replies.jsonl"


def score(dataset: Path = DEFAULT_DATASET) -> dict[str, Any]:
    """Return exact normalized-intent accuracy and per-row outcomes."""

    rows = [
        json.loads(line)
        for line in dataset.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    results = []
    for row in rows:
        prediction = map_reply(row["text"])
        predicted_intent = prediction[0] if prediction is not None else "clarify"
        expected = row["expected_intent"].replace("-", "_")
        results.append(
            {
                "id": row["id"],
                "category": row["category"],
                "expected_intent": expected,
                "predicted_intent": predicted_intent,
                "correct": predicted_intent.replace("-", "_") == expected,
            }
        )
    correct = sum(result["correct"] for result in results)
    count = len(results)
    return {
        "dataset": str(dataset.relative_to(ROOT) if dataset.is_relative_to(ROOT) else dataset),
        "evaluator": "deterministic map_reply baseline (not a model evaluation)",
        "count": count,
        "correct": correct,
        "accuracy": correct / count if count else 0.0,
        "by_category": _category_summary(results),
        "results": results,
    }


def _category_summary(results: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for row in results:
        category = row["category"]
        current = summary.setdefault(category, {"correct": 0, "count": 0})
        current["count"] += 1
        current["correct"] += int(row["correct"])
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    parser.add_argument(
        "--fail-under",
        type=float,
        default=None,
        help="optional minimum accuracy as a fraction (for example 0.95)",
    )
    args = parser.parse_args(argv)
    report = score(args.dataset)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(
            f"{report['evaluator']}: {report['correct']}/{report['count']} "
            f"({report['accuracy']:.1%})"
        )
        for category, values in sorted(report["by_category"].items()):
            print(f"  {category}: {values['correct']}/{values['count']}")
    if args.fail_under is not None:
        if not 0 <= args.fail_under <= 1:
            parser.error("--fail-under must be between 0 and 1")
        return 1 if report["accuracy"] < args.fail_under else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
