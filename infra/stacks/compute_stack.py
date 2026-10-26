"""Compute, API Gateway, and Lambda stack for the OERF platform.

Provisions the channel router, evidence analyzer, report generator, WebSocket
API, stream broadcaster, action handler, inbound queue, and ingest rule.
One role, one 30-day log group, and active tracing per function.
"""

from __future__ import annotations

import os

from aws_cdk import (
    CfnOutput,
    CustomResource,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_authorizers as authorizers,
    aws_apigatewayv2_integrations as integrations,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_event_sources as event_sources,
    aws_logs as logs,
    aws_sns as sns,
    aws_sns_subscriptions as subscriptions,
    aws_sqs as sqs,
    custom_resources as cr,
)
from constructs import Construct

from infra.constructs.agentcore_runtime import AgentCoreRuntime
from infra.constructs.bedrock_guardrail import BedrockGuardrail
from infra.constructs.waba_event_destination import WabaEventDestination
from infra.iam_conditions import pin_source_account
from infra.runtime_asset import runtime_code_args
from infra.stacks.data_stack import OerfDataStack
from infra.step_functions import STEP_FUNCTION_NAMES, STEP_HANDLERS

# Task-token APIs have no resource type in the IAM Service Authorization
# Reference. The resource stays "*"; see docs/security/iam-exceptions.md.
_TASK_TOKEN_ACTIONS = ["states:SendTaskSuccess"]
_INBOUND_TIMEOUT = Duration.seconds(30)
_VISIBILITY_MULTIPLE = 6
_DEFAULT_INGEST_CONCURRENCY = 5
_WA_PHONE_NUMBER_ID_PARAM = "/oerf/whatsapp/phone-number-id"
_DEFAULT_MODEL_ID = "amazon.nova-lite-v1:0"

