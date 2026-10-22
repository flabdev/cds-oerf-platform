"""AgentCore runtime for the OERF-206 placeholder.

The container lives in ``oerf/platform/agent/agentcore/``. ``runtime.py`` next
to it is the step composition root, so this image is not that module.

``DockerImageAsset`` publishes into the CDK asset repository. The execution
role pulls that repository.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_bedrockagentcore as agentcore,
    aws_dynamodb as dynamodb,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_logs as logs,
)
from constructs import Construct

_IMAGE = Path(__file__).resolve().parents[2] / "oerf" / "platform" / "agent" / "agentcore"
IDLE_TIMEOUT = Duration.seconds(60)
MAX_LIFETIME = Duration.minutes(5)
_LOG_GROUP = "/aws/vendedlogs/bedrock-agentcore/oerf_agent"
_RUNTIME_NAME = "oerf_agent"


class AgentCoreRuntime(Construct):
    """Placeholder runtime, its image, and the role that may invoke it."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        incident_table: dynamodb.ITable,
        tenant_table: dynamodb.ITable,
        guardrail_resources: list[str],
        guardrail_id: str,
        guardrail_version: str,
        model_id: str,
    ) -> None:
        super().__init__(scope, construct_id)
        image_asset = ecr_assets.DockerImageAsset(
            self,
            "Image",
            directory=str(_IMAGE),
            platform=ecr_assets.Platform.LINUX_ARM64,
        )
        stack = Stack.of(self)
        # AgentCore passes both keys when it assumes this role.
        # https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html
        self.execution_role = iam.Role(
            self,
            "AgentRuntimeExecutionRole",
            role_name="oerf-agent-runtime",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": stack.account},
                    "ArnLike": {
                        "aws:SourceArn": stack.format_arn(
                            service="bedrock-agentcore",
                            resource="*",
                        )
                    },
                },
            ),
        )
        image_asset.repository.grant_pull(self.execution_role)
        incident_table.grant_read_data(self.execution_role)
        tenant_table.grant_read_data(self.execution_role)
        self._grant_model_and_guardrail(model_id, guardrail_resources)
        log_group = logs.LogGroup(
            self,
            "LogGroup",
            log_group_name=_LOG_GROUP,
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        log_group.grant_write(self.execution_role)
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
                resources=["*"],
            )
        )
        self.runtime = agentcore.Runtime(
            self,
            "Runtime",
            runtime_name=_RUNTIME_NAME,
            description="placeholder, behaviour owned by OERF-206",
            agent_runtime_artifact=agentcore.AgentRuntimeArtifact.from_image_uri(
                image_asset.image_uri
            ),
            execution_role=self.execution_role,
            network_configuration=agentcore.RuntimeNetworkConfiguration.using_public_network(),
            lifecycle_configuration=agentcore.LifecycleConfiguration(
                idle_runtime_session_timeout=IDLE_TIMEOUT,
                max_lifetime=MAX_LIFETIME,
            ),
            environment_variables={
                "BEDROCK_MODEL_ID": model_id,
                "GUARDRAIL_ID": guardrail_id,
                "GUARDRAIL_VERSION": guardrail_version,
                "INCIDENT_TABLE_NAME": incident_table.table_name,
                "TENANT_TABLE_NAME": tenant_table.table_name,
            },
            logging_configs=[
                agentcore.LoggingConfig(
                    log_type=agentcore.LogType.APPLICATION_LOGS,
                    destination=agentcore.LoggingDestination.cloud_watch_logs(log_group),
                )
            ],
            tracing_enabled=True,
            manage_delivery_resource_policy=False,
        )
        self.agent_runtime_arn = self.runtime.agent_runtime_arn
        self.caller_role = iam.Role(
            self,
            "AgentCallerRole",
            role_name="oerf-agent-caller",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        self.caller_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[self.agent_runtime_arn],
            )
        )

    def _grant_model_and_guardrail(self, model_id: str, guardrail_resources: list[str]) -> None:
        region = Stack.of(self).region
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[f"arn:aws:bedrock:{region}::foundation-model/{model_id}"],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:ApplyGuardrail"],
                resources=guardrail_resources,
            )
        )
