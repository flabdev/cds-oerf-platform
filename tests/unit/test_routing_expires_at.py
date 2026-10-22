"""Routing rows that used ``ttl`` now store ``expires_at``."""

from __future__ import annotations

import re
from pathlib import Path

from oerf.platform.api import ws_authorizer, ws_handler
from oerf.platform.channels import router

_NOW = 1_700_000_000
_DAY = 86400
_WEEK = 7 * _DAY
_OERF = Path(__file__).resolve().parents[2] / "oerf"
_TTL_ATTRIBUTE = re.compile(r"""['"]ttl['"]\s*:|\[\s*['"]ttl['"]\s*\]\s*=""")


class _Table:
    def __init__(self, items: dict | None = None) -> None:
        self.items = items or {}
        self.put_items: list[dict] = []

    def get_item(self, Key: dict) -> dict:
        return {"Item": self.items.get((Key["pk"], Key["sk"]))}

    def put_item(self, Item: dict) -> None:
        self.put_items.append(Item)
        self.items[(Item["pk"], Item["sk"])] = Item


class _Dynamo:
    def __init__(self, table: _Table) -> None:
        self.table = table

    def Table(self, _name: str) -> _Table:
        return self.table


def _assert_expires(item: dict, expected: int) -> None:
    assert item["expires_at"] == expected
    assert isinstance(item["expires_at"], int)
    assert "ttl" not in item


def test_connect_writes_expires_at_for_one_day(monkeypatch) -> None:
    table = _Table()
    monkeypatch.setattr(ws_handler.time, "time", lambda: _NOW)
    monkeypatch.setattr(ws_handler, "get_table", lambda: table)

    response = ws_handler.handler(
        {
            "requestContext": {
                "routeKey": "$connect",
                "connectionId": "conn-1",
                "authorizer": {"tenant_id": "t1", "role": "ops_user", "sub": "user-1"},
            }
        },
        None,
    )

    item = table.put_items[0]
    assert response["statusCode"] == 200
    assert item["pk"] == "WS#conn-1"
    assert item["sk"] == "METADATA"
    assert item["tenant_id"] == "t1"
    _assert_expires(item, _NOW + _DAY)


def test_subscribe_writes_expires_at_for_one_day(monkeypatch) -> None:
    table = _Table({("WS#conn-1", "METADATA"): {"tenant_id": "t1", "role": "ops_user"}})
    monkeypatch.setattr(ws_handler.time, "time", lambda: _NOW)
    monkeypatch.setattr(ws_handler, "get_table", lambda: table)

    response = ws_handler.handler(
        {
            "requestContext": {"routeKey": "subscribe", "connectionId": "conn-1"},
            "body": '{"scope": "incident", "incident_id": "INC-1"}',
        },
        None,
    )

    item = table.put_items[0]
    assert response["statusCode"] == 200
    assert item["pk"] == "WS_SUB#t1"
    assert item["sk"] == "CONN#conn-1"
    assert item["incident_id"] == "INC-1"
    _assert_expires(item, _NOW + _DAY)


def test_authorizer_binding_writes_expires_at_for_one_day(monkeypatch) -> None:
    table = _Table()
    monkeypatch.setattr(ws_authorizer.time, "time", lambda: _NOW)
    monkeypatch.setattr(ws_authorizer, "ROUTING_TABLE_NAME", "routing")
    monkeypatch.setattr(ws_authorizer, "get_dynamodb", lambda: _Dynamo(table))

    assert ws_authorizer.bind_connection("conn-1", "t1", "ops_user", "user-1") is True

    item = table.put_items[0]
    assert item["pk"] == "WS#conn-1"
    assert item["sk"] == "METADATA"
    assert item["connected_at"] == _NOW
    _assert_expires(item, _NOW + _DAY)


def test_outbound_records_write_expires_at_for_seven_days(monkeypatch) -> None:
    table = _Table()
    monkeypatch.setattr(router.time, "time", lambda: _NOW)
    monkeypatch.setattr(router, "ROUTING_TABLE_NAME", "routing")
    monkeypatch.setattr(router, "get_dynamodb", lambda: _Dynamo(table))

    router.record_outbound_message(
        "t1",
        "inc-1",
        "msg-1",
        "sms",
        "+910000000101",
        "SENT",
        task_token="token-1",
    )

    stored = {(item["pk"], item["sk"]): item for item in table.put_items}
    message = stored[("TENANT#t1#INCIDENT#inc-1", "MSG#msg-1")]
    active = stored[("TENANT#t1#INCIDENT#inc-1", "ACTIVE_TASK_TOKEN")]
    correlation = stored[("MSG_CORRELATION#msg-1", "TOKEN")]
    assert message["created_at"] == _NOW
    assert message["task_token"] == "token-1"
    assert active["task_token"] == "token-1"
    assert correlation["task_token"] == "token-1"
    for item in (message, active, correlation):
        _assert_expires(item, _NOW + _WEEK)


def test_no_dynamodb_item_writes_an_attribute_named_ttl() -> None:
    offenders = [
        f"{path.relative_to(_OERF.parent)}:{number}: {line.strip()}"
        for path in _OERF.rglob("*.py")
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if _TTL_ATTRIBUTE.search(line)
    ]
    assert offenders == []
