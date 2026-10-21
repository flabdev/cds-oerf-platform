"""Custom resource adds custom:tenant_id without touching an existing schema."""

from typing import Any

from oerf.platform.auth.tenant_attribute import (
    PHYSICAL_ID,
    SCHEMA_NAME,
    attribute_definition,
    on_event,
)


class _Cognito:
    def __init__(self, schema: list[dict[str, str]]) -> None:
        self.schema = schema
        self.added: list[dict[str, Any]] | None = None
        self.pool_ids: list[str] = []

    def describe_user_pool(self, UserPoolId: str) -> dict[str, Any]:
        self.pool_ids.append(UserPoolId)
        return {"UserPool": {"SchemaAttributes": self.schema}}

    def add_custom_attributes(
        self,
        UserPoolId: str,
        CustomAttributes: list[dict[str, Any]],
    ) -> None:
        self.pool_ids.append(UserPoolId)
        self.added = CustomAttributes


def test_attribute_is_a_mutable_optional_string() -> None:
    definition = attribute_definition()

    assert definition["Name"] == "tenant_id"
    assert definition["AttributeDataType"] == "String"
    assert definition["Mutable"] is True
    assert definition["Required"] is False
    assert definition["StringAttributeConstraints"]["MinLength"] == "1"


def test_adds_the_attribute_when_the_schema_does_not_have_it() -> None:
    client = _Cognito(schema=[{"Name": "email"}])

    result = on_event(
        {"RequestType": "Create", "ResourceProperties": {"UserPoolId": "pool-1"}},
        client,
    )

    assert result == {"PhysicalResourceId": PHYSICAL_ID}
    assert client.added == [attribute_definition()]
    assert client.pool_ids == ["pool-1", "pool-1"]


def test_does_not_call_add_when_the_attribute_exists() -> None:
    client = _Cognito(schema=[{"Name": SCHEMA_NAME}])

    on_event(
        {"RequestType": "Update", "ResourceProperties": {"UserPoolId": "pool-1"}},
        client,
    )

    assert client.added is None


def test_delete_does_not_call_cognito() -> None:
    client = _Cognito(schema=[])

    result = on_event(
        {"RequestType": "Delete", "PhysicalResourceId": PHYSICAL_ID},
        client,
    )

    assert result == {"PhysicalResourceId": PHYSICAL_ID}
    assert client.pool_ids == []
    assert client.added is None
