"""The deployed router handler fails closed when the event has no tenant."""

from oerf.platform.channels import router


def test_missing_tenant_does_not_send_or_write(monkeypatch) -> None:
    monkeypatch.setattr(router, "USE_MOCK_WHATSAPP", True)
    monkeypatch.setattr(router, "ROUTING_TABLE_NAME", "routing")
    monkeypatch.setattr(
        router,
        "send_whatsapp",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("whatsapp")),
    )
    monkeypatch.setattr(
        router,
        "get_dynamodb",
        lambda: (_ for _ in ()).throw(AssertionError("dynamodb")),
    )

    result = router.handler(
        {
            "incident_id": "INC-1",
            "recipient_phone": "+910000000101",
            "message": "pull over",
            "channel_priority": ["WHATSAPP"],
        },
        None,
    )

    assert result == {"success": False, "error": "tenant_required", "attempts": []}


def test_blank_tenant_does_not_send_or_write(monkeypatch) -> None:
    monkeypatch.setattr(
        router,
        "send_sms",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("sms")),
    )
    monkeypatch.setattr(
        router,
        "get_dynamodb",
        lambda: (_ for _ in ()).throw(AssertionError("dynamodb")),
    )

    result = router.handler(
        {
            "tenant_id": "  ",
            "recipient_phone": "+910000000101",
            "message": "pull over",
            "channel_priority": ["SMS"],
        },
        None,
    )

    assert result["error"] == "tenant_required"
    assert result["attempts"] == []
