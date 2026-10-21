"""Placeholder inbound handler: empty failures, count-only log."""

import logging

from oerf.platform.inbound.handler import handler


def test_inbound_returns_no_failures_and_logs_only_the_count(caplog) -> None:
    event = {
        "Records": [
            {
                "messageId": "msg-do-not-log",
                "body": "tenant t1 shipment PH-2841 token secret-body",
            }
        ]
    }

    with caplog.at_level(logging.INFO, logger="oerf.platform.inbound.handler"):
        result = handler(event, None)

    assert result == {"batchItemFailures": []}
    assert "record_count=1" in caplog.text
    assert "msg-do-not-log" not in caplog.text
    assert "PH-2841" not in caplog.text
    assert "secret-body" not in caplog.text
    assert "t1" not in caplog.text
