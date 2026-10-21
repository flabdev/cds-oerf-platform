from pathlib import Path

from aws_cdk import (
    CfnOutput,
    CustomResource,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_stepfunctions as sfn,
    custom_resources as cr,
)
from constructs import Construct

# PITR is enabled on all four DynamoDB tables (see guide section 21).
# The installed aws-cdk-lib deprecates the boolean ``point_in_time_recovery``
# property in favour of ``point_in_time_recovery_specification``.
PITR_ENABLED = dynamodb.PointInTimeRecoverySpecification(
    point_in_time_recovery_enabled=True,
)
_AUTH_ASSET = Path(__file__).resolve().parents[2] / "oerf" / "platform" / "auth"


def _auth_lambda(
    stack: Stack,
    construct_id: str,
    function_name: str,
    handler: str,
) -> lambda_.Function:
    """One role and a 30-day log group for a Cognito auth function."""
    log_group = logs.LogGroup(
        stack,
        f"{construct_id}LogGroup",
        retention=logs.RetentionDays.ONE_MONTH,
        removal_policy=RemovalPolicy.DESTROY,
    )
    role = iam.Role(
        stack,
        f"{construct_id}Role",
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
    )
    log_group.grant_write(role)
    return lambda_.Function(
        stack,
        construct_id,
        function_name=function_name,
        runtime=lambda_.Runtime.PYTHON_3_11,
        handler=handler,
        code=lambda_.Code.from_asset(
            str(_AUTH_ASSET),
            exclude=["__pycache__", "*.pyc"],
        ),
        role=role,
        log_group=log_group,
        timeout=Duration.seconds(10),
        memory_size=128,
        # tracing ACTIVE adds xray:PutTraceSegments and xray:PutTelemetryRecords
        # on Resource *. Those two actions are not resource-scopable.
        tracing=lambda_.Tracing.ACTIVE,
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
            stream=dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
            point_in_time_recovery_specification=PITR_ENABLED,
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.incident_table.add_global_secondary_index(
            index_name="GSI1",
            partition_key=dynamodb.Attribute(
                name="GSI1PK",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="GSI1SK",
                type=dynamodb.AttributeType.STRING,
            ),
            projection_type=dynamodb.ProjectionType.ALL,
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
        # ---------------------------------------------------------
        # Step Functions: cold_chain_excursion_v1 workflow skeleton
        # ---------------------------------------------------------
        workflow_stub = lambda_.Function(
            self,
            "ColdChainWorkflowStub",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="workflow_stub.handler",
            code=lambda_.Code.from_asset(
                str(
                    Path(__file__).resolve().parents[2]
                    / "domains"
                    / "cold_chain_excursion"
                    / "steps"
                ),
                exclude=["__pycache__", "*.pyc", ".gitkeep"],
            ),
            timeout=Duration.seconds(30),
            description=(
                "Temporary deterministic integration stub for cold_chain_excursion_v1"
            ),
        )
        workflow_role = iam.Role(
            self,
            "OerfColdChainStateMachineRole",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
            description="Execution role for the OERF cold-chain state machine",
        )
        workflow_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[workflow_stub.function_arn, f"{workflow_stub.function_arn}:*"],
            )
        )
        asl_path = (
            Path(__file__).resolve().parents[2]
            / "domains"
            / "cold_chain_excursion"
            / "statemachine.asl.json"
        )
        definition = asl_path.read_text(encoding="utf-8").replace(
            "__STUB_ARN__", workflow_stub.function_arn
        )
        self.cold_chain_state_machine = sfn.StateMachine(
            self,
            "OerfColdChainStateMachine",
            state_machine_name="oerf-cold-chain",
            state_machine_type=sfn.StateMachineType.STANDARD,
            role=workflow_role,
            tracing_enabled=True,
            definition_body=sfn.DefinitionBody.from_string(definition),
        )
        # ---------------------------------------------------------
        # Cognito: User Pool for Dashboard Auth
        # ---------------------------------------------------------
        # feature_plan is UserPoolTier. It updates in place. Do not set
        # custom_attributes here: Schema is create-only and would replace this pool.
        # V2 pre-token events, which copy custom:tenant_id onto the access token,
        # require Essentials or Plus.
        self.user_pool = cognito.UserPool(
            self,
            "OerfUserPool",
            user_pool_name="oerf-users",
            self_sign_up_enabled=False,
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            feature_plan=cognito.FeaturePlan.ESSENTIALS,
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.user_pool.add_trigger(
            cognito.UserPoolOperation.PRE_TOKEN_GENERATION_CONFIG,
            _auth_lambda(
                self,
                "PreTokenFunction",
                "oerf-pre-token",
                "pre_token.handler",
            ),
        )
        # The L2 writes LambdaVersion V1_0 for this trigger. V1 cannot add
        # claims to the access token. Override the same property to V2_0.
        # This stays on LambdaConfig and does not change Schema.
        self.user_pool.node.default_child.add_property_override(
            "LambdaConfig.PreTokenGenerationConfig.LambdaVersion",
            "V2_0",
        )
        tenant_attribute_fn = _auth_lambda(
            self,
            "TenantIdAttributeFunction",
            "oerf-tenant-attribute",
            "tenant_attribute.handler",
        )
        tenant_attribute_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:DescribeUserPool",
                    "cognito-idp:AddCustomAttributes",
                ],
                resources=[self.user_pool.user_pool_arn],
            )
        )
        tenant_attribute_provider = cr.Provider(
            self,
            "TenantIdAttributeProvider",
            on_event_handler=tenant_attribute_fn,
        )
        CustomResource(
            self,
            "TenantIdAttribute",
            service_token=tenant_attribute_provider.service_token,
            properties={"UserPoolId": self.user_pool.user_pool_id},
        )

        user_pool_id = self.user_pool.user_pool_id
        cognito.CfnUserPoolGroup(
            self, "PlatformAdminGroup", user_pool_id=user_pool_id, group_name="platform_admin"
        )
        cognito.CfnUserPoolGroup(
            self, "TenantAdminGroup", user_pool_id=user_pool_id, group_name="tenant_admin"
        )
        cognito.CfnUserPoolGroup(
            self, "OpsUserGroup", user_pool_id=user_pool_id, group_name="ops_user"
        )

        # ---------------------------------------------------------
        # IAM: GitHub OIDC Provider for CI/CD
        # ---------------------------------------------------------
        github_domain = "token.actions.githubusercontent.com"
        github_provider = iam.OpenIdConnectProvider(
            self,
            "GithubOIDCProvider",
            url=f"https://{github_domain}",
            client_ids=["sts.amazonaws.com"],
        )

        github_role = iam.Role(
            self,
            "GitHubDeployRole",
            role_name="OerfGitHubDeployRole",
            assumed_by=iam.OpenIdConnectPrincipal(github_provider, conditions={
                "StringEquals": {
                    f"{github_domain}:aud": "sts.amazonaws.com",
                },
                "StringLike": {
                    f"{github_domain}:sub": [
                        "repo:flabdev/cds-oerf-platform:*",
                        "repo:flabdev*/cds-oerf-platform*:*",
                        "repo:*cds-oerf-platform*:*",
                    ],
                },
            }),
            description="Role assumed by GitHub Actions to deploy CDK",
        )
        github_role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("AdministratorAccess"))

        # ---------------------------------------------------------
        # Stack Outputs
        # ---------------------------------------------------------
        CfnOutput(self, "GitHubDeployRoleArn", value=github_role.role_arn)
        CfnOutput(self, "PlatformTableName", value=self.platform_table.table_name)
        CfnOutput(self, "TenantTableName", value=self.tenant_table.table_name)
        CfnOutput(self, "IncidentTableName", value=self.incident_table.table_name)
        CfnOutput(self, "RoutingTableName", value=self.routing_table.table_name)
        CfnOutput(self, "EvidenceBucketName", value=self.evidence_bucket.bucket_name)
        CfnOutput(self, "ReportBucketName", value=self.report_bucket.bucket_name)
        CfnOutput(self, "CognitoUserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(self, "EventBusArn", value=self.event_bus.event_bus_arn)
        CfnOutput(
            self, "ColdChainStateMachineArn", value=self.cold_chain_state_machine.state_machine_arn
        )

