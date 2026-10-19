"""Template assertions for the dashboard API stack. No AWS calls."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infra.stacks.api_stack import OerfApiStack
from infra.stacks.data_stack import OerfDataStack

ACCOUNT = "123456789012"
REGION = "us-east-1"


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    env = cdk.Environment(account=ACCOUNT, region=REGION)
    data = OerfDataStack(app, "OerfDataStack", env=env)
    stack = OerfApiStack(app, "OerfApiStack", data_stack=data, env=env)
    return Template.from_stack(stack)


def test_rest_routes_require_the_cognito_authorizer(template: Template) -> None:
    template.resource_count_is("AWS::ApiGateway::RestApi", 1)
    template.has_resource_properties(
        "AWS::ApiGateway::Authorizer",
        {"Type": "COGNITO_USER_POOLS"},
    )
    methods = template.find_resources("AWS::ApiGateway::Method")
    protected = [
        method
        for method in methods.values()
        if method["Properties"].get("HttpMethod") != "OPTIONS"
    ]
    assert protected
    assert all(
        method["Properties"]["AuthorizationType"] == "COGNITO_USER_POOLS" for method in protected
    )


def test_actions_use_the_fifo_queue_and_cannot_transition_state(template: Template) -> None:
    template.has_resource_properties("AWS::SQS::Queue", {"FifoQueue": True})
    rendered = str(template.to_json())
    assert "states:SendTaskSuccess" not in rendered
    assert "states:StartExecution" not in rendered
    assert "sqs:SendMessage" in rendered


def test_dashboard_bucket_is_private_behind_cloudfront(template: Template) -> None:
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            }
        },
    )
    template.resource_count_is("AWS::CloudFront::Distribution", 1)
    template.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)


def test_websocket_routes_and_outputs_exist(template: Template) -> None:
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Api",
        {"ProtocolType": "WEBSOCKET"},
    )
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    keys = {route["Properties"]["RouteKey"] for route in routes.values()}
    assert {"$connect", "$disconnect", "$default", "subscribe"} <= keys
    outputs = template.find_outputs("*")
    names = set(outputs)
    assert {"DashboardUrl", "RestApiUrl", "WebSocketUrl", "ActionQueueUrl"} <= names
