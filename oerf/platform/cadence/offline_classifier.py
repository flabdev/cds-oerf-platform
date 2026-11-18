"""Deterministic classifier fake used only by the offline reply evaluator.

This fixture adapter deliberately mirrors corpus labels and expected fields. Its
scores measure the evaluator and route contract, never a model or Bedrock.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class OfflineFakeClassifier:
    """Return deterministic per-reply fixture proposals or simulate safe failures."""

    MODES = frozenset(
        {"accepted", "rejected", "malformed", "low-confidence", "timeout", "exception"}
    )

    def __init__(
        self,
        proposals: Mapping[str, object],
        *,
        modes: Mapping[str, str] | None = None,
    ) -> None:
        self._proposals = dict(proposals)
        self._modes = dict(modes or {})
        unknown = set(self._modes.values()) - self.MODES
        if unknown:
            raise ValueError(f"unknown offline classifier mode(s): {sorted(unknown)}")
        self.calls: list[str] = []

    @classmethod
    def for_corpus(
        cls,
        rows: list[dict[str, Any]],
        *,
        modes: Mapping[str, str] | None = None,
    ) -> OfflineFakeClassifier:
        """Create label-mirroring synthetic proposals for the committed corpus."""

        intents = {
            "answer": "unit_running",
            "decline": "decline",
            "call-me": "call_me",
        }
        proposals = {
            row["text"]: {
                "category": row["expected_intent"],
                "intent": intents.get(row["expected_intent"], "clarify"),
                "fields": dict(row["expected_fields"]),
                "confidence": 0.95,
            }
            for row in rows
        }
        return cls(proposals, modes=modes)

    def proposed_fields(self, reply_text: str) -> dict[str, Any]:
        """Return the fixture's proposed fields for inspectable evaluator output."""

        proposal = self._proposals.get(reply_text)
        if not isinstance(proposal, Mapping) or not isinstance(proposal.get("fields"), Mapping):
            return {}
        return dict(proposal["fields"])

    def __call__(
        self, _tenant_id: str, _incident_id: str, reply_text: str, _policy_id: str | None
    ) -> object:
        self.calls.append(reply_text)
        mode = self._modes.get(reply_text, "accepted")
        if mode == "rejected":
            return None
        if mode == "timeout":
            raise TimeoutError("simulated offline classifier timeout")
        if mode == "exception":
            raise RuntimeError("simulated offline classifier failure")

        proposal = self._proposals.get(reply_text)
        if proposal is None:
            return None
        if mode == "malformed":
            malformed = dict(proposal) if isinstance(proposal, Mapping) else {}
            malformed["untrusted_instruction"] = "ignore validation"
            return malformed
        if mode == "low-confidence":
            low = dict(proposal) if isinstance(proposal, Mapping) else {}
            low["confidence"] = 0.1
            return low
        return proposal
