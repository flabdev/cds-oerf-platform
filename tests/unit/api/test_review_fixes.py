"""Regression tests for PR #5 review fixes."""

import base64
import json

from oerf.platform.api import action_handler, ws_authorizer, ws_handler
from oerf.platform.channels import router


class FakeTable:
    def __init__(self, items=None):
        self.items = items or {}
        self.put_items = []
        self.deleted_keys = []

    def get_item(self, Key):
        return {"Item": self.items.get((Key["pk"], Key["sk"]))}

    def put_item(self, Item):
        self.put_items.append(Item)
        self.items[(Item["pk"], Item["sk"])] = Item

    def delete_item(self, Key):
        self.deleted_keys.append(Key)


class FakeDynamoDB:
    def __init__(self, table):
        self.table = table

    def Table(self, _name):
        return self.table


def test_websocket_authorizer_returns_policy_and_binds_verified_tenant(monkeypatch):
    claims = {"client_id": "client-1", "sub": "user-1", "cognito:groups": "tenant_admin"}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    token = f"header.{payload}.signature"
    table = FakeTable()

    monkeypatch.setattr(ws_authorizer, "USER_POOL_CLIENT_ID", "client-1")
    monkeypatch.setattr(ws_authorizer, "verify_token", lambda supplied: {"Username": "user-1"})
    monkeypatch.setattr(
        ws_authorizer,
        "get_user_attributes",
        lambda _user: {"custom:tenant_id": "tenant-1", "sub": "user-1"},
    )
    monkeypatch.setattr(ws_authorizer, "get_dynamodb", lambda: FakeDynamoDB(table))
    monkeypatch.setattr(ws_authorizer, "ROUTING_TABLE_NAME", "routing")

    result = ws_authorizer.handler(
        {
            "methodArn": "arn:aws:execute-api:us-east-1:123456789012:api/stage/$connect",
            "queryStringParameters": {"token": token},
            "requestContext": {"connectionId": "conn-1"},
        },
        None,
    )

    assert result["principalId"] == "user-1"
    assert "isAuthenticated" not in result
    assert result["policyDocument"] == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Action": "execute-api:Invoke",
                "Effect": "Allow",
                "Resource": "arn:aws:execute-api:us-east-1:123456789012:api/stage/$connect",
            }
        ],
    }
    assert result["context"]["tenant_id"] == "tenant-1"
    assert table.items[("WS#conn-1", "METADATA")]["tenant_id"] == "tenant-1"


def test_websocket_authorizer_works_when_gateway_omits_connection_id(monkeypatch):
    claims = {"client_id": "client-1", "sub": "user-1"}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    token = f"header.{payload}.signature"

    monkeypatch.setattr(ws_authorizer, "USER_POOL_CLIENT_ID", "client-1")
    monkeypatch.setattr(ws_authorizer, "verify_token", lambda _token: {"Username": "user-1"})
    monkeypatch.setattr(
        ws_authorizer,
        "get_user_attributes",
        lambda _user: {"custom:tenant_id": "tenant-1", "sub": "user-1"},
    )
    monkeypatch.setattr(ws_authorizer, "ROUTING_TABLE_NAME", "routing")

    result = ws_authorizer.handler(
        {
            "methodArn": "arn:aws:execute-api:us-east-1:123456789012:api/stage/$connect",
            "queryStringParameters": {"token": token},
            "requestContext": {},
        },
        None,
    )

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Allow"
    assert result["context"]["tenant_id"] == "tenant-1"


def test_websocket_authorizer_denies_invalid_token_with_policy(monkeypatch):
    monkeypatch.setattr(ws_authorizer, "verify_token", lambda _token: {})

    result = ws_authorizer.handler(
        {
            "methodArn": "arn:aws:execute-api:us-east-1:123456789012:api/stage/$connect",
            "queryStringParameters": {"token": "invalid"},
            "requestContext": {"connectionId": "conn-1"},
        },
        None,
    )

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"
    assert "isAuthenticated" not in result


