"""Single Channel Router Lambda (ADR-008).

Routes outbound notifications with cascade fallback:
WhatsApp (or RCS) -> SMS -> Voice -> Email.
Supports simulated delivery during development when phone numbers/WABA
are in sandbox/approval, with seamless flip to real AWS End User Messaging.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict, Field

from oerf.platform.channels.engagement import EngagementStore
from oerf.platform.channels.providers.base import MessageProvider
from oerf.platform.channels.registry import PhoneRegistry
from oerf.platform.channels.templates import MissingFacts, MissingTemplate, render_message
from oerf.platform.sandbox.join import SANDBOX_TENANT, SandboxGate

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Environment variables
ROUTING_TABLE_NAME = os.environ.get("ROUTING_TABLE_NAME", "")
SES_SOURCE_EMAIL = os.environ.get("SES_SOURCE_EMAIL", "")
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
USE_MOCK_WHATSAPP = os.environ.get("USE_MOCK_WHATSAPP", "false").lower() == "true"
USE_MOCK_SMS = os.environ.get("USE_MOCK_SMS", "false").lower() == "true"
USE_MOCK_EMAIL = os.environ.get("USE_MOCK_EMAIL", "false").lower() == "true"

# Lazy-loaded AWS clients
_dynamodb = None
_ses = None


def get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def get_ses():
    global _ses
    if _ses is None:
        _ses = boto3.client("sesv2")
    return _ses


def record_outbound_message(
    tenant_id: str,
    incident_id: str,
    message_id: str,
    channel: str,
    recipient: str,
    status: str,
    task_token: str | None = None,
) -> None:
    """Record outbound message in RoutingTable for audit and callback correlation."""
    if not ROUTING_TABLE_NAME:
        logger.warning("ROUTING_TABLE_NAME not set, skipping routing record")
        return

    table = get_dynamodb().Table(ROUTING_TABLE_NAME)
    now = int(time.time())
    expires_at = now + (7 * 86400)  # 7-day retention

    item = {
        "pk": f"TENANT#{tenant_id}#INCIDENT#{incident_id}",
        "sk": f"MSG#{message_id}",
        "tenant_id": tenant_id,
        "incident_id": incident_id,
        "message_id": message_id,
        "channel": channel,
        "recipient": recipient,
        "status": status,
        "created_at": now,
        "expires_at": expires_at,
    }
    if task_token:
        item["task_token"] = task_token

        # Store active task token under the canonical ACTIVE_TASK_TOKEN key
        # so that action_handler can retrieve it to resume Step Functions
        table.put_item(
            Item={
                "pk": f"TENANT#{tenant_id}#INCIDENT#{incident_id}",
                "sk": "ACTIVE_TASK_TOKEN",
                "task_token": task_token,
                "incident_id": incident_id,
                "tenant_id": tenant_id,
                "channel": channel,
                "message_id": message_id,
                "created_at": now,
                "expires_at": expires_at,
            }
        )

        # Also store reverse lookup by message_id for inbound webhooks
        table.put_item(
            Item={
                "pk": f"MSG_CORRELATION#{message_id}",
                "sk": "TOKEN",
                "task_token": task_token,
                "incident_id": incident_id,
                "tenant_id": tenant_id,
                "expires_at": expires_at,
            }
        )

    table.put_item(Item=item)


def send_whatsapp(recipient: str, message: str) -> dict[str, Any]:
    """Send WhatsApp via AWS End User Messaging Social, simulating if configured."""
    msg_id = f"wa-{uuid.uuid4().hex[:12]}"
    if USE_MOCK_WHATSAPP:
        logger.info("[SIMULATED WHATSAPP] To: %s | Message: %s", recipient, message)
        return {
            "success": False,
            "message_id": msg_id,
            "channel": "WHATSAPP",
            "status": "SIMULATED",
        }

    if not WHATSAPP_PHONE_NUMBER_ID:
        logger.warning(
            "WHATSAPP_PHONE_NUMBER_ID not configured, falling back to next channel"
        )
        return {
            "success": False,
            "error": "WHATSAPP_PHONE_NUMBER_ID not configured",
            "channel": "WHATSAPP",
        }

    # Live integration via AWS End User Messaging (Social Messaging)
    try:
        eum = boto3.client("socialmessaging")
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "text",
            "text": {"preview_url": False, "body": message},
        }
        resp = eum.send_whatsapp_message(
            originationPhoneNumberId=WHATSAPP_PHONE_NUMBER_ID,
            metaApiVersion="v20.0",
            message=json.dumps(payload).encode("utf-8"),
        )
        actual_msg_id = resp.get("messageId") or msg_id
        return {
            "success": True,
            "message_id": actual_msg_id,
            "channel": "WHATSAPP",
            "status": "SENT",
        }
    except Exception as exc:
        logger.error("Failed to send WhatsApp message via socialmessaging: %s", exc)
        return {"success": False, "error": str(exc), "channel": "WHATSAPP"}


def send_sms(recipient: str, message: str) -> dict[str, Any]:
    """Send SMS message via Amazon SNS or simulate if explicitly configured."""
    msg_id = f"sms-{uuid.uuid4().hex[:12]}"
    if USE_MOCK_SMS:
        logger.info("[SIMULATED SMS] To: %s | Message: %s", recipient, message)
        return {"success": False, "message_id": msg_id, "channel": "SMS", "status": "SIMULATED"}

    try:
        sns = boto3.client("sns")
        resp = sns.publish(PhoneNumber=recipient, Message=message)
        actual_msg_id = resp.get("MessageId") or msg_id
        return {"success": True, "message_id": actual_msg_id, "channel": "SMS", "status": "SENT"}
    except Exception as exc:
        logger.error("Failed to send SMS via SNS: %s", exc)
        return {"success": False, "error": str(exc), "channel": "SMS"}


def send_email(recipient: str, subject: str, body: str) -> dict[str, Any]:
    """Send email via Amazon SESv2, simulating only when explicitly configured."""
    msg_id = f"email-{uuid.uuid4().hex[:12]}"
    if USE_MOCK_EMAIL:
        logger.info("[SIMULATED EMAIL] To: %s | Subject: %s", recipient, subject)
        return {
            "success": False,
            "message_id": msg_id,
            "channel": "EMAIL",
            "status": "SIMULATED",
        }

    if not SES_SOURCE_EMAIL:
        # Never report a delivery the router cannot actually perform.
        logger.warning("SES_SOURCE_EMAIL not configured, email cannot be delivered")
        return {
            "success": False,
            "error": "SES_SOURCE_EMAIL not configured",
            "channel": "EMAIL",
        }

    try:
        ses = get_ses()
        ses.send_email(
            FromEmailAddress=SES_SOURCE_EMAIL,
            Destination={"ToAddresses": [recipient]},
            Content={
                "Simple": {
                    "Subject": {"Data": subject},
                    "Body": {"Text": {"Data": body}},
                }
            },
        )
        return {"success": True, "message_id": msg_id, "channel": "EMAIL", "status": "SENT"}
    except ClientError as exc:
        logger.error("SES send failed: %s", exc)
        return {"success": False, "error": str(exc), "channel": "EMAIL"}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point for channel router.
    
    Expected event payload:
    {
        "tenant_id": "T1",
        "incident_id": "INC-001",
        "recipient_role": "driver",
        "recipient_phone": "+1234567890",
        "recipient_email": "driver@example.com",
        "message": "Cold-chain alert: temperature exceeded limit.",
        "subject": "OERF Alert: Excursion Detected",
        "task_token": "<stepfunctions-token>",
        "channel_priority": ["WHATSAPP", "SMS", "EMAIL"]
    }
    """
    logged_event = {key: value for key, value in event.items() if key != "task_token"}
    logger.info("Routing notification: %s", json.dumps(logged_event))

    tenant_id = event.get("tenant_id", "default")
    incident_id = event.get("incident_id", "unknown")
    recipient_phone = event.get("recipient_phone")
    recipient_email = event.get("recipient_email")
    message = event.get("message", "")
    subject = event.get("subject", "OERF Alert")
    task_token = event.get("task_token")
    channel_priority = event.get("channel_priority", ["WHATSAPP", "SMS", "EMAIL"])

    result = {"success": False, "attempts": []}

    for channel in channel_priority:
        ch_upper = channel.upper()
        res = {"channel": ch_upper, "success": False}

        if ch_upper == "WHATSAPP" and recipient_phone:
            res = send_whatsapp(recipient_phone, message)
        elif ch_upper == "SMS" and recipient_phone:
            res = send_sms(recipient_phone, message)
        elif ch_upper == "EMAIL" and recipient_email:
            res = send_email(recipient_email, subject, message)

        result["attempts"].append(res)

        if res.get("status") == "SIMULATED":
            result["success"] = False
            result["simulated"] = True
            result["channel"] = ch_upper
            break

        if res.get("success"):
            result["success"] = True
            result["delivered_channel"] = ch_upper
            result["message_id"] = res.get("message_id")

            delivered_to = (
                recipient_phone if ch_upper in ("WHATSAPP", "SMS") else recipient_email
            )

            record_outbound_message(
                tenant_id=tenant_id,
                incident_id=incident_id,
                message_id=res["message_id"],
                channel=ch_upper,
                recipient=delivered_to or "",
                status=res.get("status", "SENT"),
                task_token=task_token,
            )
            break

    return result


