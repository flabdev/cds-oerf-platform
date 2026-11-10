"""Private persistence for the pending human QA task-token review."""

from __future__ import annotations

import threading
from typing import Any, Protocol

from oerf.platform.storage.dynamo import incident_pk, key, s, text


class QAReviewStore(Protocol):
    def put(self, *, tenant_id: str, incident_id: str, task_token: str) -> None: ...

    def get(self, *, tenant_id: str, incident_id: str) -> str | None: ...

    def delete(self, *, tenant_id: str, incident_id: str) -> None: ...


class DynamoQAReviewStore:
    """Store the private callback token in the incident table."""

    def __init__(self, client: Any, table_name: str) -> None:
        self._client = client
        self._table = table_name

    def put(self, *, tenant_id: str, incident_id: str, task_token: str) -> None:
        self._client.put_item(
            TableName=self._table,
            Item={
                **key(incident_pk(tenant_id, incident_id), "QA_REVIEW"),
                "tenant_id": s(tenant_id),
                "incident_id": s(incident_id),
                "task_token": s(task_token),
            },
        )

    def get(self, *, tenant_id: str, incident_id: str) -> str | None:
        response = self._client.get_item(
            TableName=self._table,
            Key=key(incident_pk(tenant_id, incident_id), "QA_REVIEW"),
            ConsistentRead=True,
        )
        item = response.get("Item")
        return text(item["task_token"]) if item else None

    def delete(self, *, tenant_id: str, incident_id: str) -> None:
        self._client.delete_item(
            TableName=self._table,
            Key=key(incident_pk(tenant_id, incident_id), "QA_REVIEW"),
        )


class MemoryQAReviewStore:
    """Thread-safe test adapter for pending callback tokens."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, str], str] = {}

    def put(self, *, tenant_id: str, incident_id: str, task_token: str) -> None:
        with self._lock:
            self._rows[(tenant_id, incident_id)] = task_token

    def get(self, *, tenant_id: str, incident_id: str) -> str | None:
        with self._lock:
            return self._rows.get((tenant_id, incident_id))

    def delete(self, *, tenant_id: str, incident_id: str) -> None:
        with self._lock:
            self._rows.pop((tenant_id, incident_id), None)
