"""DynamoDB step results: one conditional row per ``(incident, step)``.

The client is injected. This module never constructs one. The row keeps the
first write for a key, because Step Functions retries and a retried step must
replay the decision a reviewer already saw rather than replace it.

Keys follow the incident table convention::

    pk = TENANT#{tenant_id}#INC#{incident_id}
    sk = STEP#{step}
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from oerf.platform.agent.idempotency import StepRecord
from oerf.platform.storage.dynamo import (
    Item,
    is_conditional_failure,
    iso_z,
    key,
    required_text,
    s,
    text,
)
from oerf.platform.tenancy.context import TenantContext

_JSON = ("decision", "result", "step_output")


class DynamoStepResultStore:
    """``StepResultStore`` on the incident table."""

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

    def get(self, *, tenant_id: str, incident_id: str, step: str) -> StepRecord | None:
        context = TenantContext(tenant_id=tenant_id)
        incident = required_text(incident_id, "incident_id")
        name = required_text(step, "step")
        loaded = self._client.get_item(
            TableName=self._table,
            Key=key(f"TENANT#{context.tenant_id}#INC#{incident}", f"STEP#{name}"),
        )
        item = loaded.get("Item")
        if not item:
            return None
        return _record(item, context.tenant_id, incident, name)

    def put(
        self,
        *,
        tenant_id: str,
        incident_id: str,
        step: str,
        decision: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        step_output: dict[str, Any] | None = None,
    ) -> StepRecord:
        """Write the outcome unless the row exists, then return the current row."""
        context = TenantContext(tenant_id=tenant_id)
        incident = required_text(incident_id, "incident_id")
        name = required_text(step, "step")
        pk = f"TENANT#{context.tenant_id}#INC#{incident}"
        sk = f"STEP#{name}"
        item: Item = {
            **key(pk, sk),
            "tenant_id": s(context.tenant_id),
            "incident_id": s(incident),
            "step": s(name),
            "completed_at": s(iso_z(self._clock())),
        }
        if decision is not None:
            item["decision"] = s(json.dumps(decision))
        if result is not None:
            item["result"] = s(json.dumps(result))
        if step_output is not None:
            item["step_output"] = s(json.dumps(step_output))
        try:
            self._client.put_item(
                TableName=self._table,
                Item=item,
                ConditionExpression="attribute_not_exists(#pk)",
                ExpressionAttributeNames={"#pk": "pk"},
            )
        except Exception as exc:
            if not is_conditional_failure(exc):
                raise
            existing = self.get(tenant_id=context.tenant_id, incident_id=incident, step=name)
            if existing is None:
                raise
            return existing
        return _record(item, context.tenant_id, incident, name)


def _record(item: Item, tenant_id: str, incident_id: str, step: str) -> StepRecord:
    return StepRecord(
        tenant_id=tenant_id,
        incident_id=incident_id,
        step=step,
        completed_at=text(item["completed_at"]) if "completed_at" in item else "",
        decision=_json(item, "decision"),
        result=_json(item, "result") or {},
        step_output=_json(item, "step_output"),
    )


def _json(item: Item, name: str) -> dict[str, Any] | None:
    if name not in item:
        return None
    parsed = json.loads(text(item[name]))
    return parsed if isinstance(parsed, dict) else None
