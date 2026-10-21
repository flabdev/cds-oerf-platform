"""Add Cognito ``custom:tenant_id`` without replacing the user pool.

``AWS::Cognito::UserPool`` ``Schema`` is create-only. Setting it on the
existing pool replaces ``OerfUserPool``. This handler calls
``AddCustomAttributes`` on the current pool id, and skips the call when the
attribute is already in the schema.
"""

from __future__ import annotations

from typing import Any

ATTRIBUTE_NAME = "tenant_id"
SCHEMA_NAME = "custom:tenant_id"
PHYSICAL_ID = "oerf-custom-tenant-id"


def attribute_definition() -> dict[str, Any]:
    """Schema fragment passed to ``AddCustomAttributes``."""
    return {
        "Name": ATTRIBUTE_NAME,
        "AttributeDataType": "String",
        "Mutable": True,
        "Required": False,
        "StringAttributeConstraints": {"MinLength": "1", "MaxLength": "128"},
    }


def ensure_tenant_id_attribute(pool_id: str, client: Any) -> bool:
    """Add the attribute when it is missing. Return whether a write happened."""
    described = client.describe_user_pool(UserPoolId=pool_id)
    schema = described.get("UserPool", {}).get("SchemaAttributes", [])
    if any(item.get("Name") == SCHEMA_NAME for item in schema):
        return False
    client.add_custom_attributes(
        UserPoolId=pool_id,
        CustomAttributes=[attribute_definition()],
    )
    return True


def on_event(event: dict[str, Any], client: Any) -> dict[str, str]:
    """Custom-resource handler. Delete does not remove the attribute."""
    physical_id = str(event.get("PhysicalResourceId") or PHYSICAL_ID)
    if event.get("RequestType") == "Delete":
        return {"PhysicalResourceId": physical_id}
    properties = event["ResourceProperties"]
    ensure_tenant_id_attribute(str(properties["UserPoolId"]), client)
    return {"PhysicalResourceId": physical_id}


def handler(event: dict[str, Any], _context: Any) -> dict[str, str]:
    """Lambda entry point. boto3 is imported here so unit tests stay offline."""
    import boto3

    return on_event(event, boto3.client("cognito-idp"))
