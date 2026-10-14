"""Accept each provider message once.

Records use the ``INMSG#`` key. ``expires_at`` is seven days after the record
is accepted, measured by the clock. A later DynamoDB adapter maps that value
to the item TTL. Provider retries often repeat the original timestamp, so
expiry is compared with the clock rather than ``received_at``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict

RETENTION = timedelta(days=7)


class DedupeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    is_duplicate: bool


class InboundDedupe:
    """Conditional insert keyed by provider message id."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._items: dict[tuple[str, str], tuple[str, datetime]] = {}
        self._guard = threading.Lock()
        self._clock = clock or (lambda: datetime.now(UTC))

    def register(
        self,
        *,
        message_id: str,
        channel: str,
        received_at: datetime,
    ) -> DedupeResult:
        """Record the message unless an unexpired copy is already stored.

        ``received_at`` is the provider timestamp. Retention is measured from
        the clock, because a retry usually repeats the original timestamp.
        """

        if not message_id.strip():
            raise ValueError("message_id must be a non-empty string")
        key = (f"INMSG#{message_id.strip()}", "META")
        with self._guard:
            now = self._clock()
            current = self._items.get(key)
            if current is not None and current[1] > now:
                return DedupeResult(is_duplicate=True)
            self._items[key] = (channel, now + RETENTION)
            return DedupeResult(is_duplicate=False)

    def stored(self, message_id: str) -> dict[str, object] | None:
        key = (f"INMSG#{message_id}", "META")
        with self._guard:
            current = self._items.get(key)
        if current is None:
            return None
        channel, expires_at = current
        return {"pk": key[0], "sk": key[1], "channel": channel, "expires_at": expires_at}
