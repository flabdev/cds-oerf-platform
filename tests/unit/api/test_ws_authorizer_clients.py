"""WebSocket $connect accepts the dashboard client and rejects the others."""

import base64
import json
import time

import pytest
from botocore.exceptions import ClientError

from oerf.platform.api import ws_authorizer

pytestmark = pytest.mark.security

_HTTP = "http-client"
_DASHBOARD = "dashboard-client"


def _token(claims: dict[str, object]) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _event(token: str) -> dict[str, object]:
    return {
        "methodArn": "arn:aws:execute-api:us-east-1:123456789012:api/stage/$connect",
        "queryStringParameters": {"token": token},
        "requestContext": {"connectionId": "conn-1"},
    }


def _allow(monkeypatch: pytest.MonkeyPatch, dashboard_id: str = _DASHBOARD) -> None:
    monkeypatch.setattr(ws_authorizer, "USER_POOL_CLIENT_ID", _HTTP)
    monkeypatch.setattr(ws_authorizer, "ALLOWED_CLIENT_IDS", _HTTP)
    monkeypatch.setattr(
        ws_authorizer, "DASHBOARD_CLIENT_PARAMETER", "/oerf/ws-allowed-clients/dashboard"
    )
    monkeypatch.setattr(ws_authorizer, "_dashboard_clients_cache", None)
    monkeypatch.setattr(ws_authorizer, "verify_token", lambda _token: {"Username": "user-1"})
    monkeypatch.setattr(
        ws_authorizer,
        "get_user_attributes",
        lambda _user: {"custom:tenant_id": "t1", "sub": "user-1"},
    )
    monkeypatch.setattr(ws_authorizer, "ROUTING_TABLE_NAME", "")

    class _Parameters:
        def get_parameter(self, *, Name: str) -> dict[str, object]:
            assert Name == "/oerf/ws-allowed-clients/dashboard"
            return {"Parameter": {"Value": dashboard_id}}

    monkeypatch.setattr(ws_authorizer, "get_ssm", lambda: _Parameters())


def _claims(client_id: str, token_use: str = "access") -> dict[str, object]:
    return {
        "client_id": client_id,
        "token_use": token_use,
        "sub": "user-1",
        "cognito:groups": "ops_user",
        "exp": int(time.time()) + 3600,
    }


def test_dashboard_client_access_token_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow(monkeypatch)

    result = ws_authorizer.handler(_event(_token(_claims(_DASHBOARD))), None)

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Allow"
    assert result["context"]["tenant_id"] == "t1"


def test_unknown_client_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow(monkeypatch)

    result = ws_authorizer.handler(_event(_token(_claims("other-client"))), None)

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


def test_id_token_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow(monkeypatch)

    result = ws_authorizer.handler(
        _event(_token(_claims(_DASHBOARD, token_use="id"))),
        None,
    )

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


def test_get_user_failure_logs_the_cognito_code_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    token = "short-access-token"

    class _Cognito:
        def get_user(self, *, AccessToken: str) -> dict[str, str]:
            assert AccessToken == token
            raise ClientError(
                {"Error": {"Code": "NotAuthorizedException", "Message": f"bad {token}"}},
                "GetUser",
            )

    monkeypatch.setattr(ws_authorizer, "USER_POOL_ID", "pool-1")
    monkeypatch.setattr(ws_authorizer, "get_cognito", lambda: _Cognito())

    assert ws_authorizer.verify_token(token) == {}

    text = capsys.readouterr().out
    assert "NotAuthorizedException" in text
    assert token not in text


def test_expired_access_token_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow(monkeypatch)
    claims = _claims(_HTTP)
    claims["exp"] = int(time.time()) - 10

    result = ws_authorizer.handler(_event(_token(claims)), None)

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"
