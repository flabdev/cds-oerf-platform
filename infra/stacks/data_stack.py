import json
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
    aws_xray as xray,
    custom_resources as cr,
)
from constructs import Construct

from infra.step_functions import (
    ASL_ENGAGE_PLACEHOLDERS,
    ASL_STEP_PLACEHOLDERS,
    CHANNEL_ROUTER_NAME,
    ENGAGE_FUNCTION_NAMES,
    STEP_FUNCTION_NAMES,
    function_arn,
)

# PITR is enabled on all four DynamoDB tables (see guide section 21).
# The installed aws-cdk-lib deprecates the boolean ``point_in_time_recovery``
# property in favour of ``point_in_time_recovery_specification``.
PITR_ENABLED = dynamodb.PointInTimeRecoverySpecification(
    point_in_time_recovery_enabled=True,
)
_AUTH_ASSET = Path(__file__).resolve().parents[2] / "oerf" / "platform" / "auth"


def _agentcore_enabled(scope: Construct) -> bool:
    """True only when context ``agentcore:enabled`` is the string ``true``."""
    return scope.node.try_get_context("agentcore:enabled") == "true"


def _enable_transaction_search(stack: Stack) -> xray.CfnTransactionSearchConfig:
    """Let X-Ray write spans so a trace destination of CloudWatch Logs is active.

    ``IndexingPercentage`` is omitted. CloudFormation then keeps the service default.
    The ``*`` in each log-group ARN is the log stream, and the source ARN ``*`` is
    any X-Ray resource in this account. See ``docs/security/iam-exceptions.md``.
    """
    account = stack.account
    region = stack.region
    document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "TransactionSearchXRayAccess",
                "Effect": "Allow",
                "Principal": {"Service": "xray.amazonaws.com"},
                "Action": "logs:PutLogEvents",
                "Resource": [
                    f"arn:aws:logs:{region}:{account}:log-group:aws/spans:*",
                    (
                        f"arn:aws:logs:{region}:{account}:log-group:"
                        "/aws/application-signals/data:*"
                    ),
                ],
                "Condition": {
                    "ArnLike": {"aws:SourceArn": f"arn:aws:xray:{region}:{account}:*"},
                    "StringEquals": {"aws:SourceAccount": account},
                },
            }
        ],
    }
    policy = logs.CfnResourcePolicy(
        stack,
        "TransactionSearchXRayAccess",
        policy_name="TransactionSearchXRayAccess",
        policy_document=json.dumps(document),
    )
    # Account-wide singleton. Deleting it (stack delete, or a later deploy
    # with agentcore off) turns Transaction Search off for the account.
    config = xray.CfnTransactionSearchConfig(stack, "TransactionSearchConfig")
    config.node.add_dependency(policy)
    return config


def _kms_managed(scope: Construct) -> bool:
    """AWS managed table keys only when context ``dynamodb:kmsManaged`` is ``true``.

    The default omits ``encryption``, which is the AWS owned key already on
    the deployed tables. A table's encryption mode can change four times per
    24 hours, then once per 6 hours (ADR-033).
    """
    return scope.node.try_get_context("dynamodb:kmsManaged") == "true"


def _use_aws_managed_key(table: dynamodb.Table) -> None:
    """Name ``SSEType`` ``KMS``. The L2 sets ``SSEEnabled`` and omits the type."""
    cfn_table = table.node.default_child
    if not isinstance(cfn_table, dynamodb.CfnTable):
        raise TypeError("DynamoDB table default child is not CfnTable")
    cfn_table.add_property_override(
        "SSESpecification",
        {"SSEEnabled": True, "SSEType": "KMS"},
    )


