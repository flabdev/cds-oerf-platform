"""Point one WABA event destination at the inbound topic.

Delete sends an empty ``eventDestinations`` list. The API allows 0 or 1
items, and 0 clears the destination.
"""

from __future__ import annotations

from typing import Any

import boto3

_client = None


def social_messaging():
    """Return the End User Messaging Social client. Tests replace this."""
    global _client
    if _client is None:
        _client = boto3.client("socialmessaging")
    return _client


def on_event(event: dict[str, Any], _context: object) -> dict[str, str]:
    """Create, update, or clear the WABA event destination."""
    props = event.get("ResourceProperties") or {}
    waba_id = str(props.get("WabaId") or "")
    request_type = event.get("RequestType")
    if request_type == "Delete":
        physical = str(event.get("PhysicalResourceId") or waba_id)
        _put(physical, [])
        return {"PhysicalResourceId": physical}

    topic_arn = str(props.get("TopicArn") or "")
    if not waba_id.startswith("waba-") or not topic_arn:
        raise ValueError("WabaId and TopicArn are required")
    _put(waba_id, [{"eventDestinationArn": topic_arn}])
    return {"PhysicalResourceId": waba_id}


def _put(waba_id: str, destinations: list[dict[str, str]]) -> None:
    social_messaging().put_whatsapp_business_account_event_destinations(
        id=waba_id,
        eventDestinations=destinations,
    )
