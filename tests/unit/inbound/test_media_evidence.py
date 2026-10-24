"""WhatsApp media through End User Messaging. Stubber stands in for the API."""

import hashlib
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, unquote, urlparse

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from oerf.platform.channels import phone_id
from oerf.platform.channels.engagement import MemoryEngagementStore
from oerf.platform.channels.registry import MemoryPhoneRegistry
from oerf.platform.inbound import handler as inbound
from oerf.platform.inbound.dedupe import InboundDedupe
from oerf.platform.inbound.handler import (
    MediaConfigError,
    MediaRejected,
    WhatsAppMediaEvidenceStore,
    configure_dependencies,
    handler,
    process_message,
)

NOW = datetime(2026, 10, 20, 10, 20, tzinfo=UTC)
DEADLINE = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
PHONE = "phone-1"
MEDIA_ID = "media-do-not-log"


@pytest.fixture(autouse=True)
def _reset_phone_cache():
    phone_id._cached_phone_number_id = phone_id._PHONE_UNSET
    phone_id._ssm = None
    yield
    phone_id._cached_phone_number_id = phone_id._PHONE_UNSET
    phone_id._ssm = None


class _Callbacks:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def send_task_success(self, *, taskToken: str, output: str) -> None:
        self.calls.append({"taskToken": taskToken, "output": output})


class _Social:
    def __init__(self) -> None:
        self.client = boto3.client(
            "socialmessaging",
            region_name="us-east-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )
        self.stubber = Stubber(self.client)
        self.calls: list[dict] = []

    def get_whatsapp_message_media(self, **kwargs):
        self.calls.append(kwargs)
        return self.client.get_whatsapp_message_media(**kwargs)


class _S3:
    def __init__(self) -> None:
        self.calls = 0

    def generate_presigned_url(self, *args, **kwargs):
        del args, kwargs
        self.calls += 1
        raise AssertionError("presign")


def _signer():
    return boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        config=Config(signature_version="s3v4"),
    )


def _store(social: _Social, s3) -> WhatsAppMediaEvidenceStore:
    return WhatsAppMediaEvidenceStore("evidence", social=social, s3=s3)


def _prefix(tenant: str = "tenant-a", message_id: str = "incoming-1") -> str:
    digest = hashlib.sha256(message_id.encode("utf-8")).hexdigest()
    return f"tenants/{tenant}/inbound/{digest}/"


def test_allowed_image_is_stored_through_a_presigned_put(monkeypatch) -> None:
    monkeypatch.setattr(inbound, "_whatsapp_phone_number_id", lambda: PHONE)
    social = _Social()
    social.stubber.add_response(
        "get_whatsapp_message_media",
        {"mimeType": "image/jpeg", "fileSize": 128},
    )
    social.stubber.add_response(
        "get_whatsapp_message_media",
        {"mimeType": "image/jpeg", "fileSize": 128},
    )
    social.stubber.activate()

    key = _store(social, _signer()).store("tenant-a", "incoming-1", MEDIA_ID, "image/gif")

    assert key.startswith(_prefix())
    assert key.endswith(".jpg")
    assert social.calls[0]["metadataOnly"] is True
    assert "destinationS3PresignedUrl" not in social.calls[0]
    assert social.calls[0]["originationPhoneNumberId"] == PHONE
    destination = social.calls[1]["destinationS3PresignedUrl"]
    assert destination["headers"] == {}
    url = destination["url"]
    assert _prefix() in unquote(url)
    query = parse_qs(urlparse(url).query)
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Expires"] == ["300"]
    assert "us-east-1" in query["X-Amz-Credential"][0]
    assert query["X-Amz-SignedHeaders"] == ["host"]
    assert "x-amz-server-side-encryption" not in url.lower()
    social.stubber.assert_no_pending_responses()


def test_disallowed_type_is_rejected_without_a_presign(monkeypatch, capsys) -> None:
    monkeypatch.setattr(inbound, "_whatsapp_phone_number_id", lambda: PHONE)
    social = _Social()
    social.stubber.add_response(
        "get_whatsapp_message_media",
        {"mimeType": "image/gif", "fileSize": 10},
    )
    social.stubber.activate()
    s3 = _S3()

    with pytest.raises(MediaRejected):
        _store(social, s3).store("tenant-a", "incoming-1", MEDIA_ID, "image/jpeg")

    assert s3.calls == 0
    assert len(social.calls) == 1
    text = capsys.readouterr().out
    assert "inbound media rejected" in text
    assert MEDIA_ID not in text


def test_oversize_file_uses_the_env_cap(monkeypatch) -> None:
    monkeypatch.setenv("INBOUND_MEDIA_MAX_BYTES", "1000")
    monkeypatch.setattr(inbound, "_whatsapp_phone_number_id", lambda: PHONE)
    social = _Social()
    social.stubber.add_response(
        "get_whatsapp_message_media",
        {"mimeType": "application/pdf", "fileSize": 1001},
    )
    social.stubber.activate()
    s3 = _S3()

    with pytest.raises(MediaRejected):
        _store(social, s3).store("tenant-a", "incoming-1", MEDIA_ID, None)

    assert s3.calls == 0
    assert len(social.calls) == 1


