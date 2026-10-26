"""Post one WhatsApp reply onto the inbound queue.

Operator tool. It never runs in Lambda. It never calls ``SendTaskSuccess``.
The body is a provider message: ``channel`` is ``whatsapp``, and there is no
``action`` and no ``action_id``.

    python -m scripts.demo_reply --profile cds-oerf-poc --tenant t1 \\
        --shipment PH-2841 --role driver --button running

The address comes from :func:`resolve_party_address`. Mocked channels use the
contact registry. Live channels use ``/oerf/demo-numbers/{tenant}/{role}``.
The command prints the shipment and the message id. It does not print the number.
"""

from __future__ import annotations

import argparse
import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oerf.platform.channels.address import AddressError, channels_are_live, resolve_party_address

REGION = "us-east-1"
DATA_STACK = "OerfDataStack"
COMPUTE_STACK = "OerfComputeStack"
ROUTER_FUNCTION = "oerf-channel-router"
TEMPLATE_NAME = "oerf_driver_checkin"
_TEMPLATES = Path(__file__).resolve().parent / "whatsapp" / "templates.json"


def button_labels() -> dict[str, str]:
    """Slug to the quick-reply label on ``oerf_driver_checkin``."""

    document = json.loads(_TEMPLATES.read_text(encoding="utf-8"))
    templates = document.get("templates")
    if not isinstance(templates, list):
        raise SystemExit("whatsapp templates have no template list")
    match = None
    for item in templates:
        if isinstance(item, dict) and item.get("name") == TEMPLATE_NAME:
            match = item
            break
    if match is None:
        raise SystemExit(f"{TEMPLATE_NAME} is not in the WhatsApp template file")
    buttons = match.get("buttons")
    if not isinstance(buttons, list):
        raise SystemExit(f"{TEMPLATE_NAME} has no buttons")
    labels: dict[str, str] = {}
    for button in buttons:
        if not isinstance(button, dict):
            continue
        text = button.get("text")
        if isinstance(text, str) and text.strip():
            labels[_slug(text)] = text.strip()
    if not labels:
        raise SystemExit(f"{TEMPLATE_NAME} has no button labels")
    return labels


def channels_live_from_configuration(variables: Mapping[str, str] | None) -> bool:
    """True when the deployed router has WhatsApp mock mode off.

    A missing flag is a failure. The laptop environment is not the source.
    """

    if not isinstance(variables, Mapping) or "USE_MOCK_WHATSAPP" not in variables:
        raise SystemExit(f"cannot read USE_MOCK_WHATSAPP on {ROUTER_FUNCTION}")
    return channels_are_live(str(variables["USE_MOCK_WHATSAPP"]))


def provider_message(
    *,
    sender: str,
    text: str,
    received_at: str,
    provider_msg_id: str | None = None,
) -> dict[str, Any]:
    """One inbound provider body. It cannot take the dashboard branch."""

    message_id = provider_msg_id or f"wa-demo-{uuid.uuid4().hex}"
    body = {
        "channel": "whatsapp",
        "provider_msg_id": message_id,
        "sender": sender,
        "received_at": received_at,
        "text": text,
        "type": "text",
        "media": [],
    }
    if body["channel"] == "dashboard" or "action" in body or "action_id" in body:
        raise RuntimeError("demo_reply cannot queue a dashboard action")
    return body


def queued_summary(
    message: Mapping[str, Any], *, tenant_id: str, shipment_id: str, role: str
) -> str:
    """Ids only. The sender stays out of the operator output."""

    return (
        f"queued tenant={tenant_id} shipment={shipment_id} role={role} "
        f"provider_msg_id={message['provider_msg_id']}"
    )


def _slug(label: str) -> str:
    folded = " ".join(label.strip().casefold().replace("_", " ").split())
    return folded.replace(" ", "_")


