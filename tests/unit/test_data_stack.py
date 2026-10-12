"""CDK assertion tests for the initial OERF data infrastructure.

These tests assert on the synthesized CloudFormation template only and never
make real AWS calls (see guide section 24).
"""

import aws_cdk as cdk
import pytest
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk.assertions import Template

from infra.stacks.data_stack import OerfDataStack

ACCOUNT = "123456789012"
REGION = "us-east-1"

EXPECTED_TABLE_NAMES = (
    "PlatformTable",
    "TenantTable",
    "IncidentTable",
    "RoutingTable",
)
EXPECTED_BUCKET_NAMES = ("EvidenceBucket", "ReportBucket")

COMPOSITE_KEY_SCHEMA = [
    {"AttributeName": "pk", "KeyType": "HASH"},
    {"AttributeName": "sk", "KeyType": "RANGE"},
]
COMPOSITE_ATTRIBUTES = [
    {"AttributeName": "pk", "AttributeType": "S"},
    {"AttributeName": "sk", "AttributeType": "S"},
]


@pytest.fixture(scope="module")
def stack() -> OerfDataStack:
    app = cdk.App()
    return OerfDataStack(
        app,
        "OerfDataStack",
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )


@pytest.fixture(scope="module")
def template(stack: OerfDataStack) -> Template:
    return Template.from_stack(stack)


def test_stack_exposes_construct_attributes(stack: OerfDataStack) -> None:
    assert isinstance(stack.platform_table, dynamodb.Table)
    assert isinstance(stack.tenant_table, dynamodb.Table)
    assert isinstance(stack.incident_table, dynamodb.Table)
    assert isinstance(stack.routing_table, dynamodb.Table)


def test_four_dynamodb_tables_are_created(
    template: Template,
    stack: OerfDataStack,
) -> None:
    template.resource_count_is("AWS::DynamoDB::Table", 4)

    tables = (
        stack.platform_table,
        stack.tenant_table,
        stack.incident_table,
        stack.routing_table,
    )
    logical_ids = [stack.get_logical_id(table.node.default_child) for table in tables]
    resource_ids = set(template.find_resources("AWS::DynamoDB::Table"))

    assert len(set(logical_ids)) == 4
    assert set(logical_ids) <= resource_ids
    # CDK appends a hash suffix to the logical id, so match on the prefix.
    assert all(
        logical_id.startswith(name)
        for logical_id, name in zip(logical_ids, EXPECTED_TABLE_NAMES)
    )


def test_two_s3_buckets_are_created(
    template: Template,
    stack: OerfDataStack,
) -> None:
    template.resource_count_is("AWS::S3::Bucket", 2)

    buckets = (stack.evidence_bucket, stack.report_bucket)
    logical_ids = [
        stack.get_logical_id(bucket.node.default_child) for bucket in buckets
    ]
    resource_ids = set(template.find_resources("AWS::S3::Bucket"))

    assert len(set(logical_ids)) == 2
    assert set(logical_ids) <= resource_ids
    assert all(
        logical_id.startswith(name)
        for logical_id, name in zip(logical_ids, EXPECTED_BUCKET_NAMES)
    )


def test_oerf_event_bus_is_created(template: Template) -> None:
    template.resource_count_is("AWS::Events::EventBus", 1)
    template.has_resource_properties(
        "AWS::Events::EventBus",
        {"Name": "oerf-events"},
    )


def test_dynamodb_tables_use_on_demand_billing(template: Template) -> None:
    for logical_id, table in template.find_resources("AWS::DynamoDB::Table").items():
        assert table["Properties"]["BillingMode"] == "PAY_PER_REQUEST", logical_id


def test_dynamodb_tables_use_pk_sk_composite_key(template: Template) -> None:
    for logical_id, table in template.find_resources("AWS::DynamoDB::Table").items():
        assert table["Properties"]["KeySchema"] == COMPOSITE_KEY_SCHEMA, logical_id
        assert table["Properties"]["AttributeDefinitions"] == COMPOSITE_ATTRIBUTES, (
            logical_id
        )


def test_dynamodb_tables_have_point_in_time_recovery(template: Template) -> None:
    for logical_id, table in template.find_resources("AWS::DynamoDB::Table").items():
        assert table["Properties"]["PointInTimeRecoverySpecification"] == {
            "PointInTimeRecoveryEnabled": True
        }, logical_id


def test_dynamodb_tables_do_not_define_global_secondary_indexes(
    template: Template,
) -> None:
    for logical_id, table in template.find_resources("AWS::DynamoDB::Table").items():
        assert "GlobalSecondaryIndexes" not in table["Properties"], logical_id


def test_s3_buckets_block_public_access(template: Template) -> None:
    for logical_id, bucket in template.find_resources("AWS::S3::Bucket").items():
        assert bucket["Properties"]["PublicAccessBlockConfiguration"] == {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }, logical_id


def test_s3_buckets_have_versioning_enabled(template: Template) -> None:
    for logical_id, bucket in template.find_resources("AWS::S3::Bucket").items():
        assert bucket["Properties"]["VersioningConfiguration"] == {
            "Status": "Enabled"
        }, logical_id


def test_s3_buckets_use_managed_encryption(template: Template) -> None:
    for logical_id, bucket in template.find_resources("AWS::S3::Bucket").items():
        assert bucket["Properties"]["BucketEncryption"] == {
            "ServerSideEncryptionConfiguration": [
                {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
            ]
        }, logical_id


def test_persistent_resources_use_retain_removal_policy(
    template: Template,
) -> None:
    persistent_resources = {
        **template.find_resources("AWS::DynamoDB::Table"),
        **template.find_resources("AWS::S3::Bucket"),
    }
    assert len(persistent_resources) == 6
    for logical_id, resource in persistent_resources.items():
        assert resource["DeletionPolicy"] == "Retain", logical_id
        assert resource["UpdateReplacePolicy"] == "Retain", logical_id