def _context_enabled(scope: Construct, key: str, *, default: bool) -> bool:
    """Read a boolean CDK context value. Missing context returns ``default``."""
    raw = scope.node.try_get_context(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in {"false", "0", "no"}


def _context_str(scope: Construct, key: str, default: str = "") -> str:
    """Read a string CDK context value. Missing context returns ``default``."""
    raw = scope.node.try_get_context(key)
    if raw is None:
        return default
    return str(raw)


def _context_int(scope: Construct, key: str, default: int) -> int:
    """Read an integer CDK context value. Missing context returns ``default``."""
    raw = scope.node.try_get_context(key)
    if raw is None or raw == "":
        return default
    return int(raw)


class OerfComputeStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        data_stack: OerfDataStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Shared asset. Packaged from the repo root so imports stay ``oerf.*``
        # and do not shadow the stdlib ``platform`` module. The allow-list is
        # ``oerf``, ``domains``, and ``sim``, which keeps the demo shipment
        # seed data available to ``seed_demo_devices``. The same zip vendors
        # pydantic and jsonschema for manylinux2014 / Python 3.11.
        lambda_code = lambda_.Code.from_asset(".", **runtime_code_args())

        # Demo rows for t1, t2, and sbx. A normal deploy leaves the registry
        # alone. Context demo:seedDevices=true writes them; PutItem is safe to
        # run again. Delete of the resource does not remove the rows.
        if _context_enabled(self, "demo:seedDevices", default=False):
            self.device_seed_fn = self._function(
                "DeviceSeedFunction",
                function_name="oerf-device-seed",
                handler="oerf.platform.channels.devices.seed_handler",
                code=lambda_code,
                timeout=Duration.seconds(30),
                environment={"ROUTING_TABLE_NAME": data_stack.routing_table.table_name},
            )
            data_stack.routing_table.grant_write_data(self.device_seed_fn)
            seed_provider = cr.Provider(
                self,
                "DeviceSeedProvider",
                on_event_handler=self.device_seed_fn,
            )
            CustomResource(
                self,
                "DemoDeviceSeed",
                service_token=seed_provider.service_token,
                properties={"SeedVersion": "1"},
            )

        # ---------------------------------------------------------
        # 1. Channel Router Lambda (ADR-008)
        # ---------------------------------------------------------
        ses_source_email = os.environ.get("SES_SOURCE_EMAIL", "")
        # Live channel sends only when context channels:live is true.
        channel_mock = "false" if _context_enabled(self, "channels:live", default=False) else "true"
        whatsapp_phone_number_id = _context_str(self, "whatsapp:phoneNumberId")
        self.channel_router_fn = self._function(
            "ChannelRouterFunction",
            function_name="oerf-channel-router",
            handler="oerf.platform.channels.router.handler",
            code=lambda_code,
            timeout=Duration.seconds(30),
            environment={
                "ROUTING_TABLE_NAME": data_stack.routing_table.table_name,
                "INCIDENT_TABLE_NAME": data_stack.incident_table.table_name,
                "TENANT_TABLE_NAME": data_stack.tenant_table.table_name,
                "USE_MOCK_WHATSAPP": channel_mock,
                "USE_MOCK_SMS": channel_mock,
                "WHATSAPP_PHONE_NUMBER_ID": whatsapp_phone_number_id,
                "WA_PHONE_NUMBER_ID_PARAM": _WA_PHONE_NUMBER_ID_PARAM,
                "SES_SOURCE_EMAIL": ses_source_email,
                "USE_MOCK_EMAIL": channel_mock,
            },
        )
        data_stack.routing_table.grant_read_write_data(self.channel_router_fn)
        incident_arn = data_stack.incident_table.table_arn
        tenant_arn = data_stack.tenant_table.table_arn
        # ENG# lives on the incident table. CONTACTENG# is written in the same
        # transaction as the engagement row. The transaction is authorised by
        # the item-level actions.
        self.channel_router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                ],
                resources=[incident_arn],
            )
        )
        self.channel_router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem"],
                resources=[tenant_arn],
            )
        )
        if ses_source_email:
            # SESv2 SendEmail authorizes as ses:SendEmail on the identity ARN.
            self.channel_router_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["ses:SendEmail"],
                    resources=[
                        f"arn:aws:ses:{self.region}:{self.account}:identity/{ses_source_email}"
                    ],
                )
            )
        # sns:Publish to a phone number has no resource ARN. SMS should move
        # to pinpoint-sms-voice-v2 under OERF-116 (Mudita).
        self.channel_router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["sns:Publish"],
                resources=["*"],
            )
        )
        # social-messaging:SendWhatsAppMessage requires resource type phone-number-id.
        # arn:${Partition}:social-messaging:${Region}:${Account}:phone-number-id/${Id}
        # Service Authorization Reference, list_socialmessaging.html.
        if whatsapp_phone_number_id:
            whatsapp_resources = [
                (
                    f"arn:aws:social-messaging:{self.region}:{self.account}:"
                    f"phone-number-id/{whatsapp_phone_number_id}"
                )
            ]
        else:
            # TODO: scope this to the phone-number-id ARN when context
            # whatsapp:phoneNumberId is set. See docs/security/iam-exceptions.md.
            whatsapp_resources = ["*"]
        self.channel_router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["social-messaging:SendWhatsAppMessage"],
                resources=whatsapp_resources,
            )
        )
        # WA_PHONE_NUMBER_ID_PARAM names the SSM parameter. The router reads
        # it once per process and uses WHATSAPP_PHONE_NUMBER_ID only when
        # this name is unset. GetParameter is limited to that one parameter.
        # The ARN drops the leading slash.
        self.channel_router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    (
                        f"arn:aws:ssm:{self.region}:{self.account}:parameter"
                        "/oerf/whatsapp/phone-number-id"
                    ),
                    (
                        f"arn:aws:ssm:{self.region}:{self.account}:parameter"
                        "/oerf/demo-numbers/*"
                    ),
                ],
            )
        )

        # ---------------------------------------------------------
        # 2. Bedrock Vision Evidence Extractor Lambda (ADR-006)
        # ---------------------------------------------------------
        # Live Bedrock calls use the same channels:live flag as the channel mocks.
        # ADR-020: amazon.nova-lite-v1:0, in-region on demand in us-east-1.
        # The model card publishes a bedrock-runtime endpoint for this id and
        # the sample Invoke uses the bare id, so the resource is the
        # foundation-model ARN. Converse is authorized as bedrock:InvokeModel.
        # ApplyGuardrail covers the guardrail and the US profile in each
        # destination region. See BedrockGuardrail.apply_resources.
        self.guardrail = BedrockGuardrail(self, "Guardrail")
        bedrock_model_id = _context_str(self, "bedrock:modelId", _DEFAULT_MODEL_ID)
        bedrock_mock = "false" if _context_enabled(self, "channels:live", default=False) else "true"
        self.evidence_analyzer_fn = self._function(
            "EvidenceAnalyzerFunction",
            function_name="oerf-evidence-analyzer",
            handler="oerf.platform.evidence.vision.handler",
            code=lambda_code,
            timeout=Duration.seconds(60),
            environment={
                "INCIDENT_TABLE_NAME": data_stack.incident_table.table_name,
                "BEDROCK_MODEL_ID": bedrock_model_id,
                "USE_MOCK_BEDROCK": bedrock_mock,
                "GUARDRAIL_ID": self.guardrail.guardrail_id,
                "GUARDRAIL_VERSION": self.guardrail.guardrail_version,
            },
        )
        data_stack.evidence_bucket.grant_read(self.evidence_analyzer_fn)
        data_stack.incident_table.grant_read_write_data(self.evidence_analyzer_fn)
        self.evidence_analyzer_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/{bedrock_model_id}"
                ],
            )
        )
        self.evidence_analyzer_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:ApplyGuardrail"],
                resources=self.guardrail.apply_resources,
            )
        )

        # ---------------------------------------------------------
        # 3. Report Generator Lambda (ADR-006)
        # ---------------------------------------------------------
        # generator.py reads the incident and writes the report bucket.
        # It does not call SES. ADR-008 keeps channel send on the router.
        self.report_generator_fn = self._function(
            "ReportGeneratorFunction",
            function_name="oerf-report-generator",
            handler="oerf.platform.report.generator.handler",
            code=lambda_code,
            timeout=Duration.seconds(30),
            environment={
                "REPORT_BUCKET_NAME": data_stack.report_bucket.bucket_name,
                "INCIDENT_TABLE_NAME": data_stack.incident_table.table_name,
            },
        )
        data_stack.report_bucket.grant_read_write(self.report_generator_fn)
        data_stack.incident_table.grant_read_data(self.report_generator_fn)

        # Cognito app client shared by the HTTP API authorizer and by the
        # WebSocket $connect authorizer.
        user_pool_client = data_stack.user_pool.add_client(
            "OerfHttpApiClient",
            user_pool_client_name="oerf-http-api-client",
            generate_secret=False,
        )

        # ---------------------------------------------------------
        # 4. WebSocket Handler & API Gateway (ADR-010)
        # ---------------------------------------------------------
        self.ws_handler_fn = self._function(
            "WsHandlerFunction",
            function_name="oerf-ws-handler",
            handler="oerf.platform.api.ws_handler.handler",
            code=lambda_code,
            timeout=Duration.seconds(10),
            environment={
                "ROUTING_TABLE_NAME": data_stack.routing_table.table_name,
            },
        )
        data_stack.routing_table.grant_read_write_data(self.ws_handler_fn)

        # $connect authorizer. cognito-idp:GetUser is authorized by the
        # caller's access token. Cognito does not evaluate IAM for that
        # operation, so this role has no cognito-idp statement.
        # https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_GetUser.html
        self.ws_authorizer_fn = self._function(
            "WsAuthorizerFunction",
            function_name="oerf-ws-authorizer",
            handler="oerf.platform.api.ws_authorizer.handler",
            code=lambda_code,
            timeout=Duration.seconds(10),
            environment={
                "ROUTING_TABLE_NAME": data_stack.routing_table.table_name,
                "USER_POOL_ID": data_stack.user_pool.user_pool_id,
                "USER_POOL_CLIENT_ID": user_pool_client.user_pool_client_id,
            },
        )
        data_stack.routing_table.grant_read_write_data(self.ws_authorizer_fn)
        ws_authorizer = authorizers.WebSocketLambdaAuthorizer(
            "OerfWsAuthorizer",
            self.ws_authorizer_fn,
            authorizer_name="oerf-ws-authorizer",
            identity_source=["route.request.querystring.token"],
        )

        self.ws_api = apigwv2.WebSocketApi(
            self,
            "OerfWebSocketApi",
            api_name="oerf-ws-api",
            connect_route_options=apigwv2.WebSocketRouteOptions(
                integration=integrations.WebSocketLambdaIntegration(
                    "ConnectIntegration", self.ws_handler_fn
                ),
                authorizer=ws_authorizer,
            ),
            disconnect_route_options=apigwv2.WebSocketRouteOptions(
                integration=integrations.WebSocketLambdaIntegration(
                    "DisconnectIntegration", self.ws_handler_fn
                )
            ),
            default_route_options=apigwv2.WebSocketRouteOptions(
                integration=integrations.WebSocketLambdaIntegration(
                    "DefaultIntegration", self.ws_handler_fn
                )
            ),
        )

        self.ws_api.add_route(
            "subscribe",
            integration=integrations.WebSocketLambdaIntegration(
                "SubscribeIntegration", self.ws_handler_fn
            ),
        )

        self.ws_stage = apigwv2.WebSocketStage(
            self,
            "WsProdStage",
            web_socket_api=self.ws_api,
            stage_name="prod",
            auto_deploy=True,
        )

        ws_callback_url = (
            f"https://{self.ws_api.api_id}.execute-api.{self.region}.amazonaws.com/"
            f"{self.ws_stage.stage_name}"
        )

        # ---------------------------------------------------------
        # 5. DynamoDB Stream Broadcaster Lambda (ADR-010)
        # ---------------------------------------------------------
        self.stream_broadcaster_fn = self._function(
            "StreamBroadcasterFunction",
            function_name="oerf-stream-broadcaster",
            handler="oerf.platform.api.stream_broadcaster.handler",
            code=lambda_code,
            timeout=Duration.seconds(15),
            environment={
                "ROUTING_TABLE_NAME": data_stack.routing_table.table_name,
                "WEBSOCKET_ENDPOINT": ws_callback_url,
            },
        )
        data_stack.routing_table.grant_read_write_data(self.stream_broadcaster_fn)
        # PostToConnection is execute-api:ManageConnections on POST /@connections.
        self.stream_broadcaster_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["execute-api:ManageConnections"],
                resources=[
                    (
                        f"arn:aws:execute-api:{self.region}:{self.account}:"
                        f"{self.ws_api.api_id}/{self.ws_stage.stage_name}/POST/@connections/*"
                    )
                ],
            )
        )

        # DynamoEventSource adds dynamodb:ListStreams on Resource *.
        # ListStreams has no resource type. The stream read actions stay on
        # this table's stream ARN. See docs/security/iam-exceptions.md.
        # A failed batch is bisected, then sent here after 24 hours or the
        # retry limit. DynamoDB streams are not readable past 24 hours.
        self.stream_dlq = sqs.Queue(
            self,
            "StreamBroadcasterDlq",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=Duration.days(14),
        )
        # Incident META is pk TENANT#{tid}#INC#{id}, sk META. Timeline is
        # EVT#, the lock is OPEN_INCIDENT, and debounce/ingest use other
        # pk prefixes. The handler still requires the #INC# key.
        self.stream_broadcaster_fn.add_event_source(
            event_sources.DynamoEventSource(
                data_stack.incident_table,
                starting_position=lambda_.StartingPosition.LATEST,
                batch_size=10,
                retry_attempts=3,
                bisect_batch_on_error=True,
                max_record_age=Duration.hours(24),
                report_batch_item_failures=True,
                on_failure=event_sources.SqsDlq(self.stream_dlq),
                filters=[
                    lambda_.FilterCriteria.filter(
                        {
                            "dynamodb": {
                                "Keys": {
                                    "pk": {
                                        "S": lambda_.FilterRule.begins_with("TENANT#")
                                    },
                                    "sk": {"S": lambda_.FilterRule.is_equal("META")},
                                }
                            }
                        }
                    )
                ],
            )
        )

        # ---------------------------------------------------------
        # 6. Inbound topic, queue, and handler
        # ---------------------------------------------------------
        # EUM Social event destinations publish to a standard topic. FIFO is
        # not supported. No master_key: the topic is not encrypted with a
        # customer-managed KMS key, so there is no key policy to edit.
        self.inbound_topic = sns.Topic(
            self,
            "InboundTopic",
            display_name="oerf-inbound",
            fifo=False,
            enforce_ssl=True,
        )
        self.inbound_dlq = sqs.Queue(
            self,
            "InboundDlq",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=Duration.days(14),
        )
        self.inbound_queue = sqs.Queue(
            self,
            "InboundQueue",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            visibility_timeout=Duration.seconds(
                _INBOUND_TIMEOUT.to_seconds() * _VISIBILITY_MULTIPLE
            ),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=3,
                queue=self.inbound_dlq,
            ),
        )
        self.inbound_topic.add_subscription(subscriptions.SqsSubscription(self.inbound_queue))
        waba_id = _context_str(self, "whatsapp:wabaId")
        if waba_id:
            WabaEventDestination(
                self,
                "WabaEventDestination",
                topic=self.inbound_topic,
                waba_id=waba_id,
            )

        self.inbound_fn = self._function(
            "InboundFunction",
            function_name="oerf-inbound",
            handler="oerf.platform.inbound.handler.handler",
            code=lambda_code,
            timeout=_INBOUND_TIMEOUT,
            environment={
                "TENANT_TABLE_NAME": data_stack.tenant_table.table_name,
                "INCIDENT_TABLE_NAME": data_stack.incident_table.table_name,
                "ROUTING_TABLE_NAME": data_stack.routing_table.table_name,
                "PLATFORM_TABLE_NAME": data_stack.platform_table.table_name,
                "EVIDENCE_BUCKET_NAME": data_stack.evidence_bucket.bucket_name,
                "STATE_MACHINE_ARN": data_stack.cold_chain_state_machine.state_machine_arn,
                "WA_PHONE_NUMBER_ID_PARAM": _WA_PHONE_NUMBER_ID_PARAM,
                "WHATSAPP_PHONE_NUMBER_ID": whatsapp_phone_number_id,
                "INBOUND_MEDIA_MAX_BYTES": str(10 * 1024 * 1024),
                "LOG_LEVEL": "INFO",
            },
        )
        data_stack.routing_table.grant_read_write_data(self.inbound_fn)
        data_stack.incident_table.grant_read_write_data(self.inbound_fn)
        data_stack.tenant_table.grant_read_data(self.inbound_fn)
        data_stack.platform_table.grant_read_data(self.inbound_fn)
        self.inbound_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject"],
                resources=[data_stack.evidence_bucket.arn_for_objects("tenants/*/inbound/*")],
            )
        )
        # Same phone-number-id ARN as SendWhatsAppMessage. Unset context keeps
        # Resource * and is recorded in iam-exceptions.md.
        self.inbound_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["social-messaging:GetWhatsAppMessageMedia"],
                resources=whatsapp_resources,
            )
        )
        self.inbound_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    (
                        f"arn:aws:ssm:{self.region}:{self.account}:parameter"
                        "/oerf/whatsapp/phone-number-id"
                    )
                ],
            )
        )
        # Inbound is the role that resumes a paused execution (ADR-028).
        self._grant_task_token(self.inbound_fn)
        # ADR-019. StopExecution applies to an execution ARN, not the state
        # machine. The handler derives that ARN from STATE_MACHINE_ARN and the
        # execution name ingest used for StartExecution.
        data_stack.cold_chain_state_machine.grant_execution(
            self.inbound_fn, "states:StopExecution"
        )
        self.inbound_fn.add_event_source(
            event_sources.SqsEventSource(
                self.inbound_queue,
                report_batch_item_failures=True,
            )
        )

        # ---------------------------------------------------------
        # 7. REST / HTTP Action & Ingestion API (ADR-004, ADR-009)
        # ---------------------------------------------------------
        self.action_handler_fn = self._function(
            "ActionHandlerFunction",
            function_name="oerf-action-handler",
            handler="oerf.platform.api.action_handler.handler",
            code=lambda_code,
            timeout=Duration.seconds(15),
            environment={
                "INCIDENT_TABLE_NAME": data_stack.incident_table.table_name,
                "ROUTING_TABLE_NAME": data_stack.routing_table.table_name,
            },
        )
        data_stack.incident_table.grant_read_write_data(self.action_handler_fn)
        data_stack.routing_table.grant_read_write_data(self.action_handler_fn)

        self.http_api = apigwv2.HttpApi(
            self,
            "OerfHttpApi",
            api_name="oerf-http-api",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_headers=["Content-Type", "Authorization"],
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.OPTIONS,
                ],
                allow_origins=["*"],
            ),
        )

        http_authorizer = authorizers.HttpUserPoolAuthorizer(
            "OerfHttpAuthorizer",
            pool=data_stack.user_pool,
            user_pool_clients=[user_pool_client],
            identity_source=["$request.header.Authorization"],
        )

        action_integration = integrations.HttpLambdaIntegration(
            "ActionIntegration", self.action_handler_fn
        )
        self.http_api.add_routes(
            path="/incidents",
            methods=[apigwv2.HttpMethod.GET],
            integration=action_integration,
            authorizer=http_authorizer,
        )
        self.http_api.add_routes(
            path="/incidents/{id}",
            methods=[apigwv2.HttpMethod.GET],
            integration=action_integration,
            authorizer=http_authorizer,
        )

        # ---------------------------------------------------------
        # 8. Ingest: sim.reefer TemperatureReading
        # ---------------------------------------------------------
        ingest_concurrency = _context_int(
            self, "ingest:reservedConcurrency", _DEFAULT_INGEST_CONCURRENCY
        )
        self.ingest_fn = self._function(
            "IngestFunction",
            function_name="oerf-ingest",
            handler="oerf.platform.ingest.handler.handler",
            code=lambda_code,
            timeout=Duration.seconds(30),
            reserved_concurrent_executions=ingest_concurrency,
            environment={
                "TENANT_TABLE_NAME": data_stack.tenant_table.table_name,
                "INCIDENT_TABLE_NAME": data_stack.incident_table.table_name,
                "ROUTING_TABLE_NAME": data_stack.routing_table.table_name,
                "EVENT_BUS_NAME": data_stack.event_bus.event_bus_name,
                "STATE_MACHINE_ARN": data_stack.cold_chain_state_machine.state_machine_arn,
                "LOG_LEVEL": "INFO",
            },
        )
        data_stack.tenant_table.grant_read_data(self.ingest_fn)
        # Incident ids are ADD on TENANT#{tid} / COUNTER#INCIDENT. This is the
        # only write on the tenant table. IAM cannot restrict the sort key.
        self.ingest_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:UpdateItem"],
                resources=[data_stack.tenant_table.table_arn],
            )
        )
        data_stack.routing_table.grant_read_data(self.ingest_fn)
        data_stack.incident_table.grant_read_write_data(self.ingest_fn)
        # Ingest is the only caller of StartExecution. Cancel rebuilds this name.
        data_stack.cold_chain_state_machine.grant_start_execution(self.ingest_fn)

        self.ingest_dlq = sqs.Queue(
            self,
            "IngestTargetDlq",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=Duration.days(14),
        )
        temperature_rule = events.Rule(
            self,
            "TemperatureReadingRule",
            event_bus=data_stack.event_bus,
            event_pattern=events.EventPattern(
                source=["sim.reefer"],
                detail_type=["TemperatureReading"],
            ),
        )
        temperature_rule.add_target(
            targets.LambdaFunction(
                self.ingest_fn,
                dead_letter_queue=self.ingest_dlq,
                retry_attempts=2,
                max_event_age=Duration.hours(1),
            )
        )

        # ---------------------------------------------------------
        # 9. AgentCore runtime (OERF-312). Off unless agentcore:enabled.
        # ---------------------------------------------------------
        # The image build needs Docker, so CI synth leaves this out.
        self.agent_runtime_arn = None
        if _context_enabled(self, "agentcore:enabled", default=False):
            agent = AgentCoreRuntime(
                self,
                "AgentCore",
                incident_table=data_stack.incident_table,
                tenant_table=data_stack.tenant_table,
                guardrail_resources=self.guardrail.apply_resources,
                guardrail_id=self.guardrail.guardrail_id,
                guardrail_version=self.guardrail.guardrail_version,
                model_id=bedrock_model_id,
            )
            self.agent_runtime_arn = agent.agent_runtime_arn
            # Export names cannot contain underscores. The env name stays
            # AGENT_RUNTIME_ARN; this export is the cross-stack form.
            CfnOutput(
                self,
                "AgentRuntimeArn",
                value=agent.agent_runtime_arn,
                export_name="AGENT-RUNTIME-ARN",
            )

        # ---------------------------------------------------------
        # 10. Cold-chain step functions. Names are fixed so the data stack
        # can invoke them without a cross-stack reference.
        # ---------------------------------------------------------
        self._step_functions(data_stack, lambda_code)

        # ---------------------------------------------------------
        # Stack Outputs
        # ---------------------------------------------------------
        CfnOutput(self, "WebSocketApiUrl", value=self.ws_stage.url)
        CfnOutput(self, "HttpApiUrl", value=self.http_api.url or "")
        CfnOutput(self, "CognitoAppClientId", value=user_pool_client.user_pool_client_id)
        CfnOutput(self, "ChannelRouterArn", value=self.channel_router_fn.function_arn)
        CfnOutput(self, "InboundQueueUrl", value=self.inbound_queue.queue_url)
        CfnOutput(self, "EvidenceAnalyzerArn", value=self.evidence_analyzer_fn.function_arn)
        CfnOutput(self, "ReportGeneratorArn", value=self.report_generator_fn.function_arn)
        pin_source_account(self)

    def _step_functions(self, data_stack: OerfDataStack, code: lambda_.Code) -> None:
        """One function and one role per cold-chain phase."""

        # 30s stays under the 60s task timeout. 256 MiB covers the rules import.
        # Failure handlers do not read tenant config. Explicit construct ids
        # keep the error alarms named RecordFailureFunctionErrors,
        # EscalateFunctionErrors, and CancelFunctionErrors.
        failure_ids = {
            "record_failure": "RecordFailureFunction",
            "escalate": "EscalateFunction",
            "cancel": "CancelFunction",
        }
        failure_grants = {
            "record_failure": (False, False),
            "escalate": (True, False),
            "cancel": (True, True),
        }
        for phase, function_name in STEP_FUNCTION_NAMES.items():
            failure = phase in failure_grants
            construct_id = failure_ids.get(phase, f"{phase.capitalize()}Function")
            environment = {
                "INCIDENT_TABLE_NAME": data_stack.incident_table.table_name,
                "LOG_LEVEL": "INFO",
            }
            if not failure:
                environment["TENANT_TABLE_NAME"] = data_stack.tenant_table.table_name
            function = self._function(
                construct_id,
                function_name=function_name,
                handler=STEP_HANDLERS[phase],
                code=code,
                timeout=Duration.seconds(30),
                memory_size=256,
                environment=environment,
            )
            if failure:
                update, delete = failure_grants[phase]
                self._grant_step_data(
                    function,
                    data_stack,
                    update=update,
                    delete=delete,
                    tenant=False,
                )
            else:
                self._grant_step_data(
                    function,
                    data_stack,
                    update=phase not in {"qa", "diagnose"},
                    delete=phase == "close",
                    query_tenant=phase == "diagnose",
                )
        self._engage_functions(data_stack, code)

    def _grant_step_data(
        self,
        function: lambda_.Function,
        data_stack: OerfDataStack,
        *,
        update: bool,
        delete: bool,
        tenant: bool = True,
        query_tenant: bool = False,
    ) -> None:
        """The actions ``run_step`` actually calls. No table-wide grant helper."""

        incident = data_stack.incident_table.table_arn
        reads = [incident]
        if tenant:
            reads.append(data_stack.tenant_table.table_arn)
        function.add_to_role_policy(
            iam.PolicyStatement(actions=["dynamodb:GetItem"], resources=reads)
        )
        function.add_to_role_policy(
            iam.PolicyStatement(actions=["dynamodb:PutItem"], resources=[incident])
        )
        if update:
            function.add_to_role_policy(
                iam.PolicyStatement(actions=["dynamodb:UpdateItem"], resources=[incident])
            )
        if delete:
            function.add_to_role_policy(
                iam.PolicyStatement(actions=["dynamodb:DeleteItem"], resources=[incident])
            )
        if query_tenant:
            function.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["dynamodb:Query"],
                    resources=[data_stack.tenant_table.table_arn],
                )
            )

    def _engage_functions(self, data_stack: OerfDataStack, code: lambda_.Code) -> None:
        """Cadence and interpret. Neither role can send or resume a token.

        ``SendViaRouter`` invokes ``oerf-channel-router`` from the state machine.
        """

        del data_stack
        self._function(
            "CadenceFunction",
            function_name="oerf-engage-cadence",
            handler="oerf.platform.cadence.engage.handler",
            code=code,
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={"LOG_LEVEL": "INFO"},
        )
        environment = {"LOG_LEVEL": "INFO", "AGENTCORE_ENABLED": "false"}
        if self.agent_runtime_arn is not None:
            environment = {
                "LOG_LEVEL": "INFO",
                "AGENTCORE_ENABLED": "true",
                "AGENT_RUNTIME_ARN": self.agent_runtime_arn,
            }
        interpret = self._function(
            "InterpretFunction",
            function_name="oerf-engage-interpret",
            handler="oerf.platform.cadence.interpret.handler",
            code=code,
            timeout=Duration.seconds(30),
            memory_size=256,
            environment=environment,
        )
        if self.agent_runtime_arn is not None:
            interpret.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["bedrock-agentcore:InvokeAgentRuntime"],
                    resources=[self.agent_runtime_arn],
                )
            )

    def _function(
        self,
        construct_id: str,
        *,
        function_name: str,
        handler: str,
        code: lambda_.Code,
        timeout: Duration,
        environment: dict[str, str],
        memory_size: int = 128,
        reserved_concurrent_executions: int | None = None,
    ) -> lambda_.Function:
        """One role, a 30-day log group, and active X-Ray tracing."""
        log_group = logs.LogGroup(
            self,
            f"{construct_id}LogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        role = iam.Role(
            self,
            f"{construct_id}Role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        log_group.grant_write(role)
        return lambda_.Function(
            self,
            construct_id,
            function_name=function_name,
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler=handler,
            code=code,
            role=role,
            log_group=log_group,
            timeout=timeout,
            memory_size=memory_size,
            environment=environment,
            reserved_concurrent_executions=reserved_concurrent_executions,
            # tracing ACTIVE adds xray:PutTraceSegments and xray:PutTelemetryRecords
            # on Resource *. X-Ray rejects a resource ARN. See iam-exceptions.md.
            tracing=lambda_.Tracing.ACTIVE,
        )

    def _grant_task_token(self, fn: lambda_.Function) -> None:
        """Allow SendTaskSuccess. SendTaskFailure is not granted. Resource is ``*``."""
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=_TASK_TOKEN_ACTIONS,
                resources=["*"],
            )
        )