DEFAULT_CHANNELS = ("whatsapp", "sms", "voice", "email")
_VOICE_REPLY = " Reply on WhatsApp."


class OutboundRequest(BaseModel):
    """One send from the cadence or workflow. Providers are not called directly."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    incident_id: str
    engagement_id: str
    role: str
    template_id: str
    facts: dict[str, Any] = Field(default_factory=dict)
    contact_id: str
    recipient: str
    email: str | None = None
    channels: tuple[str, ...] = DEFAULT_CHANNELS
    rcs_roles: tuple[str, ...] = ()
    inbound_rcs: bool = False


class DeliveryAttempt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: str
    delivered: bool
    provider_msg_id: str | None = None
    reason: str | None = None
    tenant_id: str
    incident_id: str
    engagement_id: str


class DeliveryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    delivered: bool
    channel: str | None = None
    provider_msg_id: str | None = None
    attempts: list[DeliveryAttempt]
    reason: str | None = None
    simulated: bool = False
    tenant_id: str
    incident_id: str
    engagement_id: str


def route_outbound(
    request: OutboundRequest,
    *,
    registry: PhoneRegistry,
    engagements: EngagementStore,
    providers: dict[str, MessageProvider],
    messages: dict[str, Any],
    sandbox: SandboxGate | None = None,
) -> DeliveryResult:
    """Validate opt-in, render a template, fall back across channels, then correlate."""

    if (
        sandbox is not None
        and request.tenant_id == SANDBOX_TENANT
        and not sandbox.channel_send_allowed()
    ):
        return _result(request, [], delivered=False, channel="dashboard", reason="simulated")

    attempts: list[DeliveryAttempt] = []
    if not _opted_in(registry, request.tenant_id, request.contact_id):
        return _result(request, attempts, reason="OPT_OUT")

    for channel in request.channels:
        if not _opted_in(registry, request.tenant_id, request.contact_id):
            return _result(request, attempts, reason="OPT_OUT")
        if channel == "rcs" and not _rcs_allowed(request):
            attempts.append(_attempt(request, channel, delivered=False, reason="rcs_unavailable"))
            continue
        try:
            body = render_message(messages, request.template_id, channel, request.facts)
        except MissingTemplate:
            attempts.append(_attempt(request, channel, delivered=False, reason="no_template"))
            continue
        except MissingFacts:
            raise
        if channel == "voice" and "WhatsApp" not in body:
            body = f"{body}{_VOICE_REPLY}"
        provider = providers.get(channel)
        if provider is None:
            attempts.append(
                _attempt(request, channel, delivered=False, reason="provider_unavailable")
            )
            continue
        recipient = request.email if channel == "email" else request.recipient
        if not recipient:
            attempts.append(_attempt(request, channel, delivered=False, reason="no_recipient"))
            continue
        sent = provider.send(recipient, body, request.template_id)
        attempts.append(
            _attempt(
                request,
                channel,
                delivered=sent.delivered,
                provider_msg_id=sent.provider_msg_id,
                reason=sent.reason,
            )
        )
        if sent.simulated:
            return _result(
                request,
                attempts,
                delivered=False,
                channel=channel,
                reason="simulated",
                simulated=True,
            )
        if not sent.delivered or not sent.provider_msg_id:
            continue
        engagement = engagements.get(
            request.tenant_id, request.incident_id, request.engagement_id
        )
        if engagement is None:
            raise ValueError(f"engagement {request.engagement_id!r} is not open")
        engagements.register_outbound(sent.provider_msg_id, engagement)
        engagements.register_address(channel, recipient, engagement)
        return _result(
            request,
            attempts,
            delivered=True,
            channel=channel,
            provider_msg_id=sent.provider_msg_id,
        )
    reason = attempts[-1].reason if attempts else "not_delivered"
    return _result(request, attempts, reason=reason)


def _rcs_allowed(request: OutboundRequest) -> bool:
    return request.inbound_rcs and request.role in request.rcs_roles


def _opted_in(registry: PhoneRegistry, tenant_id: str, contact_id: str) -> bool:
    contact = registry.get_contact(tenant_id, contact_id)
    return contact is not None and contact.opt_in


def _attempt(
    request: OutboundRequest,
    channel: str,
    *,
    delivered: bool,
    provider_msg_id: str | None = None,
    reason: str | None = None,
) -> DeliveryAttempt:
    return DeliveryAttempt(
        channel=channel,
        delivered=delivered,
        provider_msg_id=provider_msg_id,
        reason=reason,
        tenant_id=request.tenant_id,
        incident_id=request.incident_id,
        engagement_id=request.engagement_id,
    )


def _result(
    request: OutboundRequest,
    attempts: list[DeliveryAttempt],
    *,
    delivered: bool = False,
    channel: str | None = None,
    provider_msg_id: str | None = None,
    reason: str | None = None,
    simulated: bool = False,
) -> DeliveryResult:
    return DeliveryResult(
        delivered=delivered,
        channel=channel,
        provider_msg_id=provider_msg_id,
        attempts=attempts,
        reason=reason,
        simulated=simulated,
        tenant_id=request.tenant_id,
        incident_id=request.incident_id,
        engagement_id=request.engagement_id,
    )
