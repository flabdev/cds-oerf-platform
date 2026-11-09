"""Dashboard API and private hosting.

Actions go to the compute stack's inbound queue. ``WebSocketUrl`` is that
stack's WebSocket endpoint. Stack registration lives in ``infra/app.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

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
    aws_s3_deployment as s3deploy,
    aws_sqs as sqs,
    aws_ssm as ssm,
)
from constructs import Construct

from infra.iam_conditions import pin_source_account
from infra.naming import Stage, qualify_dev_names
from infra.runtime_asset import runtime_code_args
from infra.stacks.data_stack import OerfDataStack

_DEFAULT_THROTTLE_RATE = 20
_DEFAULT_THROTTLE_BURST = 40
# The deployment Lambda shells out to the AWS CLI. 1024 MiB is that
# construct's own default; the desk build is small, and a smaller size has
# failed the CLI on a cold start. Every desk deployment passes the same value
# because they share one function.
_DESK_PUBLISH_MEMORY_MIB = 1024
_DESK_BUILD_MESSAGE = "build web/ first: cd web && npm ci && npm run build"
# Repo root is two levels above this file (infra/stacks/api_stack.py).
DESK_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"
# Checked-in branding export. Absent until someone saves it from the console.
MANAGED_LOGIN_BRANDING = (
    Path(__file__).resolve().parents[1] / "config" / "managed-login-branding.json"
)
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
        *,
        stage: Stage,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.stage = stage
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
        # because the role is account-wide. Dev does not create a second
        # AWS::ApiGateway::Account. Its access logs use the role main created.
        account_role: dict[str, object] = {"cloud_watch_role": stage.is_main}
        if stage.is_main:
            account_role["cloud_watch_role_removal_policy"] = RemovalPolicy.RETAIN
        self.rest_api = apigw.RestApi(
            self,
            "OerfDashboardRestApi",
            rest_api_name=stage.res("dashboard"),
            **account_role,
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

        domain_prefix = stage.res(self.account)
        # ManagedLoginVersion is "Update requires: No interruption". Domain and
        # UserPoolId are Replacement, so this flag must not change either of them.
        # The pool tier is already Essentials on OerfUserPool (also no interruption).
        domain_version = {}
        if _managed_login_enabled(self):
            domain_version["managed_login_version"] = (
                cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN
            )
        self.hosted_domain = cognito.UserPoolDomain(
            self,
            "DashboardHostedDomain",
            user_pool=data_stack.user_pool,
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=domain_prefix),
            **domain_version,
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
        if _managed_login_enabled(self):
            # The style references the client. It also waits for the domain so
            # version 2 exists before Cognito accepts the branding resource.
            branding = _managed_login_branding(
                self,
                user_pool_id=data_stack.user_pool.user_pool_id,
                client_id=self.user_pool_client.user_pool_client_id,
            )
            branding.node.add_dependency(self.hosted_domain)
        # The WebSocket authorizer lives in OerfComputeStack, which this stack
        # depends on. Publishing the client id lets that function allow the
        # hosted-UI client without a reference back to this stack.
        ssm.StringParameter(
            self,
            "DashboardClientIdParameter",
            parameter_name=stage.ssm("ws-allowed-clients/dashboard"),
            string_value=self.user_pool_client.user_pool_client_id,
        )
        CfnOutput(
            self,
            "CognitoDomain",
            value=f"https://{domain_prefix}.auth.{self.region}.amazoncognito.com",
        )
        CfnOutput(self, "UserPoolClientId", value=self.user_pool_client.user_pool_client_id)
        CfnOutput(self, "UserPoolId", value=data_stack.user_pool.user_pool_id)
        CfnOutput(self, "DashboardUrl", value=dashboard_origin)
        CfnOutput(self, "RestApiUrl", value=self.rest_api.url)
        CfnOutput(self, "WebSocketUrl", value=websocket_url)
        # Off unless both dashboard:enabled (this stack exists) and desk:publish.
        # Main templates stay as they are when the flag is omitted.
        if _desk_publish_enabled(self):
            _publish_desk(
                self,
                data_stack=data_stack,
                websocket_url=websocket_url,
                domain_prefix=domain_prefix,
                dashboard_origin=dashboard_origin,
            )
        pin_source_account(self)
        if not stage.is_main:
            qualify_dev_names(self)


def _desk_publish_enabled(scope: Construct) -> bool:
    return scope.node.try_get_context("desk:publish") == "true"


def _managed_login_enabled(scope: Construct) -> bool:
    return scope.node.try_get_context("auth:managedLogin") == "true"


def _managed_login_branding(
    stack: OerfApiStack,
    *,
    user_pool_id: str,
    client_id: str,
) -> cognito.CfnManagedLoginBranding:
    """One style for the dashboard client.

    ``UseCognitoProvidedValues: true`` is Cognito's default style. CloudFormation
    requires that request to omit ``Settings`` and ``Assets``, so the logo cannot
    ride along with the default. A checked-in export supplies both and turns the
    default off.
    """

    settings, assets = _branding_document(MANAGED_LOGIN_BRANDING)
    if settings is None:
        return cognito.CfnManagedLoginBranding(
            stack,
            "DashboardManagedLoginBranding",
            user_pool_id=user_pool_id,
            client_id=client_id,
            use_cognito_provided_values=True,
        )
    return cognito.CfnManagedLoginBranding(
        stack,
        "DashboardManagedLoginBranding",
        user_pool_id=user_pool_id,
        client_id=client_id,
        use_cognito_provided_values=False,
        settings=settings,
        assets=assets,
    )


def _branding_document(path: Path) -> tuple[object | None, list | None]:
    """Return ``(Settings, Assets)``. ``(None, None)`` when the file is absent."""

    if not path.is_file():
        return None, None
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or "Settings" not in document:
        raise ValueError(
            f"{path.name} must be a JSON object with Settings, and Assets when images were exported"
        )
    raw_assets = document.get("Assets")
    if raw_assets is None:
        return document["Settings"], None
    if not isinstance(raw_assets, list):
        raise ValueError(f"{path.name} Assets must be a list")
    return document["Settings"], [_branding_asset(item) for item in raw_assets]


def _branding_asset(item: object) -> cognito.CfnManagedLoginBranding.AssetTypeProperty:
    if not isinstance(item, dict):
        raise ValueError("each branding asset must be an object")
    try:
        category = item["Category"]
        color_mode = item["ColorMode"]
        extension = item["Extension"]
    except KeyError as error:
        raise ValueError(
            "each branding asset needs Category, ColorMode, and Extension"
        ) from error
    return cognito.CfnManagedLoginBranding.AssetTypeProperty(
        category=category,
        color_mode=color_mode,
        extension=extension,
        bytes=item.get("Bytes"),
        resource_id=item.get("ResourceId"),
    )


def require_desk_build(dist: Path) -> None:
    """Stop synth until ``web/dist`` has a Vite build."""

    ready = (dist / "index.html").is_file() and (dist / "assets").is_dir()
    if not ready:
        raise ValueError(_DESK_BUILD_MESSAGE)


def _publish_desk(
    stack: OerfApiStack,
    *,
    data_stack: OerfDataStack,
    websocket_url: str,
    domain_prefix: str,
    dashboard_origin: str,
) -> None:
    """Upload ``web/dist`` and write ``config.json`` from this stack.

    Hashed files under ``assets/`` are pruned only inside that prefix, so the
    prune cannot delete ``config.json`` or ``index.html``. The page deployment
    prunes other root objects and excludes ``config.json`` and ``assets/*``.
    The config deployment does not prune. It runs last and invalidates ``/*``
    after both uploads, so CloudFront does not keep a missing ``config.json``.
    ``retain_on_delete`` keeps the objects when a later deploy omits the flag.
    """

    require_desk_build(DESK_DIST)
    log_group = logs.LogGroup(
        stack,
        "DeskPublishLogGroup",
        retention=logs.RetentionDays.ONE_MONTH,
        removal_policy=RemovalPolicy.DESTROY,
    )
    shared = {
        "destination_bucket": stack.dashboard_bucket,
        "memory_limit": _DESK_PUBLISH_MEMORY_MIB,
        "log_group": log_group,
        # A later deploy without desk:publish removes these resources.
        # Retaining leaves index.html, assets, and config.json in the bucket.
        "retain_on_delete": True,
    }
    immutable = [
        s3deploy.CacheControl.set_public(),
        s3deploy.CacheControl.max_age(Duration.days(365)),
        s3deploy.CacheControl.immutable(),
    ]
    no_cache = [s3deploy.CacheControl.no_cache()]
    assets = s3deploy.BucketDeployment(
        stack,
        "DeskAssetsDeployment",
        sources=[s3deploy.Source.asset(str(DESK_DIST / "assets"))],
        destination_key_prefix="assets",
        cache_control=immutable,
        prune=True,
        exclude=["config.json"],
        **shared,
    )
    # distribution grants cloudfront:CreateInvalidation and
    # cloudfront:GetInvalidation on Resource *. CDK does not scope it.
    # See iam-exceptions.md. The assets deployment does not take one.
    pages = s3deploy.BucketDeployment(
        stack,
        "DeskPagesDeployment",
        sources=[
            s3deploy.Source.asset(
                str(DESK_DIST),
                exclude=["config.json", "assets", "assets/**"],
            )
        ],
        cache_control=no_cache,
        prune=True,
        # Sync filters, not the asset exclude. These keys stay when prune runs.
        exclude=["config.json", "assets/*"],
        distribution=stack.distribution,
        distribution_paths=["/*"],
        **shared,
    )
    # Host only. The CognitoDomain output is this host with an https:// prefix.
    # userPoolClientId is UserPoolClientId, the client the WebSocket authorizer
    # allows via the dashboard SSM parameter.
    config = s3deploy.BucketDeployment(
        stack,
        "DeskConfigDeployment",
        sources=[
            s3deploy.Source.json_data(
                "config.json",
                {
                    "apiUrl": stack.rest_api.url,
                    "cognitoDomain": (
                        f"{domain_prefix}.auth.{stack.region}.amazoncognito.com"
                    ),
                    "userPoolClientId": stack.user_pool_client.user_pool_client_id,
                    "wsUrl": websocket_url,
                    "region": stack.region,
                    "userPoolId": data_stack.user_pool.user_pool_id,
                },
            )
        ],
        content_type="application/json",
        cache_control=no_cache,
        prune=False,
        distribution=stack.distribution,
        distribution_paths=["/*"],
        **shared,
    )
    config.node.add_dependency(assets)
    config.node.add_dependency(pages)
    CfnOutput(stack, "DeskConfigUrl", value=f"{dashboard_origin}/config.json")
