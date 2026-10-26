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
from oerf.platform.channels.phone_id import _whatsapp_phone_number_id as _resolve_phone_number_id
from oerf.platform.channels.providers.base import MessageProvider
from oerf.platform.channels.registry import PhoneRegistry
from oerf.platform.channels.templates import (
    MissingFacts,
    MissingTemplate,
    parameter_values,
    render_message,
)
from oerf.platform.observability.logging import get_logger, log
from oerf.platform.sandbox.join import SANDBOX_TENANT, SandboxGate

logger = get_logger(__name__)
logger.setLevel(logging.INFO)

# Environment variables
ROUTING_TABLE_NAME = os.environ.get("ROUTING_TABLE_NAME", "")
SES_SOURCE_EMAIL = os.environ.get("SES_SOURCE_EMAIL", "")
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
WA_PHONE_NUMBER_ID_PARAM = os.environ.get("WA_PHONE_NUMBER_ID_PARAM", "").strip()
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


def _whatsapp_phone_number_id() -> str:
    """Return the live WhatsApp phone-number id, resolved once per process."""

    return _resolve_phone_number_id(
        WA_PHONE_NUMBER_ID_PARAM,
        WHATSAPP_PHONE_NUMBER_ID,
        logger,
        channel="WHATSAPP",
    )


def _required_text(value: Any) -> str | None:
    """Return a stripped string, or None when the value is missing or blank."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


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


def send_whatsapp(
    recipient: str,
    message: str,
    template_id: str | None = None,
    parameters: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Send WhatsApp via AWS End User Messaging Social, simulating if configured.

    A mocked send returns a synthetic id and does not call the API. A live
    send with a template id uses ``oerf_<logical_name>`` and the parameters in
    template order. Session text remains for callers that have no template.
    """
    msg_id = f"wa-{uuid.uuid4().hex[:12]}"
    if USE_MOCK_WHATSAPP:
        log(logger, logging.INFO, "send_simulated", channel="WHATSAPP", outcome="simulated")
        return {
            "success": False,
            "message_id": msg_id,
            "channel": "WHATSAPP",
            "status": "SIMULATED",
        }

    phone_number_id = _whatsapp_phone_number_id()
    if not phone_number_id:
        log(
            logger,
            logging.WARNING,
            "wa_phone_id_missing",
            channel="WHATSAPP",
            outcome="error",
            count=1,
        )
        return {
            "success": False,
            "error": "wa_phone_id_missing",
            "channel": "WHATSAPP",
        }

    # Live integration via AWS End User Messaging (Social Messaging)
    try:
        eum = boto3.client("socialmessaging")
        payload = _whatsapp_payload(recipient, message, template_id, parameters)
        resp = eum.send_whatsapp_message(
            originationPhoneNumberId=phone_number_id,
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
        log(logger, logging.ERROR, "send_failed", channel="WHATSAPP", outcome="error")
        return {"success": False, "error": str(exc), "channel": "WHATSAPP"}


def _whatsapp_payload(
    recipient: str,
    message: str,
    template_id: str | None,
    parameters: tuple[str, ...],
) -> dict[str, Any]:
    """A Utility template when the rung names one, otherwise session text."""

    if not template_id:
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "text",
            "text": {"preview_url": False, "body": message},
        }
    name = template_id if template_id.startswith("oerf_") else f"oerf_{template_id}"
    template: dict[str, Any] = {"name": name, "language": {"code": "en"}}
    if parameters:
        template["components"] = [
            {
                "type": "body",
                "parameters": [{"type": "text", "text": str(value)} for value in parameters],
            }
        ]
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "template",
        "template": template,
    }


def send_sms(recipient: str, message: str) -> dict[str, Any]:
    """Send SMS message via Amazon SNS or simulate if explicitly configured."""
    msg_id = f"sms-{uuid.uuid4().hex[:12]}"
    if USE_MOCK_SMS:
        log(logger, logging.INFO, "send_simulated", channel="SMS", outcome="simulated")
        return {"success": False, "message_id": msg_id, "channel": "SMS", "status": "SIMULATED"}

    try:
        sns = boto3.client("sns")
        resp = sns.publish(PhoneNumber=recipient, Message=message)
        actual_msg_id = resp.get("MessageId") or msg_id
        return {"success": True, "message_id": actual_msg_id, "channel": "SMS", "status": "SENT"}
    except Exception as exc:
        log(logger, logging.ERROR, "send_failed", channel="SMS", outcome="error")
        return {"success": False, "error": str(exc), "channel": "SMS"}


def send_email(recipient: str, subject: str, body: str) -> dict[str, Any]:
    """Send email via Amazon SESv2, simulating only when explicitly configured."""
    msg_id = f"email-{uuid.uuid4().hex[:12]}"
    if USE_MOCK_EMAIL:
        log(logger, logging.INFO, "send_simulated", channel="EMAIL", outcome="simulated")
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
        log(logger, logging.ERROR, "send_failed", channel="EMAIL", outcome="error")
        return {"success": False, "error": str(exc), "channel": "EMAIL"}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point for channel router.

    An engagement rung stores its task token on ``ENG#`` and sends through
    ``send_rung``. The legacy payload below does not.
    
    Expected event payload:
    {
        "tenant_id": "T1",
        "incident_id": "INC-001",
        "recipient_role": "driver",
        "recipient_phone": "+910000000101",
        "recipient_email": "driver@example.com",
        "message": "Cold-chain alert: temperature exceeded limit.",
        "subject": "OERF Alert: Excursion Detected",
        "task_token": "<stepfunctions-token>",
        "channel_priority": ["WHATSAPP", "SMS", "EMAIL"]
    }
    """
    if event.get("step_name") == "EngagementSendViaRouter":
        from oerf.platform.channels.engagement_send import send_engagement

        return send_engagement(event)
    tenant_id = _required_text(event.get("tenant_id"))
    incident_id = event.get("incident_id", "unknown")
    if tenant_id is None:
        logger.error("Rejected route: tenant_required")
        return {"success": False, "error": "tenant_required", "attempts": []}
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

    if result.get("simulated"):
        outcome = "simulated"
    elif result.get("success"):
        outcome = "sent"
    else:
        outcome = "not_sent"
    channel = result.get("delivered_channel") or result.get("channel") or ""
    if not isinstance(channel, str) or not channel.strip():
        channel = "none" if outcome in ("error", "not_sent") else ""
    log(
        logger,
        logging.INFO,
        "route_complete",
        tenant_id=tenant_id,
        incident_id=incident_id if isinstance(incident_id, str) else None,
        channel=channel,
        outcome=outcome,
        count=len(result["attempts"]),
    )
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
        parameters = (
            tuple(parameter_values(messages, request.template_id, channel, request.facts))
            if channel == "whatsapp"
            else ()
        )
        sent = provider.send(recipient, body, request.template_id, parameters=parameters)
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
            if sent.provider_msg_id:
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
                delivered=False,
                channel=channel,
                provider_msg_id=sent.provider_msg_id,
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
