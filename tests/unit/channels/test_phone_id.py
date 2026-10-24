"""Shared WhatsApp phone-number id reader."""

from __future__ import annotations

import logging

from oerf.platform.channels import phone_id

_PARAM = "/oerf/whatsapp/phone-number-id"


def test_parameter_is_read_once_and_cached(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class _SSM:
        def get_parameter(self, **kwargs: object) -> dict[str, dict[str, str]]:
            calls.append(kwargs)
            return {"Parameter": {"Value": "  from-ssm  "}}

    monkeypatch.setattr(phone_id, "_cached_phone_number_id", phone_id._PHONE_UNSET)
    monkeypatch.setattr(phone_id, "_ssm_client", lambda: _SSM())
    logger = logging.getLogger("oerf.platform.channels.phone_id")

    first = phone_id._whatsapp_phone_number_id(_PARAM, "from-env", logger)
    second = phone_id._whatsapp_phone_number_id(_PARAM, "from-env", logger)

    assert first == "from-ssm"
    assert second == "from-ssm"
    assert calls == [{"Name": _PARAM, "WithDecryption": True}]


def test_failed_read_is_not_cached_so_the_next_read_can_succeed(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class _SSM:
        def get_parameter(self, **kwargs: object) -> dict[str, dict[str, str]]:
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("ssm-down")
            return {"Parameter": {"Value": "from-ssm"}}

    monkeypatch.setattr(phone_id, "_cached_phone_number_id", phone_id._PHONE_UNSET)
    monkeypatch.setattr(phone_id, "_ssm_client", lambda: _SSM())
    logger = logging.getLogger("oerf.platform.channels.phone_id")

    first = phone_id._whatsapp_phone_number_id(_PARAM, "from-env", logger)
    second = phone_id._whatsapp_phone_number_id(_PARAM, "from-env", logger)
    third = phone_id._whatsapp_phone_number_id(_PARAM, "from-env", logger)

    assert first == ""
    assert second == "from-ssm"
    assert third == "from-ssm"
    assert calls == [
        {"Name": _PARAM, "WithDecryption": True},
        {"Name": _PARAM, "WithDecryption": True},
    ]