def test_invalid_media_id_is_rejected_without_retry(monkeypatch, capsys) -> None:
    monkeypatch.setattr(inbound, "_whatsapp_phone_number_id", lambda: PHONE)
    social = _Social()
    social.stubber.add_client_error(
        "get_whatsapp_message_media",
        service_error_code="ValidationException",
        service_message="bad",
        http_status_code=400,
    )
    social.stubber.activate()

    with pytest.raises(MediaRejected):
        _store(social, _S3()).store("tenant-a", "incoming-1", MEDIA_ID, None)

    assert len(social.calls) == 1
    assert MEDIA_ID not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("code", "status"),
    [("ThrottlingException", 429), ("InternalServerError", 500)],
)
def test_throttled_or_server_error_raises_for_retry(monkeypatch, code: str, status: int) -> None:
    monkeypatch.setattr(inbound, "_whatsapp_phone_number_id", lambda: PHONE)
    social = _Social()
    social.stubber.add_client_error(
        "get_whatsapp_message_media",
        service_error_code=code,
        service_message="later",
        http_status_code=status,
    )
    social.stubber.activate()

    with pytest.raises(ClientError):
        _store(social, _S3()).store("tenant-a", "incoming-1", MEDIA_ID, None)

    assert len(social.calls) == 1


def test_missing_phone_id_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(inbound, "_whatsapp_phone_number_id", lambda: "")
    social = _Social()
    s3 = _S3()

    with pytest.raises(MediaConfigError):
        _store(social, s3).store("tenant-a", "incoming-1", MEDIA_ID, None)

    assert social.calls == []
    assert s3.calls == 0


def test_phone_id_comes_from_ssm_and_does_not_fall_back(monkeypatch) -> None:
    monkeypatch.setattr(inbound, "WA_PHONE_NUMBER_ID_PARAM", "/oerf/whatsapp/phone-number-id")
    monkeypatch.setattr(inbound, "WHATSAPP_PHONE_NUMBER_ID", "env-should-not-win")
    seen: list[str] = []

    class _SSM:
        def get_parameter(self, **kwargs):
            seen.append(kwargs["Name"])
            assert kwargs["WithDecryption"] is True
            return {"Parameter": {"Value": "ssm-phone"}}

    monkeypatch.setattr(phone_id, "_ssm_client", lambda: _SSM())

    assert inbound._whatsapp_phone_number_id() == "ssm-phone"
    assert inbound._whatsapp_phone_number_id() == "ssm-phone"
    assert seen == ["/oerf/whatsapp/phone-number-id"]


def test_disallowed_media_is_acknowledged_without_a_media_callback(monkeypatch) -> None:
    monkeypatch.setattr(inbound, "_whatsapp_phone_number_id", lambda: PHONE)
    social = _Social()
    social.stubber.add_response(
        "get_whatsapp_message_media",
        {"mimeType": "image/gif", "fileSize": 10},
    )
    social.stubber.activate()
    deps, callbacks = _deps(_store(social, _S3()))
    body = _provider()
    body["media"] = [{"id": MEDIA_ID, "mime_type": "image/gif"}]
    configure_dependencies(deps)
    try:
        response = handler(
            {"Records": [{"messageId": "sqs-1", "body": json.dumps(body)}]},
            None,
        )
    finally:
        configure_dependencies(None)

    assert response == {"batchItemFailures": []}
    assert process_message(body, deps) == "duplicate"
    payload = json.loads(callbacks.calls[0]["output"])
    assert payload["text"] == "yes"
    assert payload["media"] == []
    assert MEDIA_ID not in callbacks.calls[0]["output"]


def test_throttled_media_call_is_retried_by_sqs(monkeypatch) -> None:
    monkeypatch.setattr(inbound, "_whatsapp_phone_number_id", lambda: PHONE)
    social = _Social()
    social.stubber.add_client_error(
        "get_whatsapp_message_media",
        service_error_code="ThrottlingException",
        service_message="later",
        http_status_code=429,
    )
    social.stubber.add_response(
        "get_whatsapp_message_media",
        {"mimeType": "image/png", "fileSize": 20},
    )
    social.stubber.add_response(
        "get_whatsapp_message_media",
        {"mimeType": "image/png", "fileSize": 20},
    )
    social.stubber.activate()
    deps, callbacks = _deps(_store(social, _signer()))
    body = _provider()
    body["media"] = [{"id": MEDIA_ID, "mime_type": "image/png"}]
    event = {"Records": [{"messageId": "sqs-1", "body": json.dumps(body)}]}
    configure_dependencies(deps)
    try:
        first = handler(event, None)
        assert first == {"batchItemFailures": [{"itemIdentifier": "sqs-1"}]}
        assert callbacks.calls == []
        second = handler(event, None)
    finally:
        configure_dependencies(None)

    assert second == {"batchItemFailures": []}
    payload = json.loads(callbacks.calls[0]["output"])
    assert payload["media"][0]["evidence_key"].startswith(_prefix("trusted-tenant"))
    assert payload["media"][0]["evidence_key"].endswith(".png")
    url = social.calls[-1]["destinationS3PresignedUrl"]["url"]
    assert "x-amz-server-side-encryption" not in url.lower()


def _deps(store: WhatsAppMediaEvidenceStore):
    engagements = MemoryEngagementStore(clock=lambda: NOW)
    registry = MemoryPhoneRegistry()
    registry.register_contact("trusted-tenant", "contact-1", "driver", "+15550000001")
    callbacks = _Callbacks()
    deps = inbound.InboundDependencies(
        InboundDedupe(clock=lambda: NOW),
        engagements,
        callbacks,
        registry,
        store,
    )
    engagements.register_outbound(
        "outbound-1",
        engagements.open_engagement(
            "trusted-tenant",
            "incident-1",
            "engagement-1",
            "driver",
            "confirm",
            "policy-1",
            "secret-task-token",
            {"shipment_id": "ship-1"},
            contact_id="contact-1",
            rung=1,
            deadline=DEADLINE,
        ),
    )
    return deps, callbacks


def _provider() -> dict:
    return {
        "provider": "whatsapp",
        "message_id": "incoming-1",
        "from": "+15550000001",
        "timestamp": "2026-10-20T10:20:00Z",
        "text": "yes",
        "context_msg_id": "outbound-1",
    }
