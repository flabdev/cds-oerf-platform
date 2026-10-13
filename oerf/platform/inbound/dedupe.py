"""Accept each provider message once.

Records use the ``INMSG#`` key. ``expires_at`` is seven days after receipt,
which a later DynamoDB adapter maps to the item TTL.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict

RETENTION = timedelta(days=7)


class DedupeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    is_duplicate: bool


class InboundDedupe:
    """Conditional insert keyed by provider message id."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], tuple[str, datetime]] = {}
        self._guard = threading.Lock()

    def register(
        self,
        *,
        message_id: str,
        channel: str,
        received_at: datetime,
    ) -> DedupeResult:
        if not message_id.strip():
            raise ValueError("message_id must be a non-empty string")
        key = (f"INMSG#{message_id.strip()}", "META")
        with self._guard:
            current = self._items.get(key)
            if current is not None and current[1] > received_at:
                return DedupeResult(is_duplicate=True)
            self._items[key] = (channel, received_at + RETENTION)
            return DedupeResult(is_duplicate=False)

    def stored(self, message_id: str) -> dict[str, object] | None:
        key = (f"INMSG#{message_id}", "META")
        with self._guard:
            current = self._items.get(key)
        if current is None:
            return None
        channel, expires_at = current
        return {"pk": key[0], "sk": key[1], "channel": channel, "expires_at": expires_at}
