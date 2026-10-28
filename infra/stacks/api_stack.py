"""Dashboard API and private hosting.

Actions go to the compute stack's inbound queue. ``WebSocketUrl`` is that
stack's WebSocket endpoint. Stack registration lives in ``infra/app.py``.
"""

from __future__ import annotations

import json

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigw,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_sqs as sqs,
    aws_ssm as ssm,
)
from constructs import Construct

from infra.iam_conditions import pin_source_account
from infra.runtime_asset import runtime_code_args
from infra.stacks.data_stack import OerfDataStack
from infra.ws_clients import DASHBOARD_CLIENT_PARAMETER

_DEFAULT_THROTTLE_RATE = 20
_DEFAULT_THROTTLE_BURST = 40
_ACCESS_LOG = json.dumps(
    {
        "requestId": "$context.requestId",
        "ip": "$context.identity.sourceIp",
        "httpMethod": "$context.httpMethod",
        "resourcePath": "$context.resourcePath",
        "status": "$context.status",
        "responseLatency": "$context.responseLatency",
        "sub": "$context.authorizer.claims.sub",
    },
    separators=(",", ":"),
)


def _context_int(scope: Construct, key: str, default: int) -> int:
    raw = scope.node.try_get_context(key)
    if raw is None or raw == "":
        return default
    return int(raw)


def _dashboard_function(stack: Stack, construct_id: str, **kwargs: object) -> lambda_.Function:
    """One role, a 30-day log group, and active X-Ray tracing."""

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
        role=role,
        log_group=log_group,
        # tracing ACTIVE adds xray:PutTraceSegments and xray:PutTelemetryRecords
        # on Resource *. X-Ray rejects a resource ARN. See iam-exceptions.md.
        tracing=lambda_.Tracing.ACTIVE,
        **kwargs,
    )


class OerfApiStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        data_stack: OerfDataStack,
        inbound_queue: sqs.IQueue,
        websocket_url: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        code = lambda_.Code.from_asset(".", **runtime_code_args())
        self.query_fn = _dashboard_function(
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
        self.action_fn = _dashboard_function(
            self,
            "DashboardActionFunction",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="oerf.platform.api.dashboard.handler",
            code=code,
            timeout=Duration.seconds(10),
            environment={
                "INBOUND_QUEUE_URL": inbound_queue.queue_url,
                "INCIDENT_TABLE_NAME": data_stack.incident_table.table_name,
            },
        )
        self.action_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["sqs:SendMessage"],
                resources=[inbound_queue.queue_arn],
            )
        )
        data_stack.incident_table.grant_read_data(self.action_fn)

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
                response_headers_policy=cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
            ),
        )
        dashboard_origin = f"https://{self.distribution.distribution_domain_name}"

        authorizer = apigw.CognitoUserPoolsAuthorizer(
            self,
            "DashboardCognitoAuthorizer",
            cognito_user_pools=[data_stack.user_pool],
        )
        authorization = {
            "authorizer": authorizer,
            "authorization_type": apigw.AuthorizationType.COGNITO,
        }
        access_logs = logs.LogGroup(
            self,
            "DashboardRestAccessLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        # Access logging needs the account CloudWatch role. CDK grants that
        # role when cloud_watch_role is true. Deleting this stack retains it,
        # because the role is account-wide.
        self.rest_api = apigw.RestApi(
            self,
            "OerfDashboardRestApi",
            rest_api_name="oerf-dashboard",
            cloud_watch_role=True,
            cloud_watch_role_removal_policy=RemovalPolicy.RETAIN,
            deploy_options=apigw.StageOptions(
                throttling_rate_limit=_context_int(
                    self, "api:throttleRate", _DEFAULT_THROTTLE_RATE
                ),
                throttling_burst_limit=_context_int(
                    self, "api:throttleBurst", _DEFAULT_THROTTLE_BURST
                ),
                access_log_destination=apigw.LogGroupLogDestination(access_logs),
                access_log_format=apigw.AccessLogFormat.custom(_ACCESS_LOG),
            ),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=[dashboard_origin],
                allow_methods=["GET", "POST", "PUT", "OPTIONS"],
                allow_headers=["Content-Type", "Authorization", "Idempotency-Key"],
            ),
        )
        # A Cognito authorizer rejection never reaches the Lambda. Those
        # gateway responses omit the preflight headers, so the browser reports
        # the 401 as a CORS failure.
        cors_headers = {
            "Access-Control-Allow-Origin": f"'{dashboard_origin}'",
            "Access-Control-Allow-Headers": "'Content-Type,Authorization,Idempotency-Key'",
            "Access-Control-Allow-Methods": "'GET,POST,PUT,OPTIONS'",
        }
        self.rest_api.add_gateway_response(
            "UnauthorizedCors",
            type=apigw.ResponseType.UNAUTHORIZED,
            response_headers=cors_headers,
        )
        self.rest_api.add_gateway_response(
            "AccessDeniedCors",
            type=apigw.ResponseType.ACCESS_DENIED,
            response_headers=cors_headers,
        )
        self.query_fn.add_environment("DASHBOARD_ORIGIN", dashboard_origin)
        self.action_fn.add_environment("DASHBOARD_ORIGIN", dashboard_origin)
        query_integration = apigw.LambdaIntegration(self.query_fn, proxy=True)
        action_integration = apigw.LambdaIntegration(self.action_fn, proxy=True)
        incidents = self.rest_api.root.add_resource("incidents")
        incidents.add_method("GET", query_integration, **authorization)
        one = incidents.add_resource("{id}")
        one.add_method("GET", query_integration, **authorization)
        one.add_resource("actions").add_method("POST", action_integration, **authorization)
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

        self.hosted_domain = cognito.UserPoolDomain(
            self,
            "DashboardHostedDomain",
            user_pool=data_stack.user_pool,
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=f"oerf-{self.account}"),
        )
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
                    # GetUser, which the WebSocket authorizer calls, requires this scope.
                    cognito.OAuthScope.COGNITO_ADMIN,
                ],
                callback_urls=[dashboard_origin, "http://localhost:5173", "http://127.0.0.1:5173"],
                logout_urls=[dashboard_origin, "http://localhost:5173", "http://127.0.0.1:5173"],
            ),
        )
        self.user_pool_client.node.add_dependency(self.hosted_domain)
        # The WebSocket authorizer lives in OerfComputeStack, which this stack
        # depends on. Publishing the client id lets that function allow the
        # hosted-UI client without a reference back to this stack.
        ssm.StringParameter(
            self,
            "DashboardClientIdParameter",
            parameter_name=DASHBOARD_CLIENT_PARAMETER,
            string_value=self.user_pool_client.user_pool_client_id,
        )
        CfnOutput(
            self,
            "CognitoDomain",
            value=f"https://oerf-{self.account}.auth.{self.region}.amazoncognito.com",
        )
        CfnOutput(self, "UserPoolClientId", value=self.user_pool_client.user_pool_client_id)
        CfnOutput(self, "UserPoolId", value=data_stack.user_pool.user_pool_id)
        CfnOutput(self, "DashboardUrl", value=dashboard_origin)
        CfnOutput(self, "RestApiUrl", value=self.rest_api.url)
        CfnOutput(self, "WebSocketUrl", value=websocket_url)
        pin_source_account(self)
