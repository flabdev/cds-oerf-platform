from aws_cdk import (
    RemovalPolicy,
    Stack,
    Duration,
    CfnOutput,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_s3 as s3,
    aws_kms as kms,
    aws_cognito as cognito,
    aws_iam as iam,
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
        # KMS: Customer Managed Key for Encryption
        # ---------------------------------------------------------
        self.kms_key = kms.Key(
            self,
            "OerfKmsKey",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

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
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.kms_key,
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
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.kms_key,
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
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.kms_key,
            stream=dynamodb.StreamViewType.NEW_IMAGE,
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
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.kms_key,
            time_to_live_attribute="ttl",
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
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.kms_key,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.evidence_bucket.add_lifecycle_rule(expiration=Duration.days(90))

        # ---------------------------------------------------------
        # S3: Report Bucket
        # ---------------------------------------------------------
        self.report_bucket = s3.Bucket(
            self,
            "ReportBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.kms_key,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.report_bucket.add_lifecycle_rule(expiration=Duration.days(90))

        # ---------------------------------------------------------
        # EventBridge: OERF Custom Event Bus
        # ---------------------------------------------------------
        self.event_bus = events.EventBus(
            self,
            "OerfEventBus",
            event_bus_name="oerf-events",
        )

        # ---------------------------------------------------------
        # Cognito: User Pool for Dashboard Auth
        # ---------------------------------------------------------
        self.user_pool = cognito.UserPool(
            self, 
            "OerfUserPool",
            user_pool_name="oerf-users",
            self_sign_up_enabled=False,
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            removal_policy=RemovalPolicy.RETAIN
        )
        
        cognito.CfnUserPoolGroup(self, "PlatformAdminGroup", user_pool_id=self.user_pool.user_pool_id, group_name="platform_admin")
        cognito.CfnUserPoolGroup(self, "TenantAdminGroup", user_pool_id=self.user_pool.user_pool_id, group_name="tenant_admin")
        cognito.CfnUserPoolGroup(self, "OpsUserGroup", user_pool_id=self.user_pool.user_pool_id, group_name="ops_user")

        # ---------------------------------------------------------
        # IAM: GitHub OIDC Provider for CI/CD
        # ---------------------------------------------------------
        github_domain = "token.actions.githubusercontent.com"
        github_provider = iam.OpenIdConnectProvider(
            self, 
            "GithubOIDCProvider",
            url=f"https://{github_domain}",
            client_ids=["sts.amazonaws.com"]
        )
        
        github_role = iam.Role(
            self, 
            "GitHubDeployRole",
            role_name="OerfGitHubDeployRole",
            assumed_by=iam.OpenIdConnectPrincipal(github_provider, conditions={
                "StringLike": {
                    f"{github_domain}:sub": "repo:flabdev/cds-oerf-platform:*"
                }
            }),
            description="Role assumed by GitHub Actions to deploy CDK"
        )
        github_role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("AdministratorAccess"))

        # ---------------------------------------------------------
        # Stack Outputs
        # ---------------------------------------------------------
        CfnOutput(self, "GitHubDeployRoleArn", value=github_role.role_arn)
        CfnOutput(self, "KmsKeyArn", value=self.kms_key.key_arn)
        CfnOutput(self, "IncidentTableName", value=self.incident_table.table_name)
        CfnOutput(self, "EvidenceBucketName", value=self.evidence_bucket.bucket_name)
        CfnOutput(self, "ReportBucketName", value=self.report_bucket.bucket_name)
        CfnOutput(self, "CognitoUserPoolId", value=self.user_pool.user_pool_id)

