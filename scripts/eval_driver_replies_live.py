"""Score the live Bedrock classifier against the driver-reply corpus.

This is a model score. It is not the credential-free offline fake from
``scripts.eval_driver_replies``. The default unit suite never runs it.

From the repository root, after credentials and Bedrock access are available:

    python -m scripts.eval_driver_replies_live --confirm-bedrock-call

With ``--backend jev`` it scores the Jev decision model instead. That sends
each corpus reply (synthetic text) to TypeSafe, so it needs its own flag and a
key in ``JEV_API_KEY``:

    JEV_API_KEY=... python -m scripts.eval_driver_replies_live --backend jev \
        --confirm-jev-call

One run invokes the model once per corpus row (42). The report prints the
model id, prompt version, predicted category, validity, and token or latency
counts. It does not print reply text, prompts, or raw model output.

Validity uses the production catalog gate: a proposed fault code stays
unverified until that tenant has a ``FAULTCODE#`` row, so code-bearing answers
can be category-correct and still clarify.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from oerf.platform.cadence.interpret import validate_proposal

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "tests" / "ai-evals" / "driver_replies.jsonl"
ProposalFn = Callable[[str], tuple[dict[str, Any] | None, dict[str, int]]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    parser.add_argument(
        "--backend",
        choices=("agentcore", "jev"),
        default="agentcore",
        help="the classifier to score (default agentcore: Bedrock Nova via Strands)",
    )
    parser.add_argument(
        "--confirm-bedrock-call",
        action="store_true",
        help="required for agentcore. Without it the command makes no model call.",
    )
    parser.add_argument(
        "--confirm-jev-call",
        action="store_true",
        help="required for jev. Without it the command sends nothing to TypeSafe.",
    )
    parser.add_argument(
        "--fail-under",
        type=float,
        default=None,
        help="exit 1 when category accuracy is below this fraction",
    )
    args = parser.parse_args(argv)
    confirm = "--confirm-jev-call" if args.backend == "jev" else "--confirm-bedrock-call"
    if not (args.confirm_jev_call if args.backend == "jev" else args.confirm_bedrock_call):
        print(f"refusing: pass {confirm} to score the live model", file=sys.stderr)
        return 2
    if args.fail_under is not None and not 0 <= args.fail_under <= 1:
        parser.error("--fail-under must be between 0 and 1")
    rows = _load(args.dataset)
    if args.backend == "jev":
        classify = _jev_classifier()
        if classify is None:
            print("set JEV_API_KEY to score Jev", file=sys.stderr)
            return 2
        model_id, prompt_version = _jev_model_id(), _jev_prompt_version()
    else:
        classify = _live_classifier()
        if classify is None:
            print("classifier agent is unavailable", file=sys.stderr)
            return 2
        model_id, prompt_version = _model_id(), _prompt_version()
    report = score_rows(rows, classify, model_id=model_id, prompt_version=prompt_version)
    _print_report(report, as_json=args.json)
    if args.fail_under is not None and report["accuracy"] < args.fail_under:
        return 1
    return 0


def score_rows(
    rows: list[dict[str, Any]],
    classify: ProposalFn,
    *,
    model_id: str,
    prompt_version: str,
) -> dict[str, Any]:
    """Score proposals. ``classify`` receives reply text; the report does not keep it."""

    results: list[dict[str, Any]] = []
    for row in rows:
        try:
            proposal, meta = classify(row["text"])
        except Exception:  # noqa: BLE001 - a model failure is an unscored row
            proposal, meta = None, {"latency_ms": 0, "input_tokens": 0, "output_tokens": 0}
        predicted = _category(proposal)
        confidence = _confidence(proposal)
        validated = validate_proposal(
            proposal if isinstance(proposal, dict) else {},
            tenant_id="live-score",
            confidence_floor=0.8,
            knows_fault_code=lambda _tenant, _code: False,
        )
        route = _route(validated)
        results.append(
            {
                "id": row["id"],
                "expected_intent": row["expected_intent"],
                "lang": row.get("lang", ""),
                "predicted_label": predicted,
                "category_correct": predicted == row["expected_intent"],
                "confidence": confidence,
                "confidence_band": _band(confidence),
                "valid": validated["valid"] is True,
                "asl_route": route,
                "route_correct": route == _expected_route(row["expected_intent"]),
                "latency_ms": int(meta.get("latency_ms", 0)),
                "input_tokens": int(meta.get("input_tokens", 0)),
                "output_tokens": int(meta.get("output_tokens", 0)),
            }
        )
    return {
        "evaluator": "live model",
        "model_id": model_id,
        "prompt_version": prompt_version,
        "invocations": len(rows),
        "count": len(results),
        "correct": sum(row["category_correct"] for row in results),
        "accuracy": _accuracy(results, "category_correct"),
        "by_category": _breakdown(results, "expected_intent", "category_correct"),
        "by_language": _breakdown(results, "lang", "category_correct"),
        "by_asl_route": _breakdown(results, "asl_route", "route_correct"),
        # Calibration: how often each confidence band was right. A calibrated
        # model's ">=0.9" band should be right at least 90% of the time.
        "by_confidence": _breakdown(results, "confidence_band", "category_correct"),
        "results": results,
    }


def _confidence(proposal: dict[str, Any] | None) -> float | None:
    value = proposal.get("confidence") if isinstance(proposal, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _band(confidence: float | None) -> str:
    if confidence is None:
        return "none"
    if confidence >= 0.9:
        return ">=0.9"
    if confidence >= 0.8:
        return "0.8-0.9"
    return "<0.8"


def _load(dataset: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in dataset.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _category(proposal: dict[str, Any] | None) -> str:
    if not isinstance(proposal, dict):
        return "none"
    category = proposal.get("category")
    return category if isinstance(category, str) else "none"


def _route(response: dict[str, Any]) -> str:
    if response.get("intent") == "call_me":
        return "HandleCallback"
    if response.get("valid") is True:
        return "AcknowledgeReply → Answered"
    return "ResetLadder"


def _expected_route(expected: str) -> str:
    if expected == "call-me":
        return "HandleCallback"
    # A driver ``decline`` is not a diagnosis on critical_field (v1.58). It clarifies.
    if expected == "answer":
        return "AcknowledgeReply → Answered"
    return "ResetLadder"


def _accuracy(rows: list[dict[str, Any]], key: str) -> float:
    if not rows:
        return 0.0
    return sum(bool(row[key]) for row in rows) / len(rows)


def _breakdown(
    rows: list[dict[str, Any]], group: str, correct_key: str
) -> dict[str, dict[str, float | int]]:
    buckets: dict[str, dict[str, float | int]] = {}
    for row in rows:
        bucket = buckets.setdefault(str(row[group]), {"count": 0, "correct": 0})
        bucket["count"] = int(bucket["count"]) + 1
        bucket["correct"] = int(bucket["correct"]) + int(bool(row[correct_key]))
    for bucket in buckets.values():
        count = int(bucket["count"])
        bucket["accuracy"] = (int(bucket["correct"]) / count) if count else 0.0
    return buckets


def _print_report(report: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    print(
        "Live model score (not the offline fake): "
        f"{report['correct']}/{report['count']} ({report['accuracy']:.1%})"
    )
    print(f"model_id={report['model_id']} prompt_version={report['prompt_version']}")
    print(f"invocations={report['invocations']}")
    for title, groups in (
        ("Corpus categories", report["by_category"]),
        ("Languages", report["by_language"]),
        ("ASL routes", report["by_asl_route"]),
        ("Confidence bands (category accuracy)", report["by_confidence"]),
    ):
        print(f"{title}:")
        for label, values in sorted(groups.items()):
            print(
                f"  {label}: {values['correct']}/{values['count']} ({values['accuracy']:.1%})"
            )


def _model_id() -> str:
    from oerf.platform.agent.agentcore.classifier import model_id

    return model_id()


def _prompt_version() -> str:
    from oerf.platform.agent.agentcore.classifier import PROMPT_VERSION

    return PROMPT_VERSION


def _live_classifier() -> ProposalFn | None:
    from oerf.platform.agent.agentcore import classifier

    agent = classifier.build_agent()
    if agent is None:
        return None

    class _KeepProposedCodes:
        def knows_fault_code(self, code: str) -> bool:
            return isinstance(code, str) and bool(code.strip())

    def classify(text: str) -> tuple[dict[str, Any] | None, dict[str, int]]:
        captured: dict[str, Any] = {}
        real = classifier.record_invocation

        def wrapped(metadata: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            result = real(metadata, **kwargs)
            captured.clear()
            captured.update(result)
            return result

        classifier.record_invocation = wrapped
        started = time.monotonic()
        try:
            proposal = classifier.classify_reply(text, policy=_KeepProposedCodes(), agent=agent)
        finally:
            classifier.record_invocation = real
        elapsed = max(int((time.monotonic() - started) * 1000), 0)
        return proposal, {
            "latency_ms": int(captured.get("latency_ms", elapsed)),
            "input_tokens": int(captured.get("input_tokens", 0)),
            "output_tokens": int(captured.get("output_tokens", 0)),
        }

    return classify


def _jev_model_id() -> str:
    import os

    from oerf.platform.agent.jev import DEFAULT_MODEL

    return os.environ.get("JEV_MODEL", "").strip() or DEFAULT_MODEL


def _jev_prompt_version() -> str:
    from oerf.platform.cadence.jev_classifier import PROMPT_VERSION

    return PROMPT_VERSION


def _jev_classifier() -> ProposalFn | None:
    """Jev with the key from ``JEV_API_KEY``, or ``None`` when it is unset.

    Like the Bedrock scorer, every candidate fault code is proposed, and the
    production catalog gate in ``score_rows`` decides validity. Both backends
    are then scored against the same gate.
    """

    import os

    from oerf.platform.cadence.jev_classifier import JevReplyClassifier

    key = os.environ.get("JEV_API_KEY", "").strip()
    if not key:
        return None
    captured: dict[str, Any] = {}

    def trace(metadata: dict[str, object]) -> None:
        captured.clear()
        captured.update(metadata)

    classifier = JevReplyClassifier(
        api_key=lambda: key,
        knows_fault_code=lambda _tenant, _code: True,
        model=_jev_model_id(),
        trace=trace,
    )

    def classify(text: str) -> tuple[dict[str, Any] | None, dict[str, int]]:
        captured.clear()
        proposal = classifier("live-score", "live-score", text, "critical_field")
        return proposal, {
            "latency_ms": int(captured.get("latency_ms", 0)),
            "input_tokens": int(captured.get("input_tokens", 0)),
            "output_tokens": int(captured.get("output_tokens", 0)),
        }

    return classify


if __name__ == "__main__":
    sys.exit(main())
