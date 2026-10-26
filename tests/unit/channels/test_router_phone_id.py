"""Live WhatsApp reads its phone-number id from SSM once per process."""

from __future__ import annotations

import boto3
from botocore.stub import Stubber

from oerf.platform.channels import phone_id, router

_PARAM = "/oerf/whatsapp/phone-number-id"
_SSM_ID = "pn-from-ssm"
_ENV_ID = "pn-from-env"
_RECIPIENT = "+910000000101"


def _isolate(monkeypatch) -> None:
    for name in (
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setattr(router, "USE_MOCK_WHATSAPP", False)
    monkeypatch.setattr(router, "WA_PHONE_NUMBER_ID_PARAM", "")
    monkeypatch.setattr(router, "WHATSAPP_PHONE_NUMBER_ID", "")
    monkeypatch.setattr(phone_id, "_cached_phone_number_id", phone_id._PHONE_UNSET)
    monkeypatch.setattr(phone_id, "_ssm", None)


def _ssm(monkeypatch) -> Stubber:
    client = boto3.client("ssm", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.activate()
    monkeypatch.setattr(phone_id, "_ssm", client)
    return stubber


def _social(monkeypatch, sent: list[str]) -> None:
    class _Social:
        def send_whatsapp_message(self, **kwargs: object) -> dict[str, str]:
            sent.append(str(kwargs["originationPhoneNumberId"]))
            return {"messageId": "mid-1"}

    def client(service: str, *_args: object, **_kwargs: object) -> _Social:
        if service == "socialmessaging":
            return _Social()
        raise AssertionError(service)

    monkeypatch.setattr(router.boto3, "client", client)


def _forbid_aws(monkeypatch) -> None:
    def client(service: str, *_args: object, **_kwargs: object) -> None:
        raise AssertionError(service)

    monkeypatch.setattr(router.boto3, "client", client)


def test_live_send_reads_ssm_once_and_ignores_the_env_var(monkeypatch, capsys) -> None:
    _isolate(monkeypatch)
    monkeypatch.setattr(router, "WA_PHONE_NUMBER_ID_PARAM", _PARAM)
    monkeypatch.setattr(router, "WHATSAPP_PHONE_NUMBER_ID", _ENV_ID)
    stubber = _ssm(monkeypatch)
    stubber.add_response(
        "get_parameter",
        {"Parameter": {"Value": _SSM_ID}},
        {"Name": _PARAM, "WithDecryption": True},
    )
    sent: list[str] = []
    _social(monkeypatch, sent)

    first = router.send_whatsapp(_RECIPIENT, "hello")
    second = router.send_whatsapp(_RECIPIENT, "hello")
    text = capsys.readouterr().out

    assert first["success"] is True
    assert second["success"] is True
    assert sent == [_SSM_ID, _SSM_ID]
    assert _SSM_ID not in text
    assert _ENV_ID not in text
    assert _RECIPIENT not in text
    stubber.assert_no_pending_responses()


def test_unset_parameter_name_uses_the_env_var_and_skips_ssm(monkeypatch) -> None:
    _isolate(monkeypatch)
    monkeypatch.setattr(router, "WHATSAPP_PHONE_NUMBER_ID", _ENV_ID)
    stubber = _ssm(monkeypatch)
    sent: list[str] = []
    _social(monkeypatch, sent)

    result = router.send_whatsapp(_RECIPIENT, "hello")

    assert result["success"] is True
    assert sent == [_ENV_ID]
    stubber.assert_no_pending_responses()


def test_empty_parameter_does_not_fall_back_to_the_env_var(monkeypatch, capsys) -> None:
    _isolate(monkeypatch)
    monkeypatch.setattr(router, "WA_PHONE_NUMBER_ID_PARAM", _PARAM)
    monkeypatch.setattr(router, "WHATSAPP_PHONE_NUMBER_ID", _ENV_ID)
    stubber = _ssm(monkeypatch)
    stubber.add_response(
        "get_parameter",
        {"Parameter": {"Value": "   "}},
        {"Name": _PARAM, "WithDecryption": True},
    )
    _forbid_aws(monkeypatch)

    result = router.send_whatsapp(_RECIPIENT, "hello")
    text = capsys.readouterr().out

    assert result["success"] is False
    assert result["error"] == "wa_phone_id_missing"
    assert _ENV_ID not in result["error"]
    assert "wa_phone_id_missing" in text
    assert _ENV_ID not in text
    assert _RECIPIENT not in text
    stubber.assert_no_pending_responses()


def test_ssm_error_fails_closed_without_the_exception_text(monkeypatch, capsys) -> None:
    _isolate(monkeypatch)
    monkeypatch.setattr(router, "WA_PHONE_NUMBER_ID_PARAM", _PARAM)
    monkeypatch.setattr(router, "WHATSAPP_PHONE_NUMBER_ID", _ENV_ID)
    stubber = _ssm(monkeypatch)
    stubber.add_client_error(
        "get_parameter",
        service_error_code="AccessDeniedException",
        service_message="ssm-detail-do-not-log",
        expected_params={"Name": _PARAM, "WithDecryption": True},
    )
    _forbid_aws(monkeypatch)

    first = router.send_whatsapp(_RECIPIENT, "hello")
    second = router.send_whatsapp(_RECIPIENT, "hello")
    text = capsys.readouterr().out

    assert first["error"] == "wa_phone_id_missing"
    assert second["error"] == "wa_phone_id_missing"
    assert "ssm-detail-do-not-log" not in first["error"]
    assert "AccessDeniedException" not in first["error"]
    assert "ssm-detail-do-not-log" not in text
    assert "AccessDeniedException" not in text
    assert "wa_phone_id_unreadable" in text
    assert _ENV_ID not in text
    stubber.assert_no_pending_responses()


def test_mock_path_does_not_read_ssm(monkeypatch, capsys) -> None:
    _isolate(monkeypatch)
    monkeypatch.setattr(router, "USE_MOCK_WHATSAPP", True)
    monkeypatch.setattr(router, "WA_PHONE_NUMBER_ID_PARAM", _PARAM)
    stubber = _ssm(monkeypatch)

    result = router.send_whatsapp(_RECIPIENT, "body-do-not-log")
    text = capsys.readouterr().out

    assert result["status"] == "SIMULATED"
    assert result["success"] is False
    assert phone_id._cached_phone_number_id is phone_id._PHONE_UNSET
    assert "body-do-not-log" not in text
    assert _RECIPIENT not in text
    stubber.assert_no_pending_responses()
