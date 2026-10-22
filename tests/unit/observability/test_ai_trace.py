"""The model-call trace accepts sanitized fields and refuses the rest."""

import pytest

from oerf.platform.observability.ai_trace import TraceRejected, record_invocation


class _Segment:
    def __init__(self) -> None:
        self.annotations: list[tuple[str, object]] = []

    def put_annotation(self, key: str, value: object) -> None:
        self.annotations.append((key, value))


def _metadata() -> dict[str, object]:
    return {
        "model_id": "amazon.nova-lite-v1:0",
        "latency_ms": 40,
        "input_tokens": 12,
        "output_tokens": 3,
        "guardrail_action": "NONE",
        "outcome": "SUCCESS",
    }


def test_allowed_fields_become_annotations() -> None:
    segment = _Segment()

    written = record_invocation(_metadata(), segment=segment)

    assert written["model_id"] == "amazon.nova-lite-v1:0"
    assert written["latency_ms"] == 40
    assert segment.annotations == list(written.items())


@pytest.mark.parametrize(
    "field",
    [
        "prompt",
        "system_prompt",
        "image",
        "images",
        "raw_output",
        "model_output",
        "phone",
        "email",
        "name",
        "pii",
    ],
)
def test_refuses_prompts_images_raw_output_and_pii(field: str) -> None:
    segment = _Segment()
    metadata = _metadata()
    metadata[field] = "not-recorded"

    with pytest.raises(TraceRejected, match="refusing fields"):
        record_invocation(metadata, segment=segment)

    assert segment.annotations == []


def test_refuses_raw_text_in_an_allowed_field() -> None:
    metadata = _metadata()
    metadata["outcome"] = "Release the shipment. Call +910000000101."

    with pytest.raises(TraceRejected, match="short status token"):
        record_invocation(metadata, segment=_Segment())
