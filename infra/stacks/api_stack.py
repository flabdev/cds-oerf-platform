"""Dashboard API, private hosting, and the WebSocket the React app uses.

Stack registration in ``infra/app.py`` belongs to the application-stack owner.
This module is synthesized on its own until that registration lands.
"""

from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
    aws_apigateway as apigw,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_authorizers as authorizers,
    aws_apigatewayv2_integrations as integrations,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_lambda as lambda_,
    aws_s3 as s3,
    aws_sqs as sqs,
)
from constructs import Construct

from infra.stacks.data_stack import OerfDataStack

_LAMBDA_EXCLUDE = [
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
]


class OerfApiStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        data_stack: OerfDataStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        code = lambda_.Code.from_asset(".", exclude=_LAMBDA_EXCLUDE)
        self.action_queue = sqs.Queue(
            self,
            "DashboardActionQueue",
            fifo=True,
            content_based_deduplication=False,
        )
        self.query_fn = lambda_.Function(
            self,
            "DashboardQueryFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="oerf.platform.api.dashboard.handler",
            code=code,
            timeout=Duration.seconds(15),
            environment={
                "INCIDENT_TABLE_NAME": data_stack.incident_table.table_name,
                "TENANT_TABLE_NAME": data_stack.tenant_table.table_name,
            },
        )
        data_stack.incident_table.grant_read_data(self.query_fn)
        data_stack.tenant_table.grant_read_write_data(self.query_fn)
        self.action_fn = lambda_.Function(
            self,
            "DashboardActionFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="oerf.platform.api.dashboard.handler",
            code=code,
            timeout=Duration.seconds(10),
            environment={
                "ACTION_QUEUE_URL": self.action_queue.queue_url,
                "INCIDENT_TABLE_NAME": data_stack.incident_table.table_name,
            },
        )
        self.action_queue.grant_send_messages(self.action_fn)
        data_stack.incident_table.grant_read_data(self.action_fn)

        authorizer = apigw.CognitoUserPoolsAuthorizer(
            self,
            "DashboardCognitoAuthorizer",
            cognito_user_pools=[data_stack.user_pool],
        )
        authorization = {
            "authorizer": authorizer,
            "authorization_type": apigw.AuthorizationType.COGNITO,
        }
        self.rest_api = apigw.RestApi(
            self,
            "OerfDashboardRestApi",
            rest_api_name="oerf-dashboard",
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=["*"],
                allow_methods=["GET", "POST", "PUT", "OPTIONS"],
                allow_headers=["Content-Type", "Authorization", "Idempotency-Key"],
            ),
        )
        query_integration = apigw.LambdaIntegration(self.query_fn, proxy=True)
        action_integration = apigw.LambdaIntegration(self.action_fn, proxy=True)
        incidents = self.rest_api.root.add_resource("incidents")
        incidents.add_method("GET", query_integration, **authorization)
        one = incidents.add_resource("{id}")
        one.add_method("GET", query_integration, **authorization)
        one.add_resource("action").add_method("POST", action_integration, **authorization)
        admin = self.rest_api.root.add_resource("admin")
        tenants = admin.add_resource("tenants")
        tenants.add_method("GET", query_integration, **authorization)
        tenants.add_method("POST", query_integration, **authorization)
        tenant = tenants.add_resource("{tid}")
        for name in ("contacts", "facilities", "faultcodes"):
            resource = tenant.add_resource(name)
            resource.add_method("GET", query_integration, **authorization)
            resource.add_method("PUT", query_integration, **authorization)
        domain = tenant.add_resource("domains").add_resource("{domain}")
        config = domain.add_resource("config")
        config.add_method("GET", query_integration, **authorization)
        config.add_method("PUT", query_integration, **authorization)
        config.add_resource("validate").add_method("POST", query_integration, **authorization)
        config.add_resource("publish").add_method("POST", query_integration, **authorization)
        cadence = domain.add_resource("cadence")
        cadence.add_method("GET", query_integration, **authorization)
        cadence.add_method("PUT", query_integration, **authorization)

        self.dashboard_bucket = s3.Bucket(
            self,
            "DashboardBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
        )
        self.distribution = cloudfront.Distribution(
            self,
            "DashboardDistribution",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(self.dashboard_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
        )
        self.hosted_domain = cognito.UserPoolDomain(
            self,
            "DashboardHostedDomain",
            user_pool=data_stack.user_pool,
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=f"oerf-{self.account}"),
        )
        site = f"https://{self.distribution.distribution_domain_name}"
        self.user_pool_client = cognito.UserPoolClient(
            self,
            "DashboardUserPoolClient",
            user_pool=data_stack.user_pool,
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=[site, "http://localhost:5173", "http://127.0.0.1:5173"],
                logout_urls=[site, "http://localhost:5173", "http://127.0.0.1:5173"],
            ),
        )
        self.user_pool_client.node.add_dependency(self.hosted_domain)
        self._websocket(data_stack, code)
        CfnOutput(
            self,
            "CognitoDomain",
            value=f"https://oerf-{self.account}.auth.{self.region}.amazoncognito.com",
        )
        CfnOutput(self, "UserPoolClientId", value=self.user_pool_client.user_pool_client_id)
        CfnOutput(self, "UserPoolId", value=data_stack.user_pool.user_pool_id)
        CfnOutput(
            self,
            "DashboardUrl",
            value=f"https://{self.distribution.distribution_domain_name}",
        )
        CfnOutput(self, "RestApiUrl", value=self.rest_api.url)
        CfnOutput(self, "WebSocketUrl", value=self.ws_stage.url)
        CfnOutput(self, "ActionQueueUrl", value=self.action_queue.queue_url)

    def _websocket(self, data_stack: OerfDataStack, code: lambda_.Code) -> None:
        self.ws_handler_fn = lambda_.Function(
            self,
            "DashboardWsFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="oerf.platform.api.ws_handler.handler",
            code=code,
            timeout=Duration.seconds(10),
            environment={"ROUTING_TABLE_NAME": data_stack.routing_table.table_name},
        )
        data_stack.routing_table.grant_read_write_data(self.ws_handler_fn)
        self.ws_authorizer_fn = lambda_.Function(
            self,
            "DashboardWsAuthorizerFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="oerf.platform.api.ws_authorizer.handler",
            code=code,
            timeout=Duration.seconds(10),
            environment={
                "ROUTING_TABLE_NAME": data_stack.routing_table.table_name,
                "USER_POOL_ID": data_stack.user_pool.user_pool_id,
                "USER_POOL_CLIENT_ID": self.user_pool_client.user_pool_client_id,
            },
        )
        data_stack.routing_table.grant_read_write_data(self.ws_authorizer_fn)
        ws_authorizer = authorizers.WebSocketLambdaAuthorizer(
            "DashboardWsAuthorizer",
            self.ws_authorizer_fn,
            identity_source=["route.request.querystring.token"],
        )
        integration = integrations.WebSocketLambdaIntegration(
            "DashboardWsIntegration", self.ws_handler_fn
        )
        self.ws_api = apigwv2.WebSocketApi(
            self,
            "DashboardWebSocket",
            api_name="oerf-dashboard-ws",
            connect_route_options=apigwv2.WebSocketRouteOptions(
                integration=integration,
                authorizer=ws_authorizer,
            ),
            disconnect_route_options=apigwv2.WebSocketRouteOptions(integration=integration),
            default_route_options=apigwv2.WebSocketRouteOptions(integration=integration),
        )
        self.ws_api.add_route("subscribe", integration=integration)
        self.ws_stage = apigwv2.WebSocketStage(
            self,
            "DashboardWsStage",
            web_socket_api=self.ws_api,
            stage_name="prod",
            auto_deploy=True,
        )
