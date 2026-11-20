"""Run credential-free offline driver-reply evaluations against the OERF corpus.

The historical exact ``map_reply`` baseline is reported separately from the
deterministic offline fixture evaluation; neither is a model or Bedrock score.
Run from the repository root with ``python -m scripts.eval_driver_replies``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from oerf.platform.cadence.interpret import interpret_workflow, map_reply
from oerf.platform.cadence.offline_classifier import OfflineFakeClassifier

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "tests" / "ai-evals" / "driver_replies.jsonl"
SYNTHETIC_CATALOG = frozenset({"E17", "E05", "E22", "E09"})


def score(
    dataset: Path = DEFAULT_DATASET,
    *,
    modes: dict[str, str] | None = None,
    catalog: set[str] | frozenset[str] | None = SYNTHETIC_CATALOG,
) -> dict[str, Any]:
    """Report the legacy baseline and offline route evaluation separately."""
    rows = [
        json.loads(line)
        for line in dataset.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    fake = OfflineFakeClassifier.for_corpus(rows, modes=modes)
    baseline_rows: list[dict[str, Any]] = []
    offline_rows: list[dict[str, Any]] = []
    for row in rows:
        mapped = map_reply(row["text"])
        old_label = mapped[0] if mapped is not None else "clarify"
        expected = row["expected_intent"]
        baseline_rows.append({"id": row["id"], "category": row["category"],
                              "expected_intent": expected, "predicted_label": old_label,
                              "correct": old_label.replace("_", "-") == expected})
        workflow = {"tenant_id": "offline-synthetic-tenant", "incident_id": row["id"],
                    "policy_id": "critical_field", "reply": {"text": row["text"]}}
        response = interpret_workflow(
            workflow,
            enabled=True,
            classify=fake,
            confidence_floor=0.8,
            knows_fault_code=(
                (lambda _tenant, code: code.upper() in catalog) if catalog is not None else None
            ),
        )["response"]
        label, intent = response.get("category", "clarify"), response.get("intent", "clarify")
        if intent == "call_me":
            route, outcome = "HandleCallback", "callback requested; not consumed"
        elif intent == "clarify" or not response.get("valid"):
            route, outcome = "ResetLadder", "not consumed"
        else:
            route, outcome = "AcknowledgeReply → Answered", "consumed"
        offline_rows.append({"id": row["id"], "category": row["category"],
                             "expected_intent": expected, "predicted_label": label,
                             "classification_correct": label == expected,
                             "proposed_fields": fake.proposed_fields(row["text"]),
                             "validated_asl_intent": intent, "valid": response.get("valid", False),
                             "expected_route": _expected_route(expected), "asl_route": route,
                             "route_correct": route == _expected_route(expected),
                             "expected_outcome": _expected_outcome(expected), "outcome": outcome,
                             "outcome_correct": outcome == _expected_outcome(expected)})
    baseline = _block(
        "deterministic map_reply baseline (not a model evaluation)", baseline_rows, "correct"
    )
    offline = _block(
        "deterministic offline fixture classifier (not a model evaluation)",
        offline_rows,
        "classification_correct",
    )
    offline.update(
        {
            "catalog": (
                "synthetic fixture (not authoritative tenant data)"
                if catalog is not None
                else "missing/unreadable (fail-closed)"
            ),
            "by_asl_route": _breakdown(offline_rows, "asl_route"),
            "by_outcome": _breakdown(offline_rows, "outcome"),
        }
    )
    return {
        "dataset": str(dataset.relative_to(ROOT) if dataset.is_relative_to(ROOT) else dataset),
        "count": len(rows),
        "baseline": baseline,
        "offline_evaluation": offline,
        # Keep the original score shape available to existing consumers.
        "evaluator": baseline["evaluator"],
        "correct": baseline["correct"],
        "accuracy": baseline["accuracy"],
        "by_category": baseline["by_category"],
        "results": baseline["results"],
    }


def _block(name: str, rows: list[dict[str, Any]], correct_key: str) -> dict[str, Any]:
    count = len(rows)
    correct = sum(bool(row[correct_key]) for row in rows)
    return {
        "evaluator": name,
        "count": count,
        "correct": correct,
        "accuracy": correct / count if count else 0.0,
        "by_category": _breakdown(rows, "category", correct_key),
        "results": rows,
    }


def _breakdown(
    rows: list[dict[str, Any]], key: str, correct_key: str | None = None
) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = result.setdefault(row[key], {"count": 0, "correct": 0})
        bucket["count"] += 1
        if correct_key is not None:
            bucket["correct"] += int(bool(row[correct_key]))
    return result


def _expected_route(expected: str) -> str:
    if expected == "call-me":
        return "HandleCallback"
    # A driver ``decline`` proposal is not a diagnosis. It clarifies.
    if expected == "answer":
        return "AcknowledgeReply → Answered"
    return "ResetLadder"


def _expected_outcome(expected: str) -> str:
    if expected == "call-me":
        return "callback requested; not consumed"
    return "consumed" if expected == "answer" else "not consumed"


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
    report = score(args.dataset, modes=None)
    baseline, offline = report["baseline"], report["offline_evaluation"]
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(
            f"Historical baseline: {baseline['correct']}/{baseline['count']} "
            f"({baseline['accuracy']:.1%})"
        )
        print(
            f"Offline fake (not model score): {offline['correct']}/{offline['count']} "
            f"({offline['accuracy']:.1%})"
        )
        for title, groups in (
            ("Corpus categories", offline["by_category"]),
            ("ASL routes", offline["by_asl_route"]),
            ("Final outcomes", offline["by_outcome"]),
        ):
            print(f"{title}:")
            for label, values in sorted(groups.items()):
                result = (
                    f"{values['correct']}/{values['count']}"
                    if title == "Corpus categories"
                    else str(values["count"])
                )
                print(f"  {label}: {result}")
    if args.fail_under is not None:
        if not 0 <= args.fail_under <= 1:
            parser.error("--fail-under must be between 0 and 1")
        return int(baseline["accuracy"] < args.fail_under)
    return 0


if __name__ == "__main__":
    sys.exit(main())
