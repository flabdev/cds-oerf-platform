"""Stream broadcaster isolation, envelope, and batch failures. No AWS calls."""

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from oerf.platform.api import stream_broadcaster as broadcaster

PHONE = "+910000000101"
TOKEN = "T" * 40
_SECRET = "SECRET-TIMELINE"
_WHEN = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def _ddb(item: dict) -> dict:
    serializer = TypeSerializer()
    return {key: serializer.serialize(value) for key, value in item.items()}


def _meta(**overrides) -> dict:
    item = {
        "pk": "TENANT#tenant-a#INC#INC-X",
        "sk": "META",
        "tenant_id": "tenant-a",
        "incident_id": "INC-X",
        "state": "VERIFYING",
        "current_stage": "verify",
        "event_seq": 4,
        "opened_at": "2026-10-18T11:00:00Z",
        "updated_at": "2026-10-18T12:00:00Z",
        "recommended_option": "facility-a",
        "options": [
            {"id": "facility-a", "name": "Ada Example", "phone": PHONE},
            {"id": "continue"},
        ],
        "timeline": [{"type": _SECRET, "phone": PHONE}],
        "evidence_analysis": {"token": TOKEN, "image": "raw-bytes"},
        "task_token": TOKEN,
        "name": "Ada Example",
    }
    item.update(overrides)
    return item


def _record(
    item: dict,
    *,
    event_name: str = "MODIFY",
    sequence: str = "111",
    image: str = "NewImage",
) -> dict:
    raw = _ddb(item)
    dynamodb = {
        "SequenceNumber": sequence,
        "Keys": {"pk": raw["pk"], "sk": raw["sk"]},
        image: raw,
    }
    return {"eventName": event_name, "dynamodb": dynamodb}


def _sub(
    connection_id: str,
    scope: str | None = None,
    incident_id: str | None = None,
    tenant: str = "tenant-a",
) -> dict:
    item = {
        "pk": f"WS_SUB#{tenant}",
        "sk": f"CONN#{connection_id}",
        "connection_id": connection_id,
        "tenant_id": tenant,
    }
    if scope is not None:
        item["scope"] = scope
    if incident_id is not None:
        item["incident_id"] = incident_id
    return item


class _Table:
    def __init__(self, items: list[dict], error: Exception | None = None) -> None:
        self.items = items
        self.deleted: list[dict] = []
        self.queried: list[str] = []
        self.error = error

    def query(self, **kwargs):
        if self.error is not None:
            raise self.error
        pk = kwargs["ExpressionAttributeValues"][":pk"]
        self.queried.append(pk)
        return {"Items": [item for item in self.items if item["pk"] == pk]}

    def delete_item(self, Key):
        self.deleted.append(Key)


class _Api:
    def __init__(self, gone: set[str] | None = None, fail: set[str] | None = None) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.gone = gone or set()
        self.fail = fail or set()

    def post_to_connection(self, ConnectionId, Data):
        if ConnectionId in self.gone:
            raise ClientError(_error("GoneException", 410), "PostToConnection")
        if ConnectionId in self.fail:
            raise ClientError(_error("LimitExceededException", 429), "PostToConnection")
        self.sent.append((ConnectionId, json.loads(Data.decode())))


class _Dynamo:
    def __init__(self, table: _Table) -> None:
        self._table = table

    def Table(self, name):
        return self._table


def _error(code: str, status: int) -> dict:
    return {
        "Error": {"Code": code, "Message": code},
        "ResponseMetadata": {"HTTPStatusCode": status},
    }


def _wire(monkeypatch, table: _Table, api: _Api) -> None:
    monkeypatch.setattr(broadcaster, "ROUTING_TABLE_NAME", "routing")
    monkeypatch.setattr(broadcaster, "WEBSOCKET_ENDPOINT", "https://example.com/prod")
    monkeypatch.setattr(broadcaster, "get_dynamodb", lambda: _Dynamo(table))
    monkeypatch.setattr(broadcaster, "get_apigw", lambda: api)
    monkeypatch.setattr(broadcaster, "_now", lambda: _WHEN)


def _run(monkeypatch, items: list[dict], records: list[dict], api: _Api | None = None, error=None):
    table = _Table(items, error)
    client = api or _Api()
    _wire(monkeypatch, table, client)
    result = broadcaster.handler({"Records": records}, None)
    return result, table, client