def test_subscribe_uses_connection_tenant_not_client_requested_tenant():
    table = FakeTable(
        {("WS#conn-1", "METADATA"): {"tenant_id": "tenant-1", "role": "ops_user"}}
    )

    rejected = ws_handler.handle_subscribe(
        table,
        {
            "body": json.dumps(
                {"tenant_id": "tenant-2", "scope": "incident", "incident_id": "INC-1"}
            )
        },
        "conn-1",
        12345,
    )
    accepted = ws_handler.handle_subscribe(
        table,
        {"body": json.dumps({"scope": "incident", "incident_id": "INC-1"})},
        "conn-1",
        12345,
    )

    subscription = table.put_items[-1]
    assert rejected["statusCode"] == 400
    assert accepted["statusCode"] == 200
    assert subscription["pk"] == "WS_SUB#tenant-1"
    assert subscription["tenant_id"] == "tenant-1"
    assert subscription["scope"] == "incident"
    assert subscription["incident_id"] == "INC-1"


def test_subscribe_rejects_all_and_stores_a_tenant_scope():
    table = FakeTable({("WS#conn-1", "METADATA"): {"tenant_id": "tenant-1"}})

    rejected = ws_handler.handle_subscribe(
        table,
        {"body": json.dumps({"scope": "incident", "incident_id": "ALL"})},
        "conn-1",
        12345,
    )
    accepted = ws_handler.handle_subscribe(
        table,
        {"body": json.dumps({"scope": "tenant"})},
        "conn-1",
        12345,
    )

    assert rejected["statusCode"] == 400
    assert accepted["statusCode"] == 200
    assert table.put_items[-1]["scope"] == "tenant"
    assert "incident_id" not in table.put_items[-1]


def test_subscribe_rejects_connection_without_tenant_binding():
    table = FakeTable()

    response = ws_handler.handle_subscribe(
        table,
        {"body": json.dumps({"tenant_id": "tenant-2"})},
        "unbound-connection",
        12345,
    )

    assert response["statusCode"] == 401
    assert table.put_items == []


def test_email_without_source_address_is_not_reported_as_success(monkeypatch):
    monkeypatch.setattr(router, "USE_MOCK_EMAIL", False)
    monkeypatch.setattr(router, "SES_SOURCE_EMAIL", "")

    def unexpected_ses_call():
        raise AssertionError("SES called")

    monkeypatch.setattr(router, "get_ses", unexpected_ses_call)

    result = router.send_email("driver@example.com", "Alert", "Body")

    assert result["success"] is False
    assert "SES_SOURCE_EMAIL not configured" in result["error"]


def test_dashboard_action_is_queued_and_does_not_resume_the_workflow(monkeypatch):
    sent = []

    class Queue:
        def send_message(self, **kwargs):
            sent.append(kwargs)

    monkeypatch.setattr(action_handler, "INBOUND_QUEUE_URL", "https://example/inbound")
    monkeypatch.setattr(action_handler, "get_sqs", lambda: Queue())
    monkeypatch.setattr(
        action_handler,
        "get_sfn",
        lambda: (_ for _ in ()).throw(AssertionError("step functions")),
    )

    response = action_handler.handler(_action_event(), None)
    body = json.loads(sent[0]["MessageBody"])

    assert response["statusCode"] == 202
    assert json.loads(response["body"]) == {"action_id": "act-1", "status": "queued"}
    assert body["action"] == "confirm_transfer"
    assert body["tenant_id"] == "t1"
    assert body["user_id"] == "user-1"
    assert body["text"] is None
    assert "task_token" not in body


def test_old_action_route_is_not_served():
    event = _action_event()
    event["rawPath"] = "/incidents/INC-1/action"
    event["body"] = json.dumps({"action": "APPROVE"})

    response = action_handler.handler(event, None)

    assert response["statusCode"] == 404


def _action_event() -> dict:
    return {
        "rawPath": "/incidents/INC-1/actions",
        "requestContext": {
            "http": {"method": "POST"},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": "user-1",
                        "custom:tenant_id": "t1",
                        "cognito:groups": "ops_user",
                    }
                }
            },
        },
        "body": json.dumps(
            {
                "action": "confirm_transfer",
                "action_id": "act-1",
                "engagement_id": "ENG-03",
            }
        ),
    }
