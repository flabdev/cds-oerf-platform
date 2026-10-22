"""Template assertions for the dashboard API stack. No AWS calls."""

import aws_cdk as cdk
import pytest
from aws_cdk import aws_sqs as sqs
from aws_cdk.assertions import Template

from infra.stacks.api_stack import OerfApiStack
from infra.stacks.data_stack import OerfDataStack

ACCOUNT = "123456789012"
REGION = "us-east-1"
WEBSOCKET_URL = "wss://abc.execute-api.us-east-1.amazonaws.com/prod"


@pytest.fixture(scope="module")
def templates() -> tuple[Template, Template]:
    app = cdk.App()
    env = cdk.Environment(account=ACCOUNT, region=REGION)
    data = OerfDataStack(app, "OerfDataStack", env=env)
    refs = cdk.Stack(app, "InboundRefs", env=env)
    queue = sqs.Queue(refs, "InboundQueue")
    stack = OerfApiStack(
        app,
        "OerfApiStack",
        data_stack=data,
        inbound_queue=queue,
        websocket_url=WEBSOCKET_URL,
        env=env,
    )
    return Template.from_stack(stack), Template.from_stack(refs)


@pytest.fixture(scope="module")
def template(templates: tuple[Template, Template]) -> Template:
    return templates[0]


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
    resources = template.find_resources("AWS::ApiGateway::Resource")
    actions_id = next(
        logical
        for logical, resource in resources.items()
        if resource["Properties"].get("PathPart") == "actions"
    )
    assert all(
        resource["Properties"].get("PathPart") != "action" for resource in resources.values()
    )
    posts = [
        method
        for method in template.find_resources("AWS::ApiGateway::Method").values()
        if method["Properties"].get("HttpMethod") == "POST"
        and method["Properties"].get("ResourceId", {}).get("Ref") == actions_id
    ]
    assert len(posts) == 1


def test_actions_enqueue_on_the_inbound_queue_and_cannot_transition_state(
    templates: tuple[Template, Template],
) -> None:
    template, refs = templates
    template.resource_count_is("AWS::SQS::Queue", 0)
    rendered = str(template.to_json())
    assert "ACTION_QUEUE_URL" not in rendered
    assert "INBOUND_QUEUE_URL" in rendered
    assert "states:SendTaskSuccess" not in rendered
    assert "states:StartExecution" not in rendered
    actions, resources = _sqs_statements(template)
    assert actions == [["sqs:SendMessage"]]
    assert resources == [{"Fn::ImportValue": _inbound_queue_export(refs)}]


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
    buckets = template.find_resources("AWS::S3::Bucket")
    assert len(buckets) == 1
    rule = next(iter(buckets.values()))["Properties"]["BucketEncryption"]
    default = rule["ServerSideEncryptionConfiguration"][0]
    assert default["ServerSideEncryptionByDefault"] == {"SSEAlgorithm": "AES256"}
    assert "BucketKeyEnabled" not in default


def test_dashboard_roles_are_not_granted_kms(template: Template) -> None:
    found = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            if statement.get("Effect") == "Deny":
                continue
            action = statement.get("Action", [])
            names = [action] if isinstance(action, str) else list(action)
            found.extend(name for name in names if name.startswith("kms:"))
    assert found == []


def test_dashboard_functions_trace_and_keep_logs_for_thirty_days(template: Template) -> None:
    functions = template.find_resources("AWS::Lambda::Function")
    assert len(functions) == 2
    log_groups = template.find_resources("AWS::Logs::LogGroup")
    assert len(log_groups) == 2
    referenced = set()
    for props in functions.values():
        assert props["Properties"]["TracingConfig"] == {"Mode": "Active"}
        log_group = props["Properties"]["LoggingConfig"]["LogGroup"]
        assert "Ref" in log_group
        referenced.add(log_group["Ref"])
    assert referenced == set(log_groups)
    for group in log_groups.values():
        assert group["Properties"]["RetentionInDays"] == 30


def test_websocket_is_the_compute_endpoint_and_cors_is_cloudfront(
    template: Template,
) -> None:
    template.resource_count_is("AWS::ApiGatewayV2::Api", 0)
    template.resource_count_is("AWS::ApiGatewayV2::Stage", 0)
    template.resource_count_is("AWS::Lambda::Function", 2)
    outputs = template.find_outputs("*")
    names = set(outputs)
    assert {"DashboardUrl", "RestApiUrl", "WebSocketUrl"} <= names
    assert "ActionQueueUrl" not in names
    assert {"CognitoDomain", "UserPoolClientId", "UserPoolId"} <= names
    assert outputs["WebSocketUrl"]["Value"] == WEBSOCKET_URL
    template.resource_count_is("AWS::Cognito::UserPoolDomain", 1)
    template.has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        {"AllowedOAuthFlows": ["code"]},
    )
    origins = _allow_origins(template.to_json())
    assert origins
    for origin in origins:
        rendered = str(origin)
        assert "*" not in rendered
        assert "DomainName" in rendered


def _sqs_statements(template: Template) -> tuple[list[list[str]], list[object]]:
    actions: list[list[str]] = []
    resources: list[object] = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            raw = statement.get("Action", [])
            names = [raw] if isinstance(raw, str) else list(raw)
            if any(name.startswith("sqs:") for name in names):
                actions.append(names)
                resources.append(statement["Resource"])
    return actions, resources


def _inbound_queue_export(template: Template) -> str:
    for output in template.to_json()["Outputs"].values():
        value = output["Value"]
        target = value.get("Fn::GetAtt") if isinstance(value, dict) else None
        if target and target[0].startswith("InboundQueue") and target[1] == "Arn":
            return output["Export"]["Name"]
    raise AssertionError("inbound queue ARN is not exported")


def _allow_origins(template: dict) -> list[object]:
    found: list[object] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    key == "method.response.header.Access-Control-Allow-Origin"
                    and not isinstance(value, bool)
                ):
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(template)
    return found
