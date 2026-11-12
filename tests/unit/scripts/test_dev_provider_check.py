"""Read-only dev provider check. The phone id never appears in the output."""

from __future__ import annotations

import pytest

from scripts.dev_provider_check import main

_PHONE = "phone-number-id-abc123secret"
_BARE = "abc123secret"
_WABA = "waba-0000000000000000"


class _Session:
    def __init__(
        self,
        *,
        phone: str = _PHONE,
        arn_id: str = _BARE,
        star: bool = False,
        flag: str = "false",
        templates: list[dict] | None = None,
        missing_phone: bool = False,
    ) -> None:
        self.phone = phone
        self.arn_id = arn_id
        self.star = star
        self.flag = flag
        self.templates = templates or []
        self.missing_phone = missing_phone

    def client(self, service: str, **_kwargs: object) -> _Session:
        del service
        return self

    def get_parameter(self, *, Name: str) -> dict:
        if Name.endswith("phone-number-id"):
            if self.missing_phone:
                raise RuntimeError("missing")
            return {"Parameter": {"Value": self.phone}}
        if Name.endswith("waba-id"):
            return {"Parameter": {"Value": _WABA}}
        raise AssertionError(Name)

    def get_function_configuration(self, *, FunctionName: str) -> dict:
        assert FunctionName == "oerf-dev-channel-router"
        return {
            "Role": "arn:aws:iam::000000000000:role/router-dev",
            "Environment": {"Variables": {"WHATSAPP_STAGE_URL_TEMPLATES": self.flag}},
        }

    def list_role_policies(self, **_kwargs: object) -> dict:
        return {"PolicyNames": ["inline"], "IsTruncated": False}

    def get_role_policy(self, **_kwargs: object) -> dict:
        resource = (
            "*"
            if self.star
            else f"arn:aws:social-messaging:us-east-1:000000000000:phone-number-id/{self.arn_id}"
        )
        return {
            "PolicyDocument": {
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "social-messaging:SendWhatsAppMessage",
                        "Resource": resource,
                    }
                ]
            }
        }

    def list_attached_role_policies(self, **_kwargs: object) -> dict:
        return {"AttachedPolicies": [], "IsTruncated": False}

    def list_whatsapp_message_templates(self, **_kwargs: object) -> dict:
        return {"templates": self.templates}


def _run(session: _Session) -> tuple[int, str]:
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(
            ["--stage", "dev", "--profile", "p"],
            session_factory=lambda _profile: session,
        )
    return code, buffer.getvalue()


def test_matching_phone_and_scoped_policy_pass_without_printing_the_id() -> None:
    code, text = _run(
        _Session(
            templates=[
                {
                    "templateName": "oerf_driver_checkin",
                    "templateLanguage": "en",
                    "templateStatus": "APPROVED",
                }
            ]
        )
    )
    assert code == 0
    assert "PASS parameter /oerf/dev/whatsapp/phone-number-id" in text
    assert "PASS phone-id match" in text
    assert "PASS social-messaging scoped" in text
    assert "oerf_dev_ops_escalation_options NOT SUBMITTED" in text
    assert "oerf_driver_checkin APPROVED" in text
    assert "STAGE_URL_TEMPLATES false" in text
    assert "WARNING" not in text
    assert _BARE not in text
    assert _PHONE not in text
    assert _WABA not in text


def test_flag_true_while_a_copy_is_pending_warns_and_still_exits_zero() -> None:
    code, text = _run(
        _Session(
            flag="true",
            templates=[
                {
                    "templateName": "oerf_dev_ops_escalation_options",
                    "templateLanguage": "en",
                    "templateStatus": "PENDING",
                },
                {
                    "templateName": "oerf_dev_qa_report_ready",
                    "templateLanguage": "en",
                    "templateStatus": "APPROVED",
                },
            ],
        )
    )
    assert code == 0
    assert "STAGE_URL_TEMPLATES true" in text
    assert "WARNING STAGE_URL_TEMPLATES is true while a dev copy is not APPROVED" in text
    assert "oerf_dev_ops_escalation_options PENDING" in text
    assert _BARE not in text


def test_star_resource_and_a_missing_parameter_fail() -> None:
    code, text = _run(_Session(star=True, missing_phone=True))
    assert code == 1
    assert "FAIL parameter /oerf/dev/whatsapp/phone-number-id" in text
    assert "FAIL social-messaging resource=*" in text
    assert "phone-id match" not in text


def test_a_different_phone_id_in_the_policy_is_a_mismatch() -> None:
    code, text = _run(_Session(arn_id="other-id"))
    assert code == 1
    assert "FAIL phone-id mismatch" in text
    assert "other-id" not in text
    assert _BARE not in text


def test_refuses_main(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOW_MAIN", raising=False)
    with pytest.raises(SystemExit):
        main(["--stage", "main", "--profile", "p"], session_factory=_Session)
