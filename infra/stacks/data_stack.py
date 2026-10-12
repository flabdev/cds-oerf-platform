from aws_cdk import (
    RemovalPolicy,
    Stack,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_s3 as s3,
)
from constructs import Construct

# PITR is enabled on all four DynamoDB tables (see guide section 21).
# The installed aws-cdk-lib deprecates the boolean ``point_in_time_recovery``
# property in favour of ``point_in_time_recovery_specification``.
PITR_ENABLED = dynamodb.PointInTimeRecoverySpecification(
    point_in_time_recovery_enabled=True,
)


class OerfDataStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---------------------------------------------------------
        # DynamoDB: Platform Table
        # ---------------------------------------------------------
        self.platform_table = dynamodb.Table(
            self,
            "PlatformTable",
            partition_key=dynamodb.Attribute(
                name="pk",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="sk",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=PITR_ENABLED,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ---------------------------------------------------------
        # DynamoDB: Tenant Table
        # ---------------------------------------------------------
        self.tenant_table = dynamodb.Table(
            self,
            "TenantTable",
            partition_key=dynamodb.Attribute(
                name="pk",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="sk",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=PITR_ENABLED,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ---------------------------------------------------------
        # DynamoDB: Incident Table
        # ---------------------------------------------------------
        self.incident_table = dynamodb.Table(
            self,
            "IncidentTable",
            partition_key=dynamodb.Attribute(
                name="pk",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="sk",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=PITR_ENABLED,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ---------------------------------------------------------
        # DynamoDB: Routing Table
        # ---------------------------------------------------------
        self.routing_table = dynamodb.Table(
            self,
            "RoutingTable",
            partition_key=dynamodb.Attribute(
                name="pk",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="sk",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=PITR_ENABLED,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ---------------------------------------------------------
        # S3: Evidence Bucket
        # ---------------------------------------------------------
        self.evidence_bucket = s3.Bucket(
            self,
            "EvidenceBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ---------------------------------------------------------
        # S3: Report Bucket
        # ---------------------------------------------------------
        self.report_bucket = s3.Bucket(
            self,
            "ReportBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ---------------------------------------------------------
        # EventBridge: OERF Custom Event Bus
        # ---------------------------------------------------------
        self.event_bus = events.EventBus(
            self,
            "OerfEventBus",
            event_bus_name="oerf-events",
        )
