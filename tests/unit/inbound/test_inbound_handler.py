"""Inbound SQS record handling: retry semantics and count-only logging.

OERF-205 replaces the placeholder that acknowledged every record. A record that
cannot be processed is returned as a partial-batch failure so SQS retries it,
and the batch is reported by count only: record bodies, identifiers and tokens
must never reach the logs.
"""

import json
from datetime import UTC, datetime
from typing import Any

from oerf.platform.channels.engagement import MemoryEngagementStore
from oerf.platform.channels.registry import MemoryPhoneRegistry
from oerf.platform.inbound.dedupe import InboundDedupe
from oerf.platform.inbound.handler import (
    InboundDependencies,
    configure_dependencies,
    handler,
)

NOW = datetime(2026, 10, 20, 10, 20, tzinfo=UTC)


class Callbacks:
    def send_task_success(self, *, taskToken: str, output: str) -> None:
        """No engagement is waiting on a token in these tests."""


def _dependencies() -> InboundDependencies:
    return InboundDependencies(
        dedupe=InboundDedupe(clock=lambda: NOW),
        engagements=MemoryEngagementStore(clock=lambda: NOW),
        callbacks=Callbacks(),
        registry=MemoryPhoneRegistry(),
    )


def _run(event: dict[str, Any], dependencies: InboundDependencies):
    configure_dependencies(dependencies)
    try:
        return handler(event, None)
    finally:
        configure_dependencies(None)


def test_unparseable_record_is_retried_and_logged_by_count_only(capsys) -> None:
    event = {
        "Records": [
            {
                "messageId": "msg-do-not-log",
                "body": "tenant t1 shipment PH-2841 token secret-body",
            }
        ]
    }

    result = _run(event, _dependencies())
    text = capsys.readouterr().out

    assert result == {"batchItemFailures": [{"itemIdentifier": "msg-do-not-log"}]}
    assert "Processed inbound records=1 failures=1" in text
    assert "msg-do-not-log" not in text
    assert "PH-2841" not in text
    assert "secret-body" not in text
    assert "t1" not in text


def test_a_malformed_record_entry_is_counted_without_crashing(capsys) -> None:
    result = _run({"Records": ["not-an-object"]}, _dependencies())

    assert result == {"batchItemFailures": []}
    assert "Processed inbound records=1 failures=0" in capsys.readouterr().out


def test_a_non_list_records_field_is_acknowledged_without_failures(capsys) -> None:
    result = _run({"Records": "unexpected"}, _dependencies())

    assert result == {"batchItemFailures": []}
    assert "Processed inbound records=0 failures=0" in capsys.readouterr().out


def test_a_recognized_record_is_not_returned_for_retry(capsys) -> None:
    event = {
        "Records": [
            {
                "messageId": "sqs-quiet",
                "body": json.dumps(
                    {
                        "provider": "whatsapp",
                        "message_id": "wamid-quiet",
                        "from": "+15550000001",
                        "timestamp": "2026-10-20T10:20:00Z",
                        "text": "acknowledged",
                    }
                ),
            }
        ]
    }

    result = _run(event, _dependencies())
    text = capsys.readouterr().out

    assert result == {"batchItemFailures": []}
    assert "Processed inbound records=1 failures=0" in text
    assert "acknowledged" not in text
