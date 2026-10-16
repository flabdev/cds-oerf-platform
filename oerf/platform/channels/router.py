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
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Environment variables
ROUTING_TABLE_NAME = os.environ.get("ROUTING_TABLE_NAME", "")
SES_SOURCE_EMAIL = os.environ.get("SES_SOURCE_EMAIL", "")
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
USE_MOCK_WHATSAPP = os.environ.get("USE_MOCK_WHATSAPP", "false").lower() == "true"
USE_MOCK_SMS = os.environ.get("USE_MOCK_SMS", "false").lower() == "true"

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
    task_token: Optional[str] = None,
) -> None:
    """Record outbound message in RoutingTable for audit and callback correlation."""
    if not ROUTING_TABLE_NAME:
        logger.warning("ROUTING_TABLE_NAME not set, skipping routing record")
        return

    table = get_dynamodb().Table(ROUTING_TABLE_NAME)
    now = int(time.time())
    ttl = now + (7 * 86400)  # 7-day TTL

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
        "ttl": ttl,
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
                "ttl": ttl,
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
                "ttl": ttl,
            }
        )

    table.put_item(Item=item)


def send_whatsapp(recipient: str, message: str) -> Dict[str, Any]:
    """Send WhatsApp message via AWS End User Messaging Social or simulate if explicitly configured."""
    msg_id = f"wa-{uuid.uuid4().hex[:12]}"
    if USE_MOCK_WHATSAPP:
        logger.info("[SIMULATED WHATSAPP] To: %s | Message: %s", recipient, message)
        return {"success": True, "message_id": msg_id, "channel": "WHATSAPP", "status": "SIMULATED"}

    if not WHATSAPP_PHONE_NUMBER_ID:
        logger.warning("WHATSAPP_PHONE_NUMBER_ID not configured, falling back to next notification channel")
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
        return {"success": True, "message_id": actual_msg_id, "channel": "WHATSAPP", "status": "SENT"}
    except Exception as exc:
        logger.error("Failed to send WhatsApp message via socialmessaging: %s", exc)
        return {"success": False, "error": str(exc), "channel": "WHATSAPP"}


def send_sms(recipient: str, message: str) -> Dict[str, Any]:
    """Send SMS message via Amazon SNS or simulate if explicitly configured."""
    msg_id = f"sms-{uuid.uuid4().hex[:12]}"
    if USE_MOCK_SMS:
        logger.info("[SIMULATED SMS] To: %s | Message: %s", recipient, message)
        return {"success": True, "message_id": msg_id, "channel": "SMS", "status": "SIMULATED"}

    try:
        sns = boto3.client("sns")
        resp = sns.publish(PhoneNumber=recipient, Message=message)
        actual_msg_id = resp.get("MessageId") or msg_id
        return {"success": True, "message_id": actual_msg_id, "channel": "SMS", "status": "SENT"}
    except Exception as exc:
        logger.error("Failed to send SMS via SNS: %s", exc)
        return {"success": False, "error": str(exc), "channel": "SMS"}


def send_email(recipient: str, subject: str, body: str) -> Dict[str, Any]:
    """Send email via Amazon SESv2."""
    msg_id = f"email-{uuid.uuid4().hex[:12]}"
    if not SES_SOURCE_EMAIL:
        logger.info("[SIMULATED EMAIL] To: %s | Subject: %s", recipient, subject)
        return {"success": True, "message_id": msg_id, "channel": "EMAIL", "status": "SIMULATED"}

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


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
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
    logger.info("Routing notification: %s", json.dumps({k: v for k, v in event.items() if k != "task_token"}))

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

        if res.get("success"):
            result["success"] = True
            result["delivered_channel"] = ch_upper
            result["message_id"] = res.get("message_id")

            record_outbound_message(
                tenant_id=tenant_id,
                incident_id=incident_id,
                message_id=res["message_id"],
                channel=ch_upper,
                recipient=recipient_phone if ch_upper in ("WHATSAPP", "SMS") else (recipient_email or ""),
                status=res.get("status", "SENT"),
                task_token=task_token,
            )
            break

    return result
