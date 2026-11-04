"""Credential-free tests for the opt-in Bedrock photo evaluation runner."""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest

from scripts.eval_photo_evidence import evaluate


def _png(width: int, height: int, rgb: tuple[int, int, int], *, placeholder: bool = False) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        checksum = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", checksum)

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    payload = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
    if placeholder:
        payload += chunk(b"tEXt", b"fixture_kind\x00generated-placeholder")
    payload += chunk(b"IDAT", zlib.compress(raw))
    return payload + chunk(b"IEND", b"")


class FakeConverse:
    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"output": {"message": {"content": [{"text": json.dumps(self.answer)}]}}}


_ANSWER = {
    "setpoint_temp_c": 5.0,
    "ambient_temp_c": 8.1,
    "fault_codes": ["E17"],
    "alarm_status": "ACTIVE",
    "door_alarm_status": "CLOSED",
    "power_status": "RUNNING",
    "confidence_score": 0.9,
    "display_readout_summary": "synthetic test display",
}


def _dataset(tmp_path: Path) -> Path:
    (tmp_path / "photo.png").write_bytes(_png(2, 2, (10, 20, 30)))
    labels = [
        {
            "id": "PH-TEST",
            "file": "photo.png",
            "expected_code": "E17",
            "readable": True,
            "set_point_c": 5.0,
            "reading_c": 8.1,
        }
    ]
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(labels), encoding="utf-8")
    return path


def test_evaluation_scores_labeled_fields_and_sends_image_bytes(tmp_path: Path) -> None:
    labels = _dataset(tmp_path)
    client = FakeConverse(_ANSWER)

    report = evaluate(client=client, labels_path=labels)

    assert report["count"] == 1
    assert report["correct"] == 1
    assert report["accuracy"] == 1.0
    assert report["quality_evidence"] is True
    assert report["field_scores"]["fault_codes"] == {"correct": 1, "count": 1}
    assert client.calls[0]["messages"][0]["content"][0]["image"]["source"]["bytes"]


def test_malformed_model_output_is_counted_not_exposed(tmp_path: Path) -> None:
    labels = _dataset(tmp_path)

    class Malformed:
        def converse(self, **kwargs: Any) -> dict[str, Any]:
            return {"output": {"message": {"content": [{"text": "not-json"}]}}}

    report = evaluate(client=Malformed(), labels_path=labels)

    assert report["correct"] == 0
    assert report["results"][0]["error"] == "invalid_model_output"
    assert "not-json" not in json.dumps(report)


def test_generated_placeholder_fixture_is_blocked_or_reported_not_validated(
    tmp_path: Path,
) -> None:
    labels = _dataset(tmp_path)
    image = tmp_path / "photo.png"
    image.write_bytes(_png(2, 2, (10, 20, 30), placeholder=True))
    client = FakeConverse(_ANSWER)

    with pytest.raises(ValueError, match="not valid model-quality evidence"):
        evaluate(client=client, labels_path=labels)
    report = evaluate(client=client, labels_path=labels, allow_generated_fixtures=True)

    assert report["quality_evidence"] is False
    assert report["generated_placeholder_fixtures"] == ["photo.png"]


def test_empty_labels_are_rejected(tmp_path: Path) -> None:
    labels = tmp_path / "labels.json"
    labels.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="non-empty"):
        evaluate(client=FakeConverse(_ANSWER), labels_path=labels)
