"""Trigger uses the token tenant and does not write when that claim is missing."""

import json
import logging

from oerf.platform.api import action_handler


class _Table:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def put_item(self, Item: dict) -> None:
        self.items.append(Item)


class _Dynamo:
    def __init__(self, table: _Table) -> None:
        self.table = table

    def Table(self, _name: str) -> _Table:
        return self.table


class _Executions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def start_execution(self, **kwargs: object) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"executionArn": "arn:aws:states:::execution/demo"}


def _forbid(name: str):
    def _call() -> None:
        raise AssertionError(f"{name} called")

    return _call


def test_trigger_without_a_token_tenant_does_not_write(monkeypatch, caplog) -> None:
    monkeypatch.setattr(action_handler, "INCIDENT_TABLE_NAME", "incidents")
    monkeypatch.setattr(action_handler, "STATE_MACHINE_ARN", "arn:aws:states:::stateMachine:demo")
    monkeypatch.setattr(action_handler, "get_dynamodb", _forbid("dynamodb"))
    monkeypatch.setattr(action_handler, "get_sfn", _forbid("stepfunctions"))
    caplog.set_level(logging.ERROR)

    response = action_handler.handler(
        {
            "rawPath": "/incidents/trigger",
            "requestContext": {
                "http": {"method": "POST"},
                "authorizer": {
                    "jwt": {"claims": {"sub": "user-1", "cognito:groups": "platform_admin"}}
                },
            },
            "queryStringParameters": {"tenant_id": "from-query"},
            "body": json.dumps({"tenant_id": "from-body", "shipment_id": "PH-9"}),
        },
        None,
    )

    assert response["statusCode"] == 403
    assert json.loads(response["body"]) == {"error": "tenant_required"}
    assert "from-body" not in caplog.text
    assert "from-query" not in caplog.text
    assert "PH-9" not in caplog.text


def test_unauthenticated_trigger_does_not_write(monkeypatch) -> None:
    monkeypatch.setattr(action_handler, "INCIDENT_TABLE_NAME", "incidents")
    monkeypatch.setattr(action_handler, "get_dynamodb", _forbid("dynamodb"))
    monkeypatch.setattr(action_handler, "get_sfn", _forbid("stepfunctions"))

    response = action_handler.handler(
        {
            "rawPath": "/incidents/trigger",
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps({"tenant_id": "from-body"}),
        },
        None,
    )

    assert response["statusCode"] == 401
    assert json.loads(response["body"]) == {"error": "unauthorized"}


def test_trigger_uses_the_token_tenant_and_ignores_the_body(monkeypatch) -> None:
    table = _Table()
    executions = _Executions()
    monkeypatch.setattr(action_handler, "INCIDENT_TABLE_NAME", "incidents")
    monkeypatch.setattr(action_handler, "STATE_MACHINE_ARN", "arn:aws:states:::stateMachine:demo")
    monkeypatch.setattr(action_handler, "get_dynamodb", lambda: _Dynamo(table))
    monkeypatch.setattr(action_handler, "get_sfn", lambda: executions)

    response = action_handler.handler(
        {
            "rawPath": "/incidents/trigger",
            "requestContext": {
                "http": {"method": "POST"},
                "authorizer": {
                    "jwt": {
                        "claims": {
                            "sub": "admin-1",
                            "custom:tenant_id": "token-tenant",
                            "cognito:groups": "platform_admin",
                        }
                    }
                },
            },
            "body": json.dumps({"tenant_id": "body-tenant", "shipment_id": "PH-1"}),
        },
        None,
    )

    assert response["statusCode"] == 201
    assert table.items[0]["tenant_id"] == "token-tenant"
    assert table.items[0]["pk"] == "TENANT#token-tenant"
    assert "body-tenant" not in json.dumps(table.items)
    assert json.loads(executions.calls[0]["input"])["tenant_id"] == "token-tenant"
