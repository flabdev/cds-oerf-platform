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

    response = ws_handler.handle_subscribe(
        table,
        {"body": json.dumps({"tenant_id": "tenant-2", "incident_id": "INC-1"})},
        "conn-1",
        12345,
    )

    subscription = table.put_items[-1]
    assert response["statusCode"] == 200
    assert subscription["pk"] == "WS_SUB#tenant-1"
    assert subscription["tenant_id"] == "tenant-1"
    assert subscription["incident_id"] == "INC-1"


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


def test_failed_stepfunctions_callback_keeps_stored_task_token(monkeypatch):
    token_key = {"pk": "TENANT#tenant-1#INCIDENT#INC-1", "sk": "ACTIVE_TASK_TOKEN"}
    table = FakeTable(
        {
            (token_key["pk"], token_key["sk"]): {
                **token_key,
                "task_token": "task-token-1",
                "ttl": 9999999999,
            }
        }
    )

    monkeypatch.setattr(action_handler, "INCIDENT_TABLE_NAME", "")
    monkeypatch.setattr(action_handler, "ROUTING_TABLE_NAME", "routing")
    monkeypatch.setattr(action_handler, "get_dynamodb", lambda: FakeDynamoDB(table))

    class FailedStepFunctions:
        def send_task_success(self, **_kwargs):
            raise RuntimeError("callback unavailable")

    monkeypatch.setattr(action_handler, "get_sfn", lambda: FailedStepFunctions())

    response = action_handler.handle_operator_action(
        "tenant-1", "INC-1", {"action": "APPROVE"}
    )

    assert response["statusCode"] == 502
    assert json.loads(response["body"])["success"] is False
    assert table.deleted_keys == []
