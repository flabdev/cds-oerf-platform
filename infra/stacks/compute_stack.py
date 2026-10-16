"""Compute, API Gateway, and Lambda Stack for OERF Platform.

Provisions:
- WebSocket API Gateway (oerf-ws-api) for real-time live dashboard updates (ADR-010).
- HTTP API Gateway (oerf-http-api) for Human-in-the-loop decisions & incident ingestion.
- Lambda functions:
    1. ChannelRouterLambda (ADR-008)
    2. EvidenceAnalyzerLambda (Bedrock Claude 3.5 Sonnet Vision - ADR-006)
    3. StreamBroadcasterLambda (DynamoDB Streams to WebSocket - ADR-010)
    4. ActionHandlerLambda (REST API & SFN callback - ADR-004)
    5. WsHandlerLambda (WebSocket connect/disconnect/subscribe)
    6. ReportGeneratorLambda (ADR-006, ADR-008)
"""

from __future__ import annotations

import os

from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_authorizers as authorizers,
    aws_apigatewayv2_integrations as integrations,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_event_sources as event_sources,
)
from constructs import Construct

from infra.stacks.data_stack import OerfDataStack


class OerfComputeStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        data_stack: OerfDataStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---------------------------------------------------------
        # Shared Lambda Code Asset (packaged from root to preserve
        # oerf.* package structure and avoid shadowing stdlib platform)
        # ---------------------------------------------------------
        lambda_code = lambda_.Code.from_asset(
            ".",
            exclude=[
                "cdk.out",
                ".git",
                ".github",
                ".venv",
                "venv",
                "tests",
                "web",
                "sim",
                "node_modules",
                "**/*.pyc",
                "**/__pycache__",
                ".pytest_cache",
                "*.md",
                "cds_oerf_platform.egg-info",
            ],
        )

        # ---------------------------------------------------------
        # 1. Channel Router Lambda (ADR-008)
        # ---------------------------------------------------------
        self.channel_router_fn = lambda_.Function(
            self,
            "ChannelRouterFunction",
            function_name="oerf-channel-router",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="oerf.platform.channels.router.handler",
            code=lambda_code,
            timeout=Duration.seconds(30),
            environment={
                "ROUTING_TABLE_NAME": data_stack.routing_table.table_name,
                "USE_MOCK_WHATSAPP": "false",
                "USE_MOCK_SMS": "false",
                "WHATSAPP_PHONE_NUMBER_ID": os.environ.get("WHATSAPP_PHONE_NUMBER_ID", ""),
                "SES_SOURCE_EMAIL": os.environ.get("SES_SOURCE_EMAIL", ""),
                "USE_MOCK_EMAIL": "false",
            },
        )
        data_stack.routing_table.grant_read_write_data(self.channel_router_fn)
        self.channel_router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail", "sns:Publish", "socialmessaging:*"],
                resources=["*"],
            )
        )

        # ---------------------------------------------------------
        # 2. Bedrock Vision Evidence Extractor Lambda (ADR-006)
        # ---------------------------------------------------------
        self.evidence_analyzer_fn = lambda_.Function(
            self,
            "EvidenceAnalyzerFunction",
            function_name="oerf-evidence-analyzer",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="oerf.platform.evidence.vision.handler",
            code=lambda_code,
            timeout=Duration.seconds(60),
            environment={
                "INCIDENT_TABLE_NAME": data_stack.incident_table.table_name,
                "BEDROCK_MODEL_ID": "anthropic.claude-3-5-sonnet-20241022-v2:0",
                "USE_MOCK_BEDROCK": "false",
            },
        )
        data_stack.evidence_bucket.grant_read(self.evidence_analyzer_fn)
        data_stack.incident_table.grant_read_write_data(self.evidence_analyzer_fn)
        self.evidence_analyzer_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=["*"],
            )
        )

        # ---------------------------------------------------------
        # 3. Report Generator Lambda (ADR-006)
        # ---------------------------------------------------------
        self.report_generator_fn = lambda_.Function(
            self,
            "ReportGeneratorFunction",
            function_name="oerf-report-generator",
            runtime=lambda_.Runtime.PYTHON_3_11,
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
        self.report_generator_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail"],
                resources=["*"],
            )
        )

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
        self.ws_handler_fn = lambda_.Function(
            self,
            "WsHandlerFunction",
            function_name="oerf-ws-handler",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="oerf.platform.api.ws_handler.handler",
            code=lambda_code,
            timeout=Duration.seconds(10),
            environment={
                "ROUTING_TABLE_NAME": data_stack.routing_table.table_name,
            },
        )
        data_stack.routing_table.grant_read_write_data(self.ws_handler_fn)

        # $connect authorizer: verifies the Cognito access token supplied as the
        # ?token= query parameter and binds the connection to its tenant.
        self.ws_authorizer_fn = lambda_.Function(
            self,
            "WsAuthorizerFunction",
            function_name="oerf-ws-authorizer",
            runtime=lambda_.Runtime.PYTHON_3_11,
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
        self.ws_authorizer_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cognito-idp:GetUser"],
                resources=[data_stack.user_pool.user_pool_arn],
            )
        )

        ws_authorizer = authorizers.WebSocketLambdaAuthorizer(
            "OerfWsAuthorizer",
            self.ws_authorizer_fn,
            authorizer_name="oerf-ws-authorizer",
            identity_source=["route.request.querystring.token"],
        )

        # Create WebSocket API
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

        ws_callback_url = f"https://{self.ws_api.api_id}.execute-api.{self.region}.amazonaws.com/{self.ws_stage.stage_name}"

        # ---------------------------------------------------------
        # 5. DynamoDB Stream Broadcaster Lambda (ADR-010)
        # ---------------------------------------------------------
        self.stream_broadcaster_fn = lambda_.Function(
            self,
            "StreamBroadcasterFunction",
            function_name="oerf-stream-broadcaster",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="oerf.platform.api.stream_broadcaster.handler",
            code=lambda_code,
            timeout=Duration.seconds(15),
            environment={
                "ROUTING_TABLE_NAME": data_stack.routing_table.table_name,
                "WEBSOCKET_ENDPOINT": ws_callback_url,
            },
        )
        data_stack.routing_table.grant_read_write_data(self.stream_broadcaster_fn)
        self.stream_broadcaster_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["execute-api:ManageConnections"],
                resources=[f"arn:aws:execute-api:{self.region}:{self.account}:{self.ws_api.api_id}/*"],
            )
        )

        # Hook up DynamoDB Stream on IncidentTable to Stream Broadcaster
        self.stream_broadcaster_fn.add_event_source(
            event_sources.DynamoEventSource(
                data_stack.incident_table,
                starting_position=lambda_.StartingPosition.LATEST,
                batch_size=10,
                retry_attempts=3,
            )
        )

        # ---------------------------------------------------------
        # 6. REST / HTTP Action & Ingestion API (ADR-004, ADR-009)
        # ---------------------------------------------------------
        self.action_handler_fn = lambda_.Function(
            self,
            "ActionHandlerFunction",
            function_name="oerf-action-handler",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="oerf.platform.api.action_handler.handler",
            code=lambda_code,
            timeout=Duration.seconds(15),
            environment={
                "INCIDENT_TABLE_NAME": data_stack.incident_table.table_name,
                "ROUTING_TABLE_NAME": data_stack.routing_table.table_name,
                "STATE_MACHINE_ARN": data_stack.cold_chain_state_machine.state_machine_arn,
            },
        )
        data_stack.incident_table.grant_read_write_data(self.action_handler_fn)
        data_stack.routing_table.grant_read_write_data(self.action_handler_fn)
        data_stack.cold_chain_state_machine.grant_start_execution(self.action_handler_fn)
        self.action_handler_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["states:SendTaskSuccess", "states:SendTaskFailure"],
                resources=["*"],
            )
        )

        # HTTP API
        self.http_api = apigwv2.HttpApi(
            self,
            "OerfHttpApi",
            api_name="oerf-http-api",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_headers=["Content-Type", "Authorization"],
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.OPTIONS,
                ],
                allow_origins=["*"],
            ),
        )

        # Add UserPoolClient to UserPool for HTTP API Authorizer
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
            path="/incidents/trigger",
            methods=[apigwv2.HttpMethod.POST],
            integration=action_integration,
            authorizer=http_authorizer,
        )
        self.http_api.add_routes(
            path="/incidents/{id}/action",
            methods=[apigwv2.HttpMethod.POST],
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
        # Stack Outputs
        # ---------------------------------------------------------
        CfnOutput(self, "WebSocketApiUrl", value=self.ws_stage.url)
        CfnOutput(self, "HttpApiUrl", value=self.http_api.url or "")
        CfnOutput(self, "CognitoAppClientId", value=user_pool_client.user_pool_client_id)
        CfnOutput(self, "ChannelRouterArn", value=self.channel_router_fn.function_arn)
        CfnOutput(self, "EvidenceAnalyzerArn", value=self.evidence_analyzer_fn.function_arn)
        CfnOutput(self, "ReportGeneratorArn", value=self.report_generator_fn.function_arn)