def _reply_text(button: str | None, text: str | None) -> str:
    if bool(button) == bool(text):
        raise SystemExit("pass exactly one of --button or --text")
    if text is not None:
        stripped = text.strip()
        if not stripped:
            raise SystemExit("--text is empty")
        return stripped
    labels = button_labels()
    assert button is not None
    found = labels.get(button.strip())
    if found is None:
        known = ", ".join(sorted(labels))
        raise SystemExit(f"unknown button {button!r}; known ids: {known}")
    return found


def _outputs(cloudformation: Any, stack_name: str) -> dict[str, str]:
    response = cloudformation.describe_stacks(StackName=stack_name)
    return {
        item["OutputKey"]: item["OutputValue"]
        for item in response["Stacks"][0].get("Outputs") or []
        if item.get("OutputKey") and item.get("OutputValue")
    }


def _contact_id(item: Mapping[str, Any], role: str) -> str:
    attribute = item.get(role)
    value = attribute.get("S") if isinstance(attribute, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"shipment has no contact for role {role}")
    return value.strip()


class _Parameters:
    def __init__(self, client: Any) -> None:
        self._client = client

    def get(self, name: str) -> str:
        found = self._client.get_parameter(Name=name, WithDecryption=True)
        parameter = found.get("Parameter") if isinstance(found, dict) else None
        value = parameter.get("Value") if isinstance(parameter, dict) else None
        return value if isinstance(value, str) else ""


def _address(
    *,
    tenant_id: str,
    role: str,
    contact_id: str,
    live: bool,
    registry: Any,
    parameters: Any,
) -> str:
    try:
        return resolve_party_address(
            tenant_id,
            role,
            contact_id,
            registry=registry,
            parameters=parameters,
            live=live,
        )
    except AddressError as exc:
        raise SystemExit(str(exc)) from exc
    except Exception:
        raise SystemExit("could not resolve the party address") from None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Queue one WhatsApp reply for a demo shipment.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--shipment", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--button")
    parser.add_argument("--text")
    args = parser.parse_args(argv)
    text = _reply_text(args.button, args.text)

    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    lambdas = session.client("lambda")
    try:
        configuration = lambdas.get_function_configuration(FunctionName=ROUTER_FUNCTION)
    except Exception:
        raise SystemExit(f"cannot read USE_MOCK_WHATSAPP on {ROUTER_FUNCTION}") from None
    variables = configuration.get("Environment", {}).get("Variables")
    live = channels_live_from_configuration(variables)

    cloudformation = session.client("cloudformation")
    data = _outputs(cloudformation, DATA_STACK)
    compute = _outputs(cloudformation, COMPUTE_STACK)
    tenant_table = data.get("TenantTableName")
    routing_table = data.get("RoutingTableName")
    queue_url = compute.get("InboundQueueUrl")
    if not tenant_table or not routing_table or not queue_url:
        raise SystemExit("stack outputs are missing the tenant table, routing table, or queue url")

    dynamodb = session.client("dynamodb")
    shipment = dynamodb.get_item(
        TableName=tenant_table,
        Key={
            "pk": {"S": f"TENANT#{args.tenant}"},
            "sk": {"S": f"SHIPMENT#{args.shipment}"},
        },
    ).get("Item")
    if not isinstance(shipment, dict):
        raise SystemExit(f"shipment {args.shipment} was not found")
    contact_id = _contact_id(shipment, args.role)

    if live:
        registry = None
        parameters = _Parameters(session.client("ssm"))
    else:
        from oerf.platform.channels.registry import DynamoPhoneRegistry

        parameters = None
        registry = DynamoPhoneRegistry(dynamodb, routing_table, tenant_table)
    sender = _address(
        tenant_id=args.tenant,
        role=args.role,
        contact_id=contact_id,
        live=live,
        registry=registry,
        parameters=parameters,
    )
    received_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    message = provider_message(sender=sender, text=text, received_at=received_at)
    session.client("sqs").send_message(QueueUrl=queue_url, MessageBody=json.dumps(message))
    print(queued_summary(message, tenant_id=args.tenant, shipment_id=args.shipment, role=args.role))


if __name__ == "__main__":
    main()
