"""The driver-reply evaluation set covers every intent and category minimum.

The set is data, not code: this suite keeps it well formed, keeps ids unique,
and enforces the coverage targets of spec page 18
(``evals--coverage-targets-42-items-minimum``). A row whose
``expected_fields`` names a fault code is also exercised against the guardrail
validator, so an expected code has to be a two-character synthetic code the
normalizer can read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_SET = _ROOT / "tests" / "ai-evals" / "driver_replies.jsonl"

#: Category to the minimum row count from the spec table.
MINIMUMS = {
    "clean_code": 5,
    "misspelt_code": 6,
    "free_text": 6,
    "hinglish": 6,
    "concern": 4,
    "help": 3,
    "call_me": 3,
    "unrelated": 3,
    "decline": 2,
    "driving_safety": 4,
}
MIN_ITEMS = 42
INTENTS = frozenset({"answer", "decline", "help", "concern", "call-me", "unrelated"})
LANGS = frozenset({"en", "hi-Latn"})
_REQUIRED = ("id", "text", "lang", "expected_intent", "expected_fields", "category")


def rows() -> list[dict]:
    return [json.loads(line) for line in _SET.read_text(encoding="utf-8").splitlines() if line]


def test_the_set_meets_the_minimum_item_count() -> None:
    assert len(rows()) >= MIN_ITEMS


@pytest.mark.parametrize("category", sorted(MINIMUMS))
def test_every_category_meets_its_minimum(category: str) -> None:
    present = [row for row in rows() if row["category"] == category]

    assert len(present) >= MINIMUMS[category], category


def test_every_category_in_the_minimum_table_is_used() -> None:
    assert {row["category"] for row in rows()} == set(MINIMUMS)


def test_ids_are_unique_and_ordered() -> None:
    ids = [row["id"] for row in rows()]

    assert ids == [f"DR-{index:03d}" for index in range(1, len(ids) + 1)]
    assert len(set(ids)) == len(ids)


def test_every_row_has_the_documented_schema() -> None:
    for row in rows():
        for name in _REQUIRED:
            assert name in row, (row["id"], name)
        assert isinstance(row["text"], str) and row["text"].strip(), row["id"]
        assert row["lang"] in LANGS, (row["id"], row["lang"])
        assert row["expected_intent"] in INTENTS, (row["id"], row["expected_intent"])
        assert isinstance(row["expected_fields"], dict), row["id"]


def test_a_named_fault_code_is_a_synthetic_code() -> None:
    for row in rows():
        code = row["expected_fields"].get("fault_code")
        if code is None:
            continue
        assert len(code) == 3 and code[0].isalpha() and code[1:].isdigit(), row["id"]


def test_a_row_with_no_code_on_screen_claims_no_fault_code() -> None:
    for row in rows():
        if row["category"] == "free_text":
            assert "fault_code" not in row["expected_fields"], row["id"]


def test_hinglish_rows_are_tagged_hi_latn() -> None:
    for row in rows():
        if row["category"] == "hinglish":
            assert row["lang"] == "hi-Latn", row["id"]