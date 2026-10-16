"""DynamoDB dedupe for provider message ids.

The client is injected. Expiry is seven days from the injected clock, compared
with that clock rather than the provider timestamp.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from oerf.platform.inbound.dedupe import RETENTION, DedupeResult
from oerf.platform.storage.dynamo import (
    dedupe_pk,
    epoch_seconds,
    from_epoch,
    is_conditional_failure,
    key,
    n,
    number,
    required_text,
    s,
    text,
)


class DynamoMessageDedupe:
    """Conditional put on the routing table."""

    def __init__(
        self,
        client: Any,
        table_name: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._table = table_name
        self._clock = clock or (lambda: datetime.now(UTC))

    def register(
        self,
        *,
        message_id: str,
        channel: str,
        received_at: datetime,
        tenant_id: str | None = None,
    ) -> DedupeResult:
        message = required_text(message_id, "message_id")
        now = self._clock()
        expires = epoch_seconds(now + RETENTION)
        item = {
            **key(dedupe_pk(message), "META"),
            "channel": s(required_text(channel, "channel")),
            "expires_at": n(expires),
        }
        if tenant_id is not None:
            item["tenant_id"] = s(required_text(tenant_id, "tenant_id"))
        try:
            self._client.put_item(
                TableName=self._table,
                Item=item,
                ConditionExpression="attribute_not_exists(#pk) OR #expires_at <= :now",
                ExpressionAttributeNames={"#pk": "pk", "#expires_at": "expires_at"},
                ExpressionAttributeValues={":now": n(epoch_seconds(now))},
            )
        except Exception as exc:
            if not is_conditional_failure(exc):
                raise
            return DedupeResult(is_duplicate=True)
        return DedupeResult(is_duplicate=False)

    def stored(self, message_id: str) -> dict[str, object] | None:
        message = required_text(message_id, "message_id")
        pk = dedupe_pk(message)
        loaded = self._client.get_item(TableName=self._table, Key=key(pk, "META"))
        item = loaded.get("Item")
        if not item:
            return None
        tenant = item.get("tenant_id")
        return {
            "pk": pk,
            "sk": "META",
            "channel": text(item["channel"]),
            "expires_at": from_epoch(number(item["expires_at"])),
            "tenant_id": text(tenant) if tenant else None,
        }
