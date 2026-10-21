"""placeholder, owner: project lead (OERF-201-205)

SQS inbound entry point. Returns no batch failures. Logs the record count only.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event: dict[str, Any], context: Any) -> dict[str, list]:
    """Acknowledge the batch. The project lead replaces this body."""
    records = event.get("Records") if isinstance(event, dict) else None
    count = len(records) if isinstance(records, list) else 0
    logger.info("record_count=%s", count)
    return {"batchItemFailures": []}
