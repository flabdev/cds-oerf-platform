"""Placeholder ingest handler: no state-machine call, count-only log."""

import logging

from oerf.platform.ingest.handler import handler


def test_ingest_returns_none_and_logs_only_the_count(caplog) -> None:
    event = {
        "source": "sim.reefer",
        "detail-type": "TemperatureReading",
        "detail": {
            "tenant_id": "t1",
            "device_id": "RF-7781",
            "shipment_id": "PH-2841",
        },
    }

    with caplog.at_level(logging.INFO, logger="oerf.platform.ingest.handler"):
        result = handler(event, None)

    assert result is None
    assert "record_count=1" in caplog.text
    assert "t1" not in caplog.text
    assert "RF-7781" not in caplog.text
    assert "PH-2841" not in caplog.text
