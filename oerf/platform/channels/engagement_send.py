"""One cadence send through ``send_rung``. The router handler is the only caller.

This module does not call a provider SDK. ``send_rung`` writes the task token
on ``ENG#`` before ``route_outbound``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from importlib.resources import files
from typing import Any

from oerf.platform.channels.address import resolve_party_address
from oerf.platform.channels.engagement_workflow import send_rung
from oerf.platform.channels.router import OutboundRequest

_ROLE = "role"


def send_engagement(event: dict[str, Any], *, ports: Any | None = None) -> dict[str, Any]:
    """Store the token, then send the rung. The return is not the task success."""

    workflow = event.get("workflow")
    token = event.get("task_token")
    if not isinstance(workflow, dict):
        raise ValueError("engagement send needs a workflow")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("engagement send needs a task token")
    built = ports if ports is not None else _ports()
    request, rung, deadline = _request(workflow, built)
    result = send_rung(
        request,
        ask=_text(workflow, "ask"),
        policy_id=_text(workflow, "policy_id"),
        task_token=token.strip(),
        rung=rung,
        deadline=deadline,
        engagements=built.engagements,
        registry=built.registry,
        providers=built.providers,
        messages=built.messages,
    )
    return {
        "delivered": result.delivered,
        "simulated": result.simulated,
        "provider_msg_id": result.provider_msg_id,
        "channel": result.channel,
        "engagement_id": result.engagement_id,
    }


def _request(workflow: dict[str, Any], ports: Any) -> tuple[OutboundRequest, int, datetime]:
    cadence = workflow.get("cadence")
    if not isinstance(cadence, dict):
        raise ValueError("engagement send needs cadence")
    role = _text(workflow, _ROLE)
    facts = workflow.get("facts") if isinstance(workflow.get("facts"), dict) else {}
    shipment = facts.get("shipment") if isinstance(facts.get("shipment"), dict) else {}
    contact_id = shipment.get(role)
    if not isinstance(contact_id, str) or not contact_id.strip():
        raise ValueError("the shipment has no contact for this role")
    recipient = resolve_party_address(
        _text(workflow, "tenant_id"),
        role,
        contact_id.strip(),
        registry=ports.registry,
        parameters=ports.parameters,
        live=not ports.mock_whatsapp,
    )
    message = cadence.get("message")
    channel = cadence.get("channel")
    rung = cadence.get("next_rung")
    deadline = cadence.get("deadline")
    if not isinstance(message, str) or not isinstance(channel, str):
        raise ValueError("engagement send needs cadence.message and cadence.channel")
    if not isinstance(rung, int) or not isinstance(deadline, str):
        raise ValueError("engagement send needs cadence.next_rung and cadence.deadline")
    return (
        OutboundRequest(
            tenant_id=_text(workflow, "tenant_id"),
            incident_id=_text(workflow, "incident_id"),
            engagement_id=_text(workflow, "engagement_id"),
            role=role,
            template_id=message,
            facts=_render_facts(facts),
            contact_id=contact_id.strip(),
            recipient=recipient,
            channels=(channel,),
        ),
        rung,
        datetime.fromisoformat(deadline.replace("Z", "+00:00")),
    )


def _render_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """Lift the template fields without dropping the gathered context."""

    rendered = dict(facts)
    shipment = facts.get("shipment") if isinstance(facts.get("shipment"), dict) else {}
    trigger = facts.get("trigger") if isinstance(facts.get("trigger"), dict) else {}
    if "shipment_id" in shipment:
        rendered["shipment_id"] = shipment["shipment_id"]
    if "temperature_c" in trigger:
        rendered["temperature_c"] = trigger["temperature_c"]
    return rendered


def _text(workflow: dict[str, Any], name: str) -> str:
    value = workflow.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"engagement send needs {name}")
    return value.strip()


def _ports() -> Any:
    import boto3

    from oerf.platform.channels import router as router_module
    from oerf.platform.channels.engagement import DynamoEngagementStore
    from oerf.platform.channels.providers.aws import (
        EmailProvider,
        SmsProvider,
        VoiceProvider,
        WhatsAppProvider,
    )
    from oerf.platform.channels.registry import DynamoPhoneRegistry

    incident = os.environ["INCIDENT_TABLE_NAME"]
    tenant = os.environ["TENANT_TABLE_NAME"]
    routing = os.environ["ROUTING_TABLE_NAME"]
    client = boto3.client("dynamodb")
    ssm = boto3.client("ssm")

    class _Parameters:
        def get(self, name: str) -> str:
            found = ssm.get_parameter(Name=name, WithDecryption=True)
            parameter = found.get("Parameter") if isinstance(found, dict) else None
            value = parameter.get("Value") if isinstance(parameter, dict) else None
            return value if isinstance(value, str) else ""

    class _Ports:
        engagements = DynamoEngagementStore(client, incident, routing)
        registry = DynamoPhoneRegistry(client, routing, tenant)
        providers = {
            "whatsapp": WhatsAppProvider(),
            "sms": SmsProvider(),
            "email": EmailProvider(),
            "voice": VoiceProvider(),
        }
        messages = json.loads(
            (files("domains") / "cold_chain_excursion" / "messages.json").read_text(
                encoding="utf-8"
            )
        )
        parameters = _Parameters()
        mock_whatsapp = router_module.USE_MOCK_WHATSAPP

    return _Ports()