def _engage_invoke_resources(region: str, account: str, stub_arn: str) -> list[str]:
    """The stub, the cadence and interpret functions, and the channel router."""

    resources = [stub_arn, f"{stub_arn}:*"]
    names = [*ENGAGE_FUNCTION_NAMES.values(), CHANNEL_ROUTER_NAME]
    for name in names:
        arn = function_arn(region, account, name)
        resources.extend([arn, f"{arn}:*"])
    return resources


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
        self.transaction_search_config = (
            _enable_transaction_search(self) if _agentcore_enabled(self) else None
        )
        kms_managed = _kms_managed(self)
        table_encryption = (
            {"encryption": dynamodb.TableEncryption.AWS_MANAGED} if kms_managed else {}
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
            **table_encryption,
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
            **table_encryption,
            point_in_time_recovery_specification=PITR_ENABLED,
            removal_policy=RemovalPolicy.RETAIN,
        )
        # TTL stays off. SBXSESSION rows store expires_at and are still read
        # after that time, including an opted-out session.

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
            **table_encryption,
            stream=dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
            point_in_time_recovery_specification=PITR_ENABLED,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="expires_at",
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
            **table_encryption,
            point_in_time_recovery_specification=PITR_ENABLED,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="expires_at",
        )
        if kms_managed:
            for table in (
                self.platform_table,
                self.tenant_table,
                self.incident_table,
                self.routing_table,
            ):
                _use_aws_managed_key(table)

        # ---------------------------------------------------------
        # S3: Evidence Bucket
        # ---------------------------------------------------------
        # Current objects last 90 days. Noncurrent versions last 30 (ADR-018).
        self.evidence_bucket = s3.Bucket(
            self,
            "EvidenceBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS_MANAGED,
            bucket_key_enabled=True,
            versioned=True,
            enforce_ssl=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-evidence",
                    expiration=Duration.days(90),
                    noncurrent_version_expiration=Duration.days(30),
                )
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ---------------------------------------------------------
        # S3: Report Bucket
        # ---------------------------------------------------------
        # Reports are the audit record, so current versions are kept.
        # Noncurrent versions expire after 90 days.
        self.report_bucket = s3.Bucket(
            self,
            "ReportBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS_MANAGED,
            bucket_key_enabled=True,
            versioned=True,
            enforce_ssl=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-noncurrent-reports",
                    noncurrent_version_expiration=Duration.days(90),
                )
            ],
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
        # Both machines invoke this function. Tracing adds the two X-Ray
        # actions on Resource *. See iam-exceptions.md.
        workflow_stub_logs = logs.LogGroup(
            self,
            "ColdChainWorkflowStubLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
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
            log_group=workflow_stub_logs,
            timeout=Duration.seconds(30),
            tracing=lambda_.Tracing.ACTIVE,
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
        # Literal ARNs. A CDK reference would make this stack depend on compute.
        step_arns = [
            function_arn(self.region, self.account, name) for name in STEP_FUNCTION_NAMES.values()
        ]
        invoke_resources = [workflow_stub.function_arn, f"{workflow_stub.function_arn}:*"]
        for arn in step_arns:
            invoke_resources.extend([arn, f"{arn}:*"])
        router_arn = function_arn(self.region, self.account, CHANNEL_ROUTER_NAME)
        invoke_resources.extend([router_arn, f"{router_arn}:*"])
        workflow_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=invoke_resources,
            )
        )
        asl_path = (
            Path(__file__).resolve().parents[2]
            / "domains"
            / "cold_chain_excursion"
            / "statemachine.asl.json"
        )
        definition = asl_path.read_text(encoding="utf-8")
        for placeholder, phase in ASL_STEP_PLACEHOLDERS.items():
            definition = definition.replace(
                placeholder,
                function_arn(self.region, self.account, STEP_FUNCTION_NAMES[phase]),
            )
        definition = definition.replace("__STUB_ARN__", workflow_stub.function_arn)
        definition = definition.replace("__CHANNEL_ROUTER_ARN__", router_arn)
        engagement_asl_path = (
            Path(__file__).resolve().parents[2]
            / "domains"
            / "cold_chain_excursion"
            / "engage_party.asl.json"
        )
        engagement_definition = engagement_asl_path.read_text(encoding="utf-8").replace(
            "__ENGAGEMENT_STUB_ARN__", workflow_stub.function_arn
        )
        for placeholder, phase in ASL_ENGAGE_PLACEHOLDERS.items():
            engagement_definition = engagement_definition.replace(
                placeholder,
                function_arn(self.region, self.account, ENGAGE_FUNCTION_NAMES[phase]),
            )
        engagement_definition = engagement_definition.replace(
            "__CHANNEL_ROUTER_ARN__",
            router_arn,
        )
        engagement_role = iam.Role(
            self,
            "EngagePartyStateMachineRole",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
            description="Execution role for the reusable engagement state machine",
        )
        engagement_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=_engage_invoke_resources(
                    self.region, self.account, workflow_stub.function_arn
                ),
            )
        )
        # ERROR only. Execution data can carry phone numbers and message text.
        # Logging adds logs:CreateLogDelivery and the rest of the delivery
        # family on Resource *. Tracing adds xray:PutTraceSegments,
        # PutTelemetryRecords, GetSamplingRules, and GetSamplingTargets on
        # Resource *. Those APIs have no resource type. See iam-exceptions.md.
        engagement_logs = logs.LogGroup(
            self,
            "EngagePartyStateMachineLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        # Raw ASL does not make CDK grant what startExecution.sync:2 needs.
        # Synchronous nested execution, from
        # https://docs.aws.amazon.com/step-functions/latest/dg/connect-stepfunctions.html
        # (IAM policies for calling nested Step Functions workflows):
        # states:StartExecution on the child state machine;
        # states:DescribeExecution and states:StopExecution on its executions
        # (the * is the execution name; see iam-exceptions.md);
        # events:PutTargets, events:PutRule, and events:DescribeRule on
        # rule/StepFunctionsGetEventsForStepFunctionsExecutionRule.
        # StartExecution uses a state machine ARN. Describe and Stop use an
        # execution ARN. Combining the three on one resource is incorrect.
        engage_state_machine_name = "oerf-engage-party"
        self.engage_party_state_machine = sfn.StateMachine(
            self,
            "EngagePartyStateMachine",
            state_machine_name=engage_state_machine_name,
            state_machine_type=sfn.StateMachineType.STANDARD,
            role=engagement_role,
            tracing_enabled=True,
            logs=sfn.LogOptions(
                destination=engagement_logs,
                level=sfn.LogLevel.ERROR,
                include_execution_data=False,
            ),
            definition_body=sfn.DefinitionBody.from_string(engagement_definition),
        )
        workflow_role.add_to_policy(
            iam.PolicyStatement(
                actions=["states:StartExecution"],
                resources=[self.engage_party_state_machine.state_machine_arn],
            )
        )
        workflow_role.add_to_policy(
            iam.PolicyStatement(
                actions=["states:DescribeExecution", "states:StopExecution"],
                resources=[
                    (
                        f"arn:aws:states:{self.region}:{self.account}:execution:"
                        f"{engage_state_machine_name}:*"
                    )
                ],
            )
        )
        workflow_role.add_to_policy(
            iam.PolicyStatement(
                actions=["events:PutTargets", "events:PutRule", "events:DescribeRule"],
                resources=[
                    (
                        f"arn:aws:events:{self.region}:{self.account}:rule/"
                        "StepFunctionsGetEventsForStepFunctionsExecutionRule"
                    )
                ],
            )
        )
        definition = definition.replace(
            "__ENGAGE_PARTY_ARN__", self.engage_party_state_machine.state_machine_arn
        )
        cold_chain_logs = logs.LogGroup(
            self,
            "ColdChainStateMachineLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.cold_chain_state_machine = sfn.StateMachine(
            self,
            "OerfColdChainStateMachine",
            state_machine_name="oerf-cold-chain",
            state_machine_type=sfn.StateMachineType.STANDARD,
            role=workflow_role,
            tracing_enabled=True,
            logs=sfn.LogOptions(
                destination=cold_chain_logs,
                level=sfn.LogLevel.ERROR,
                include_execution_data=False,
            ),
            definition_body=sfn.DefinitionBody.from_string(definition),
        )
        # ---------------------------------------------------------
        # Cognito: User Pool for Dashboard Auth
        # ---------------------------------------------------------
        # feature_plan is UserPoolTier. It updates in place. Do not set
        # custom_attributes here: Schema is create-only and would replace this pool.
        # V2 pre-token events, which copy custom:tenant_id onto the access token,
        # require Essentials or Plus.
        # Password policy, MfaConfiguration, EnabledMfas, and
        # TemporaryPasswordValidityDays are "Update requires: No interruption"
        # (CloudFormation UserPool and PasswordPolicy). UserPoolAddOns is paid
        # threat protection and stays unset.
        self.user_pool = cognito.UserPool(
            self,
            "OerfUserPool",
            user_pool_name="oerf-users",
            self_sign_up_enabled=False,
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            feature_plan=cognito.FeaturePlan.ESSENTIALS,
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
                temp_password_validity=Duration.days(3),
            ),
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
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
        # IAM: GitHub OIDC Provider for CI/CD (SEC-1)
        # ---------------------------------------------------------
        # aud and sub are exact. There is no StringLike and no wildcard.
        github_domain = "token.actions.githubusercontent.com"
        # Immutable subject first, then the name form. Either may be the token sub.
        github_subs = [
            "repo:flabdev@25605151/cds-oerf-platform@1408373494:ref:refs/heads/main",
            "repo:flabdev/cds-oerf-platform:ref:refs/heads/main",
        ]
        # Legacy custom resource. Its role grants the five iam:OpenIDConnect
        # actions on Resource *. Replacing it with the native provider would
        # replace the provider during the deploy-role change. See
        # iam-exceptions.md.
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
            assumed_by=iam.OpenIdConnectPrincipal(
                github_provider,
                conditions={
                    "StringEquals": {
                        f"{github_domain}:aud": "sts.amazonaws.com",
                        f"{github_domain}:sub": github_subs,
                    },
                },
            ),
            description="Assumed by GitHub Actions on main to deploy CDK",
        )
        # cdk deploy assumes the bootstrap roles, then calls CloudFormation and
        # reads /cdk-bootstrap/hnb659fds/version as the deploy role. Those
        # actions live on DeploymentActionRole in the bootstrap template
        # (sids CliPermissions and ReadVersion), not on this role.
        # https://docs.aws.amazon.com/cdk/v2/guide/bootstrapping.html
        account = Stack.of(self).account
        # Partition is aws. This platform deploys only in commercial us-east-1.
        bootstrap_roles = (
            f"arn:aws:iam::{account}:role/"
            f"cdk-hnb659fds-*-{account}-{Stack.of(self).region}"
        )
        # The * is the bootstrap role name. See docs/security/iam-exceptions.md.
        github_role.add_to_policy(
            iam.PolicyStatement(
                sid="AssumeCdkBootstrapRoles",
                actions=["sts:AssumeRole"],
                resources=[bootstrap_roles],
            )
        )

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
        CfnOutput(
            self,
            "EngagePartyStateMachineArn",
            value=self.engage_party_state_machine.state_machine_arn,
        )

