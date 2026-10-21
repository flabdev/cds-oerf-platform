"""WABA event-destination handler. No AWS calls."""

from __future__ import annotations

import boto3
from botocore.stub import Stubber

from infra.custom_resources import waba_destination

_WABA = "waba-01234567890123456789012345678901"
_TOPIC = "arn:aws:sns:us-east-1:123456789012:oerf-inbound"
_PUT = "put_whatsapp_business_account_event_destinations"


def _event(request_type: str, physical: str | None = None) -> dict:
    event = {
        "RequestType": request_type,
        "ResourceProperties": {"WabaId": _WABA, "TopicArn": _TOPIC},
    }
    if physical is not None:
        event["PhysicalResourceId"] = physical
    return event


def _stub(monkeypatch, destinations: list[dict[str, str]], waba_id: str = _WABA):
    for name in (
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    client = boto3.client("socialmessaging", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response(
        _PUT,
        {},
        expected_params={"id": waba_id, "eventDestinations": destinations},
    )
    stubber.activate()
    monkeypatch.setattr(waba_destination, "_client", client)
    return stubber


def test_create_sets_the_topic_and_returns_the_waba_id(monkeypatch) -> None:
    stubber = _stub(monkeypatch, [{"eventDestinationArn": _TOPIC}])

    result = waba_destination.on_event(_event("Create"), None)

    assert result == {"PhysicalResourceId": _WABA}
    stubber.assert_no_pending_responses()


def test_update_replaces_the_destination(monkeypatch) -> None:
    updated = "arn:aws:sns:us-east-1:123456789012:oerf-inbound-2"
    stubber = _stub(monkeypatch, [{"eventDestinationArn": updated}])
    event = _event("Update", physical=_WABA)
    event["ResourceProperties"]["TopicArn"] = updated

    result = waba_destination.on_event(event, None)

    assert result == {"PhysicalResourceId": _WABA}
    stubber.assert_no_pending_responses()


def test_delete_clears_destinations_for_the_physical_id(monkeypatch) -> None:
    stubber = _stub(monkeypatch, [], waba_id=_WABA)

    result = waba_destination.on_event(_event("Delete", physical=_WABA), None)

    assert result == {"PhysicalResourceId": _WABA}
    stubber.assert_no_pending_responses()
