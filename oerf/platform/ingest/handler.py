"""placeholder, owner: project lead (OERF-201-205)

EventBridge ingest entry point. Does not start a state machine. Logs the
record count only.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event: dict[str, Any], context: Any) -> None:
    """Accept one reading. The project lead replaces this body."""
    count = 1 if isinstance(event, dict) and event else 0
    logger.info("record_count=%s", count)
    return None