def _assert_clean(payloads: list[dict]) -> None:
    text = json.dumps(payloads)
    assert _SECRET not in text
    assert "timeline" not in text
    assert "evidence_analysis" not in text
    assert PHONE not in text
    assert TOKEN not in text
    assert "Ada Example" not in text
    assert "task_token" not in text


@pytest.mark.security
def test_tenant_event_never_reaches_another_tenant(monkeypatch) -> None:
    items = [
        _sub("conn-a", "incident", "INC-X", tenant="tenant-a"),
        _sub("conn-b", "incident", "INC-X", tenant="tenant-b"),
        _sub("conn-list", "tenant", tenant="tenant-b"),
    ]
    result, table, api = _run(monkeypatch, items, [_record(_meta())])

    assert result == {"batchItemFailures": []}
    assert table.queried == ["WS_SUB#tenant-a"]
    assert [item[0] for item in api.sent] == ["conn-a"]
    _assert_clean([item[1] for item in api.sent])


@pytest.mark.security
def test_incident_event_never_reaches_another_incident(monkeypatch) -> None:
    items = [
        _sub("conn-x", "incident", "INC-X"),
        _sub("conn-y", "incident", "INC-Y"),
    ]
    _result, _table, api = _run(monkeypatch, items, [_record(_meta())])

    assert [item[0] for item in api.sent] == ["conn-x"]
    assert api.sent[0][1]["incident_id"] == "INC-X"
    assert api.sent[0][1]["event_type"] == "INCIDENT_UPDATE"


@pytest.mark.security
def test_tenant_list_rows_get_summary_keys_only(monkeypatch) -> None:
    items = [_sub("conn-list", "tenant"), _sub("conn-x", "incident", "INC-X")]
    _result, _table, api = _run(monkeypatch, items, [_record(_meta())])

    by_connection = dict(api.sent)
    summary = by_connection["conn-list"]
    assert summary["event_type"] == "INCIDENT_SUMMARY"
    assert set(summary) == {
        "event_type",
        "tenant_id",
        "incident_id",
        "version",
        "emitted_at",
        "data",
    }
    assert summary["data"] == {
        "incident_id": "INC-X",
        "state": "VERIFYING",
        "stage": "verify",
        "version": 4,
    }
    incident = by_connection["conn-x"]
    assert incident["event_type"] == "INCIDENT_UPDATE"
    assert set(incident["data"]) == {
        "incident_id",
        "state",
        "stage",
        "updated_at",
        "version",
    }
    assert incident["data"]["incident_id"] == "INC-X"
    assert incident["data"]["version"] == 4
    assert isinstance(incident["data"]["version"], int)
    assert incident["version"] == 4
    assert incident["emitted_at"] == "2026-10-18T12:00:00Z"
    _assert_clean(list(by_connection.values()))


_DESK_EVENTS = Path(__file__).resolve().parents[3] / "web" / "src" / "deskEvents.js"
_DESK_CHECK = """
import { pathToFileURL } from "node:url";
const { acceptDeskEvent } = await import(pathToFileURL(process.env.DESK_EVENTS).href);
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const input = JSON.parse(Buffer.concat(chunks).toString("utf8"));
const update = acceptDeskEvent(input.update, {
  tenantId: input.update.tenant_id,
  scope: "incident",
  incidentId: input.update.incident_id,
});
const summary = acceptDeskEvent(input.summary, {
  tenantId: input.summary.tenant_id,
  scope: "tenant",
  incidentId: "",
});
const otherTenant = acceptDeskEvent(input.update, {
  tenantId: "other-tenant",
  scope: "incident",
  incidentId: input.update.incident_id,
});
const otherIncident = acceptDeskEvent(input.update, {
  tenantId: input.update.tenant_id,
  scope: "incident",
  incidentId: "INC-OTHER",
});
const leaked = acceptDeskEvent(
  {
    ...input.update,
    data: { ...input.update.data, evidence_analysis: { fault: "E17" } },
  },
  {
    tenantId: input.update.tenant_id,
    scope: "incident",
    incidentId: input.update.incident_id,
  },
);
process.stdout.write(JSON.stringify({
  update, summary, otherTenant, otherIncident, leaked,
}));
"""


def _accept_on_desk(update: dict, summary: dict) -> dict:
    """Run the real desk validator. Node resolves ``web/src/deskEvents.js``."""

    result = subprocess.run(
        ["node", "--input-type=module", "-e", _DESK_CHECK],
        input=json.dumps({"update": update, "summary": summary}),
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "DESK_EVENTS": str(_DESK_EVENTS)},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_desk_accepts_broadcaster_update_and_summary(monkeypatch) -> None:
    items = [_sub("conn-x", "incident", "INC-X"), _sub("conn-list", "tenant")]
    _result, _table, api = _run(monkeypatch, items, [_record(_meta())])
    by_connection = dict(api.sent)
    accepted = _accept_on_desk(by_connection["conn-x"], by_connection["conn-list"])

    assert accepted["update"]["kind"] == "update"
    assert accepted["update"]["incident"]["incident_id"] == "INC-X"
    assert accepted["update"]["incident"]["version"] == 4
    assert accepted["summary"]["kind"] == "summary"
    assert accepted["summary"]["incident"] == {
        "incident_id": "INC-X",
        "state": "VERIFYING",
        "stage": "verify",
        "version": 4,
    }
    assert accepted["otherTenant"] is None
    assert accepted["otherIncident"] is None
    assert accepted["leaked"] is None


@pytest.mark.security
def test_no_timeline_or_evidence_in_any_payload(monkeypatch) -> None:
    items = [_sub("conn-x", "incident", "INC-X"), _sub("conn-list", "tenant")]
    _result, _table, api = _run(monkeypatch, items, [_record(_meta())])

    assert len(api.sent) == 2
    _assert_clean([item[1] for item in api.sent])


@pytest.mark.security
def test_row_without_scope_receives_nothing(monkeypatch) -> None:
    items = [
        _sub("conn-empty"),
        _sub("conn-bare", incident_id="INC-X"),
        _sub("conn-all", "incident", "ALL"),
        _sub("conn-star", "incident", "*"),
        _sub("conn-word"),
    ]
    items[4]["scope"] = "ALL"
    _result, _table, api = _run(monkeypatch, items, [_record(_meta())])

    assert api.sent == []


def test_remove_is_skipped_because_the_contract_has_no_removed_event(monkeypatch, capsys) -> None:
    items = [_sub("conn-list", "tenant")]
    result, _table, api = _run(
        monkeypatch,
        items,
        [_record(_meta(), event_name="REMOVE", image="OldImage")],
    )

    assert result == {"batchItemFailures": []}
    assert api.sent == []
    text = capsys.readouterr().out
    assert "stream_remove_skipped" in text
    assert "removed" not in text
    assert _SECRET not in text


def test_gone_deletes_subscription_and_connection_metadata(monkeypatch) -> None:
    items = [_sub("conn-1", "incident", "INC-X")]
    api = _Api(gone={"conn-1"})
    result, table, api = _run(monkeypatch, items, [_record(_meta())], api)

    assert result == {"batchItemFailures": []}
    assert api.sent == []
    assert {"pk": "WS_SUB#tenant-a", "sk": "CONN#conn-1"} in table.deleted
    assert {"pk": "WS#conn-1", "sk": "METADATA"} in table.deleted


def test_retryable_delivery_is_a_partial_batch_failure(monkeypatch) -> None:
    items = [_sub("conn-ok", "incident", "INC-X"), _sub("conn-slow", "tenant")]
    api = _Api(fail={"conn-slow"})
    result, _table, api = _run(
        monkeypatch,
        items,
        [_record(_meta(), sequence="bad")],
        api,
    )

    assert result == {"batchItemFailures": [{"itemIdentifier": "bad"}]}
    assert [item[0] for item in api.sent] == ["conn-ok"]


def test_malformed_record_is_skipped(monkeypatch) -> None:
    items = [_sub("conn-x", "incident", "INC-X")]
    records = [
        {"eventName": "INSERT"},
        _record({"pk": "DEBOUNCE#tenant-a#ship#dev", "sk": "META", "tenant_id": "tenant-a"}),
        _record(_meta(), sequence="good"),
        "not-a-record",
    ]
    result, _table, api = _run(monkeypatch, items, records)

    assert result == {"batchItemFailures": []}
    assert [item[0] for item in api.sent] == ["conn-x"]
