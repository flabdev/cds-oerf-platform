"""Template assertions for OerfComputeStack. No AWS calls."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infra.stacks.compute_stack import OerfComputeStack
from infra.stacks.data_stack import OerfDataStack

ACCOUNT = "123456789012"
REGION = "us-east-1"
REPO_ROOT = Path(__file__).resolve().parents[2]
EXCEPTIONS = REPO_ROOT / "docs" / "security" / "iam-exceptions.md"
_IAM_ACTION = re.compile(r"`([a-z0-9-]+:[A-Za-z0-9*]+)`")
_SYNTH_ENV_KEYS = ("SES_SOURCE_EMAIL", "CDK_DEFAULT_ACCOUNT")

EXPECTED_FUNCTIONS = {
    "oerf-channel-router": {
        "handler": "oerf.platform.channels.router.handler",
        "env": {
            "ROUTING_TABLE_NAME",
            "USE_MOCK_WHATSAPP",
            "USE_MOCK_SMS",
            "WHATSAPP_PHONE_NUMBER_ID",
            "WA_PHONE_NUMBER_ID_PARAM",
            "SES_SOURCE_EMAIL",
            "USE_MOCK_EMAIL",
        },
    },
    "oerf-evidence-analyzer": {
        "handler": "oerf.platform.evidence.vision.handler",
        "env": {
            "INCIDENT_TABLE_NAME",
            "BEDROCK_MODEL_ID",
            "USE_MOCK_BEDROCK",
            "GUARDRAIL_ID",
            "GUARDRAIL_VERSION",
        },
    },
    "oerf-report-generator": {
        "handler": "oerf.platform.report.generator.handler",
        "env": {"REPORT_BUCKET_NAME", "INCIDENT_TABLE_NAME"},
    },
    "oerf-ws-handler": {
        "handler": "oerf.platform.api.ws_handler.handler",
        "env": {"ROUTING_TABLE_NAME"},
    },
    "oerf-ws-authorizer": {
        "handler": "oerf.platform.api.ws_authorizer.handler",
        "env": {"ROUTING_TABLE_NAME", "USER_POOL_ID", "USER_POOL_CLIENT_ID"},
    },
    "oerf-stream-broadcaster": {
        "handler": "oerf.platform.api.stream_broadcaster.handler",
        "env": {"ROUTING_TABLE_NAME", "WEBSOCKET_ENDPOINT"},
    },
    "oerf-action-handler": {
        "handler": "oerf.platform.api.action_handler.handler",
        "env": {
            "INCIDENT_TABLE_NAME",
            "ROUTING_TABLE_NAME",
        },
    },
    "oerf-inbound": {
        "handler": "oerf.platform.inbound.handler.handler",
        "env": {
            "TENANT_TABLE_NAME",
            "INCIDENT_TABLE_NAME",
            "ROUTING_TABLE_NAME",
            "PLATFORM_TABLE_NAME",
            "EVIDENCE_BUCKET_NAME",
            "STATE_MACHINE_ARN",
            "WA_PHONE_NUMBER_ID_PARAM",
            "WHATSAPP_PHONE_NUMBER_ID",
            "INBOUND_MEDIA_MAX_BYTES",
            "LOG_LEVEL",
        },
    },
    "oerf-ingest": {
        "handler": "oerf.platform.ingest.handler.handler",
        "env": {
            "TENANT_TABLE_NAME",
            "INCIDENT_TABLE_NAME",
            "ROUTING_TABLE_NAME",
            "EVENT_BUS_NAME",
            "STATE_MACHINE_ARN",
            "LOG_LEVEL",
        },
    },
    "oerf-step-context": {
        "handler": "domains.cold_chain_excursion.steps.context.handler",
        "env": {"INCIDENT_TABLE_NAME", "TENANT_TABLE_NAME", "LOG_LEVEL"},
    },
    "oerf-step-assess": {
        "handler": "domains.cold_chain_excursion.steps.assess.handler",
        "env": {"INCIDENT_TABLE_NAME", "TENANT_TABLE_NAME", "LOG_LEVEL"},
    },
    "oerf-step-act": {
        "handler": "domains.cold_chain_excursion.steps.act.handler",
        "env": {"INCIDENT_TABLE_NAME", "TENANT_TABLE_NAME", "LOG_LEVEL"},
    },
    "oerf-step-verify": {
        "handler": "domains.cold_chain_excursion.steps.verify.handler",
        "env": {"INCIDENT_TABLE_NAME", "TENANT_TABLE_NAME", "LOG_LEVEL"},
    },
    "oerf-step-qa": {
        "handler": "domains.cold_chain_excursion.steps.qa.handler",
        "env": {"INCIDENT_TABLE_NAME", "TENANT_TABLE_NAME", "LOG_LEVEL"},
    },
    "oerf-step-close": {
        "handler": "domains.cold_chain_excursion.steps.close.handler",
        "env": {"INCIDENT_TABLE_NAME", "TENANT_TABLE_NAME", "LOG_LEVEL"},
    },
}


def _without_synth_env():
    """Drop account and channel identity env so synth stays deterministic."""
    saved = {key: os.environ.get(key) for key in _SYNTH_ENV_KEYS}
    for key in _SYNTH_ENV_KEYS:
        os.environ.pop(key, None)
    return saved


def _restore_env(saved: dict[str, str | None]) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def synthesize(context: dict | None = None) -> tuple[OerfComputeStack, Template]:
    app = cdk.App(context=context)
    env = cdk.Environment(account=ACCOUNT, region=REGION)
    data = OerfDataStack(app, "OerfDataStack", env=env)
    stack = OerfComputeStack(app, "OerfComputeStack", data_stack=data, env=env)
    return stack, Template.from_stack(stack)


@pytest.fixture(scope="module")
def compute() -> tuple[OerfComputeStack, Template]:
    saved = _without_synth_env()
    try:
        return synthesize()
    finally:
        _restore_env(saved)


@pytest.fixture(scope="module")
def stack(compute: tuple[OerfComputeStack, Template]) -> OerfComputeStack:
    return compute[0]


@pytest.fixture(scope="module")
def template(compute: tuple[OerfComputeStack, Template]) -> Template:
    return compute[1]


def functions_by_name(template: Template) -> dict[str, dict]:
    found = {}
    for resource in template.find_resources("AWS::Lambda::Function").values():
        props = resource["Properties"]
        name = props.get("FunctionName")
        if isinstance(name, str):
            found[name] = props
    return found


def role_logical_id(function_props: dict) -> str:
    role = function_props["Role"]
    if "Fn::GetAtt" in role:
        return role["Fn::GetAtt"][0]
    return role["Ref"]


def statements_for_role(template: Template, role_logical_id: str) -> list[dict]:
    found: list[dict] = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        props = policy["Properties"]
        refs = [
            ref["Ref"]
            for ref in props.get("Roles", [])
            if isinstance(ref, dict) and "Ref" in ref
        ]
        if role_logical_id in refs:
            found.extend(props["PolicyDocument"]["Statement"])
    role = template.find_resources("AWS::IAM::Role").get(role_logical_id, {})
    for inline in role.get("Properties", {}).get("Policies", []):
        found.extend(inline["PolicyDocument"]["Statement"])
    return found


def action_names(statements: list[dict]) -> set[str]:
    names: set[str] = set()
    for statement in statements:
        action = statement.get("Action", [])
        if isinstance(action, str):
            names.add(action)
        else:
            names.update(action)
    return names


def role_actions(template: Template, function_name: str) -> set[str]:
    props = functions_by_name(template)[function_name]
    return action_names(statements_for_role(template, role_logical_id(props)))


def allowed_star_actions() -> set[str]:
    """IAM actions the exceptions file permits on Resource ``*``."""
    return set(_IAM_ACTION.findall(EXCEPTIONS.read_text(encoding="utf-8")))


def resource_is_star(resource: object) -> bool:
    if resource == "*":
        return True
    if isinstance(resource, list):
        return any(item == "*" for item in resource)
    return False


def iter_allow_statements(template: Template):
    documents = []
    for resource in template.find_resources("AWS::IAM::Policy").values():
        documents.append(resource["Properties"]["PolicyDocument"])
    for resource in template.find_resources("AWS::IAM::Role").values():
        for inline in resource["Properties"].get("Policies", []):
            documents.append(inline["PolicyDocument"])
    for document in documents:
        for statement in document.get("Statement", []):
            if statement.get("Effect", "Allow") == "Allow":
                yield statement


def test_handlers_names_env_and_distinct_roles(template: Template) -> None:
    functions = functions_by_name(template)
    assert set(EXPECTED_FUNCTIONS).issubset(set(functions))

    role_ids = []
    for name, expected in EXPECTED_FUNCTIONS.items():
        props = functions[name]
        assert props["Handler"] == expected["handler"]
        assert set(props["Environment"]["Variables"]) == expected["env"]
        role_ids.append(role_logical_id(props))

    assert len(set(role_ids)) == len(role_ids)


def test_every_function_has_active_tracing_and_thirty_day_logs(template: Template) -> None:
    functions = functions_by_name(template)
    log_groups = template.find_resources("AWS::Logs::LogGroup")
    referenced = set()
    for name in EXPECTED_FUNCTIONS:
        props = functions[name]
        assert props["TracingConfig"] == {"Mode": "Active"}
        log_group = props["LoggingConfig"]["LogGroup"]
        assert "Ref" in log_group
        referenced.add(log_group["Ref"])
    assert referenced.issubset(set(log_groups))
    for logical_id in referenced:
        assert log_groups[logical_id]["Properties"]["RetentionInDays"] == 30


_STEP_DYNAMO = {
    "oerf-step-context": {"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"},
    "oerf-step-assess": {"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"},
    "oerf-step-act": {"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"},
    "oerf-step-verify": {"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"},
    "oerf-step-qa": {"dynamodb:GetItem", "dynamodb:PutItem"},
    "oerf-step-close": {
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
    },
}
_XRAY_STAR = {"xray:PutTraceSegments", "xray:PutTelemetryRecords"}


def _action_list(statement: dict) -> list[str]:
    action = statement.get("Action", [])
    return [action] if isinstance(action, str) else list(action)


def _resource_count(statement: dict) -> int:
    resource = statement["Resource"]
    return len(resource) if isinstance(resource, list) else 1


@pytest.mark.security
def test_step_roles_have_only_the_handler_actions(template: Template) -> None:
    """Each step role matches the DynamoDB calls in its handler. X-Ray * is listed."""

    for name, expected in _STEP_DYNAMO.items():
        props = functions_by_name(template)[name]
        assert props["Timeout"] == 30
        assert props["MemorySize"] == 256
        statements = statements_for_role(template, role_logical_id(props))
        dynamo = {
            action
            for statement in statements
            for action in _action_list(statement)
            if action.startswith("dynamodb:")
        }
        assert dynamo == expected, name
        for statement in statements:
            actions = _action_list(statement)
            if "dynamodb:GetItem" in actions:
                assert _resource_count(statement) == 2, name
            elif any(action.startswith("dynamodb:") for action in actions):
                assert _resource_count(statement) == 1, name
            if resource_is_star(statement.get("Resource")):
                assert set(actions) <= _XRAY_STAR, name
        forbidden = role_actions(template, name)
        assert "states:SendTaskSuccess" not in forbidden
        assert "lambda:InvokeFunction" not in forbidden
        assert "bedrock:InvokeModel" not in forbidden
        assert "ses:SendEmail" not in forbidden


@pytest.mark.security
def test_no_role_is_granted_kms(template: Template) -> None:
    """Bucket and table grants do not add kms:Decrypt for aws/s3 or aws/dynamodb."""

    found = []
    for statement in iter_allow_statements(template):
        action = statement.get("Action", [])
        names = [action] if isinstance(action, str) else list(action)
        found.extend(name for name in names if name.startswith("kms:"))
    assert found == []


@pytest.mark.security
def test_star_resources_are_listed_in_the_exceptions_file(
    stack: OerfComputeStack, template: Template
) -> None:
    """Compute and data templates. The data stack holds the OIDC provider role."""

    data = stack.node.scope.node.try_find_child("OerfDataStack")
    assert isinstance(data, OerfDataStack)
    scanned = {
        "OerfComputeStack": template,
        "OerfDataStack": Template.from_stack(data),
    }
    allowed = allowed_star_actions()
    assert allowed, f"{EXCEPTIONS} does not list any IAM actions"
    offenders = []
    for stack_name, current in scanned.items():
        for statement in iter_allow_statements(current):
            if not resource_is_star(statement.get("Resource")):
                continue
            action = statement.get("Action", [])
            names = [action] if isinstance(action, str) else list(action)
            for name in names:
                if name not in allowed:
                    offenders.append(f"{stack_name} {name}")
    assert offenders == [], (
        "Resource * is outside docs/security/iam-exceptions.md: " + ", ".join(offenders)
    )
    oidc = {
        "iam:CreateOpenIDConnectProvider",
        "iam:DeleteOpenIDConnectProvider",
        "iam:UpdateOpenIDConnectProviderThumbprint",
        "iam:AddClientIDToOpenIDConnectProvider",
        "iam:RemoveClientIDFromOpenIDConnectProvider",
    }
    found: set[str] = set()
    for statement in iter_allow_statements(scanned["OerfDataStack"]):
        if not resource_is_star(statement.get("Resource")):
            continue
        action = statement.get("Action", [])
        names = [action] if isinstance(action, str) else list(action)
        found.update(name for name in names if name in oidc)
    assert found == oidc


@pytest.mark.security
def test_inbound_media_permissions_use_end_user_messaging_not_a_token_secret(
    template: Template,
) -> None:
    inbound = functions_by_name(template)["oerf-inbound"]
    variables = inbound["Environment"]["Variables"]
    assert variables["WA_PHONE_NUMBER_ID_PARAM"] == "/oerf/whatsapp/phone-number-id"
    assert variables["WHATSAPP_PHONE_NUMBER_ID"] == ""
    assert variables["INBOUND_MEDIA_MAX_BYTES"] == str(10 * 1024 * 1024)
    statements = statements_for_role(template, role_logical_id(inbound))
    assert _statements_with_action(statements, "secretsmanager:GetSecretValue") == []
    puts = _statements_with_action(statements, "s3:PutObject")
    assert len(puts) == 1
    resource = _resource_list(puts[0]["Resource"])[0]
    assert resource["Fn::Join"][1][-1].endswith("/tenants/*/inbound/*")
    media = _statements_with_action(statements, "social-messaging:GetWhatsAppMessageMedia")
    assert len(media) == 1
    assert media[0]["Resource"] == "*"
    parameter = _statements_with_action(statements, "ssm:GetParameter")
    assert len(parameter) == 1
    assert _resource_list(parameter[0]["Resource"]) == [
        f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/oerf/whatsapp/phone-number-id"
    ]


@pytest.mark.security
def test_send_task_success_is_only_on_the_inbound_role(template: Template) -> None:
    inbound = role_actions(template, "oerf-inbound")
    action = role_actions(template, "oerf-action-handler")
    assert "states:SendTaskSuccess" in inbound
    assert "states:SendTaskFailure" not in inbound
    assert "states:StopExecution" in inbound
    assert "states:SendTaskSuccess" not in action
    assert "states:SendTaskFailure" not in action
    assert "states:StartExecution" not in action
    assert "states:StartExecution" in role_actions(template, "oerf-ingest")
    assert "states:SendTaskSuccess" not in role_actions(template, "oerf-ingest")
    assert "sqs:SendMessage" not in action
    routes = [
        resource["Properties"]["RouteKey"]
        for resource in template.find_resources("AWS::ApiGatewayV2::Route").values()
    ]
    assert "GET /incidents" in routes
    assert "GET /incidents/{id}" in routes
    assert "POST /incidents/{id}/actions" not in routes
    assert all(not key.endswith("trigger") for key in routes)


@pytest.mark.security
def test_inbound_can_stop_only_cold_chain_executions(template: Template) -> None:
    inbound = functions_by_name(template)["oerf-inbound"]
    stops = [
        statement
        for statement in statements_for_role(template, role_logical_id(inbound))
        if "states:StopExecution" in _action_list(statement)
    ]
    assert len(stops) == 1
    rendered = json.dumps(stops[0]["Resource"])
    assert ":execution:" in rendered
    assert "OerfColdChainStateMachine" in rendered
    assert "EngageParty" not in rendered
    assert ":stateMachine:" not in rendered
    for name in functions_by_name(template):
        if name == "oerf-inbound":
            continue
        assert "states:StopExecution" not in role_actions(template, name)


@pytest.mark.security
def test_invokers_and_queue_allows_require_this_account(template: Template) -> None:
    permissions = template.find_resources("AWS::Lambda::Permission")
    assert permissions
    for logical_id, resource in permissions.items():
        props = resource["Properties"]
        assert props["Principal"] in {"apigateway.amazonaws.com", "events.amazonaws.com"}
        assert props["SourceAccount"] == ACCOUNT, logical_id
        assert "SourceArn" in props

    allows = []
    for policy in template.find_resources("AWS::SQS::QueuePolicy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            condition = statement.get("Condition", {})
            if "aws:SourceArn" not in condition.get("ArnEquals", {}):
                continue
            allows.append(statement)
    assert len(allows) == 2
    for statement in allows:
        assert statement["Condition"]["StringEquals"]["aws:SourceAccount"] == ACCOUNT


def test_inbound_topic_is_standard_and_queue_redrives_after_three(
    stack: OerfComputeStack, template: Template
) -> None:
    assert stack.inbound_topic.fifo is False
    topics = template.find_resources("AWS::SNS::Topic")
    assert len(topics) == 1
    assert next(iter(topics.values()))["Properties"].get("FifoTopic") is not True

    queues = template.find_resources("AWS::SQS::Queue")
    inbound = [
        props["Properties"]
        for props in queues.values()
        if props["Properties"].get("RedrivePolicy", {}).get("maxReceiveCount") == 3
    ]
    assert len(inbound) == 1
    assert inbound[0]["SqsManagedSseEnabled"] is True
    assert inbound[0]["VisibilityTimeout"] == 180

    policies = template.find_resources("AWS::SQS::QueuePolicy")
    sns_statements = []
    for policy in policies.values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            principal = statement.get("Principal", {})
            service = principal.get("Service") if isinstance(principal, dict) else None
            services = [service] if isinstance(service, str) else list(service or [])
            if "sns.amazonaws.com" in services:
                sns_statements.append(statement)
    assert len(sns_statements) == 1
    action = sns_statements[0].get("Action")
    actions = [action] if isinstance(action, str) else list(action or [])
    assert actions == ["sqs:SendMessage"]
    assert "aws:SourceArn" in sns_statements[0]["Condition"]["ArnEquals"]


_WABA_ID = "waba-01234567890123456789012345678901"


def _waba_arn() -> str:
    return f"arn:aws:social-messaging:{REGION}:{ACCOUNT}:waba/{_WABA_ID}"


def test_demo_device_seed_stays_out_unless_the_flag_is_set(template: Template) -> None:
    assert "oerf-device-seed" not in functions_by_name(template)
    assert template.find_resources("AWS::CloudFormation::CustomResource") == {}


def test_demo_device_seed_is_opt_in() -> None:
    saved = _without_synth_env()
    try:
        _stack, seeded = synthesize(context={"demo:seedDevices": "true"})
    finally:
        _restore_env(saved)

    functions = functions_by_name(seeded)
    assert functions["oerf-device-seed"]["Handler"] == "oerf.platform.channels.devices.seed_handler"
    assert "ROUTING_TABLE_NAME" in functions["oerf-device-seed"]["Environment"]["Variables"]
    custom = seeded.find_resources("AWS::CloudFormation::CustomResource")
    assert len(custom) == 1
    assert next(iter(custom.values()))["Properties"]["SeedVersion"] == "1"


def test_waba_destination_stays_out_without_context(template: Template) -> None:
    assert "oerf-waba-event-destination" not in functions_by_name(template)
    assert template.find_resources("AWS::CloudFormation::CustomResource") == {}
    topic = next(iter(template.find_resources("AWS::SNS::Topic").values()))
    assert "KmsMasterKeyId" not in topic["Properties"]
    for policy in template.find_resources("AWS::SNS::TopicPolicy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            principal = statement.get("Principal", {})
            service = principal.get("Service") if isinstance(principal, dict) else principal
            services = [service] if isinstance(service, str) else list(service or [])
            assert "social-messaging.amazonaws.com" not in services


def test_waba_destination_scopes_publish_and_the_provider_role() -> None:
    saved = _without_synth_env()
    try:
        _stack, enabled = synthesize(context={"whatsapp:wabaId": _WABA_ID})
    finally:
        _restore_env(saved)

    topic = next(iter(enabled.find_resources("AWS::SNS::Topic").values()))
    assert "KmsMasterKeyId" not in topic["Properties"]
    policies = enabled.find_resources("AWS::SNS::TopicPolicy")
    assert len(policies) == 1
    statements = next(iter(policies.values()))["Properties"]["PolicyDocument"]["Statement"]
    publish = [
        statement
        for statement in statements
        if _principal_services(statement) == ["social-messaging.amazonaws.com"]
    ]
    assert len(publish) == 1
    assert _resource_list(publish[0]["Action"]) == ["sns:Publish"]
    assert publish[0]["Condition"]["StringEquals"] == {
        "aws:SourceAccount": ACCOUNT,
        "aws:SourceArn": _waba_arn(),
    }

    custom = enabled.find_resources("AWS::CloudFormation::CustomResource")
    assert len(custom) == 1
    props = next(
        resource["Properties"]
        for resource in custom.values()
        if "WabaId" in resource["Properties"]
    )
    assert props["WabaId"] == _WABA_ID
    assert props["TopicArn"]["Ref"].startswith("InboundTopic")

    function = functions_by_name(enabled)["oerf-waba-event-destination"]
    assert function["TracingConfig"] == {"Mode": "Active"}
    role_statements = statements_for_role(enabled, role_logical_id(function))
    social = [
        statement
        for statement in role_statements
        if any(name.startswith("social-messaging:") for name in _action_list(statement))
    ]
    assert len(social) == 1
    assert set(_action_list(social[0])) == {
        "social-messaging:PutWhatsAppBusinessAccountEventDestinations",
        "social-messaging:GetLinkedWhatsAppBusinessAccount",
    }
    assert _resource_list(social[0]["Resource"]) == [_waba_arn()]
    logs = _statements_with_action(role_statements, "logs:PutLogEvents")
    assert len(logs) == 1
    assert logs[0]["Resource"] != "*"

    allowed = allowed_star_actions()
    offenders = []
    for statement in iter_allow_statements(enabled):
        if not resource_is_star(statement.get("Resource")):
            continue
        offenders.extend(name for name in _action_list(statement) if name not in allowed)
    assert offenders == [], "Resource * is outside iam-exceptions.md: " + ", ".join(offenders)


def _principal_services(statement: dict) -> list[str]:
    principal = statement.get("Principal", {})
    service = principal.get("Service") if isinstance(principal, dict) else principal
    if service is None:
        return []
    return [service] if isinstance(service, str) else list(service)


def _action_list(statement: dict) -> list[str]:
    action = statement.get("Action", [])
    return [action] if isinstance(action, str) else list(action)


def test_inbound_event_source_reports_batch_item_failures(template: Template) -> None:
    mappings = template.find_resources("AWS::Lambda::EventSourceMapping")
    partial = [
        mapping["Properties"]
        for mapping in mappings.values()
        if "ReportBatchItemFailures" in mapping["Properties"].get("FunctionResponseTypes", [])
        and "StartingPosition" not in mapping["Properties"]
    ]
    assert len(partial) == 1
    source = partial[0]["EventSourceArn"]
    assert source["Fn::GetAtt"][0].startswith("InboundQueue")


@pytest.mark.security
def test_queues_and_inbound_topic_deny_insecure_transport(template: Template) -> None:
    queues = template.find_resources("AWS::SQS::Queue")
    assert len(queues) == 4
    for logical_id, queue in queues.items():
        assert queue["Properties"]["SqsManagedSseEnabled"] is True, logical_id
    policies = list(template.find_resources("AWS::SQS::QueuePolicy").values())
    covered: set[str] = set()
    for policy in policies:
        props = policy["Properties"]
        statements = props["PolicyDocument"]["Statement"]
        assert any(_denies_insecure_transport(item) for item in statements)
        for queue in props["Queues"]:
            covered.add(queue["Ref"])
    assert covered == set(queues)

    topics = template.find_resources("AWS::SNS::TopicPolicy")
    assert len(topics) == 1
    statements = next(iter(topics.values()))["Properties"]["PolicyDocument"]["Statement"]
    assert any(_denies_insecure_transport(item) for item in statements)


def test_stream_broadcaster_bisects_and_fails_to_a_queue(template: Template) -> None:
    mappings = template.find_resources("AWS::Lambda::EventSourceMapping")
    stream = [
        mapping["Properties"]
        for mapping in mappings.values()
        if mapping["Properties"].get("StartingPosition") == "LATEST"
    ]
    assert len(stream) == 1
    props = stream[0]
    assert props["BisectBatchOnFunctionError"] is True
    assert props["MaximumRecordAgeInSeconds"] == 24 * 60 * 60
    assert props["MaximumRetryAttempts"] == 3
    assert "ReportBatchItemFailures" in props["FunctionResponseTypes"]
    assert "Destination" in props["DestinationConfig"]["OnFailure"]
    filters = props["FilterCriteria"]["Filters"]
    assert len(filters) == 1
    pattern = filters[0]["Pattern"]
    if isinstance(pattern, str):
        pattern = json.loads(pattern)
    assert pattern["dynamodb"]["Keys"]["sk"]["S"] == ["META"]
    assert pattern["dynamodb"]["Keys"]["pk"]["S"] == [{"prefix": "TENANT#"}]


def _denies_insecure_transport(statement: dict) -> bool:
    if statement.get("Effect") != "Deny":
        return False
    flag = statement.get("Condition", {}).get("Bool", {}).get("aws:SecureTransport")
    return flag in ("false", False)


def test_temperature_rule_retries_to_a_dlq_and_limits_ingest_concurrency(
    template: Template,
) -> None:
    rules = template.find_resources("AWS::Events::Rule")
    assert len(rules) == 1
    props = next(iter(rules.values()))["Properties"]
    pattern = props["EventPattern"]
    if isinstance(pattern, str):
        pattern = json.loads(pattern)
    assert pattern["source"] == ["sim.reefer"]
    assert pattern["detail-type"] == ["TemperatureReading"]
    assert "oerf.incident" not in json.dumps(template.to_json())

    target = props["Targets"][0]
    assert target["RetryPolicy"]["MaximumRetryAttempts"] == 2
    assert target["RetryPolicy"]["MaximumEventAgeInSeconds"] == 3600
    assert "Arn" in target["DeadLetterConfig"]

    ingest = functions_by_name(template)["oerf-ingest"]
    assert ingest["ReservedConcurrentExecutions"] == 5
    ingest_actions = role_actions(template, "oerf-ingest")
    assert "states:StartExecution" in ingest_actions
    for statement in statements_for_role(template, role_logical_id(ingest)):
        action = statement.get("Action")
        names = [action] if isinstance(action, str) else list(action or [])
        if "states:StartExecution" in names:
            assert statement.get("Resource") != "*"
            assert resource_is_star(statement.get("Resource")) is False


def test_ingest_concurrency_comes_from_context() -> None:
    saved = _without_synth_env()
    try:
        _stack, flagged = synthesize(context={"ingest:reservedConcurrency": "7"})
    finally:
        _restore_env(saved)
    assert functions_by_name(flagged)["oerf-ingest"]["ReservedConcurrentExecutions"] == 7


def test_authorizer_has_no_cognito_iam_and_report_has_no_ses(template: Template) -> None:
    iam_actions = set()
    for statement in iter_allow_statements(template):
        action = statement.get("Action", [])
        if isinstance(action, str):
            iam_actions.add(action)
        else:
            iam_actions.update(action)
    assert not any(action.startswith("cognito-idp:") for action in iam_actions)
    assert "ses:SendEmail" not in role_actions(template, "oerf-report-generator")
    assert "ses:SendEmail" not in role_actions(template, "oerf-channel-router")
    router = role_actions(template, "oerf-channel-router")
    assert "sns:Publish" in router
    assert "social-messaging:SendWhatsAppMessage" in router
    assert "socialmessaging:*" not in router
    assert "ssm:GetParameter" in router


def _router_variables(template: Template) -> dict:
    return functions_by_name(template)["oerf-channel-router"]["Environment"]["Variables"]


def _resource_list(resource: object) -> list:
    if isinstance(resource, list):
        return resource
    return [resource]


def _statements_with_action(statements: list[dict], action_name: str) -> list[dict]:
    matched = []
    for statement in statements:
        action = statement.get("Action")
        names = [action] if isinstance(action, str) else list(action or [])
        if action_name in names:
            matched.append(statement)
    return matched


def test_channel_mocks_default_true_and_whatsapp_is_unscoped_without_a_phone_id(
    template: Template,
) -> None:
    variables = _router_variables(template)
    assert variables["USE_MOCK_WHATSAPP"] == "true"
    assert variables["USE_MOCK_SMS"] == "true"
    assert variables["USE_MOCK_EMAIL"] == "true"
    assert variables["WHATSAPP_PHONE_NUMBER_ID"] == ""
    assert variables["WA_PHONE_NUMBER_ID_PARAM"] == "/oerf/whatsapp/phone-number-id"

    statements = statements_for_role(
        template, role_logical_id(functions_by_name(template)["oerf-channel-router"])
    )
    send = _statements_with_action(statements, "social-messaging:SendWhatsAppMessage")
    assert len(send) == 1
    assert send[0]["Resource"] == "*"
    parameter = _statements_with_action(statements, "ssm:GetParameter")
    assert len(parameter) == 1
    assert _resource_list(parameter[0]["Resource"]) == [
        f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/oerf/whatsapp/phone-number-id"
    ]
    vision = functions_by_name(template)["oerf-evidence-analyzer"]["Environment"]["Variables"]
    assert vision["USE_MOCK_BEDROCK"] == "true"
    assert vision["BEDROCK_MODEL_ID"] == "amazon.nova-lite-v1:0"
    bedrock_statements = statements_for_role(
        template, role_logical_id(functions_by_name(template)["oerf-evidence-analyzer"])
    )
    invoke = _statements_with_action(bedrock_statements, "bedrock:InvokeModel")
    assert len(invoke) == 1
    assert _resource_list(invoke[0]["Resource"]) == [
        f"arn:aws:bedrock:{REGION}::foundation-model/amazon.nova-lite-v1:0"
    ]


def test_channels_live_disables_mocks_and_scopes_whatsapp_to_the_phone_number() -> None:
    phone_id = "phone-number-id-01234567890123456789012345678901"
    saved = _without_synth_env()
    try:
        _stack, live = synthesize(
            context={"channels:live": "true", "whatsapp:phoneNumberId": phone_id}
        )
    finally:
        _restore_env(saved)

    variables = _router_variables(live)
    assert variables["USE_MOCK_WHATSAPP"] == "false"
    assert variables["USE_MOCK_SMS"] == "false"
    assert variables["USE_MOCK_EMAIL"] == "false"
    assert variables["WHATSAPP_PHONE_NUMBER_ID"] == phone_id

    statements = statements_for_role(
        live, role_logical_id(functions_by_name(live)["oerf-channel-router"])
    )
    send = _statements_with_action(statements, "social-messaging:SendWhatsAppMessage")
    assert len(send) == 1
    assert _resource_list(send[0]["Resource"]) == [
        f"arn:aws:social-messaging:{REGION}:{ACCOUNT}:phone-number-id/{phone_id}"
    ]
    assert "socialmessaging:*" not in role_actions(live, "oerf-channel-router")
    inbound_statements = statements_for_role(
        live, role_logical_id(functions_by_name(live)["oerf-inbound"])
    )
    fetch = _statements_with_action(
        inbound_statements, "social-messaging:GetWhatsAppMessageMedia"
    )
    assert len(fetch) == 1
    assert _resource_list(fetch[0]["Resource"]) == [
        f"arn:aws:social-messaging:{REGION}:{ACCOUNT}:phone-number-id/{phone_id}"
    ]
    assert functions_by_name(live)["oerf-inbound"]["Environment"]["Variables"][
        "WHATSAPP_PHONE_NUMBER_ID"
    ] == phone_id
    vision = functions_by_name(live)["oerf-evidence-analyzer"]["Environment"]["Variables"]
    assert vision["USE_MOCK_BEDROCK"] == "false"
    assert vision["BEDROCK_MODEL_ID"] == "amazon.nova-lite-v1:0"


def test_guardrail_is_versioned_and_only_vision_can_apply_it(template: Template) -> None:
    config = json.loads((REPO_ROOT / "infra" / "config" / "guardrail.json").read_text())
    guards = template.find_resources("AWS::Bedrock::Guardrail")
    assert len(guards) == 1
    props = next(iter(guards.values()))["Properties"]
    assert props["Name"] == config["name"]
    assert props["BlockedInputMessaging"] == config["blocked_input_message"]
    assert props["BlockedOutputsMessaging"] == config["blocked_output_message"]
    assert props["ContentPolicyConfig"]["ContentFiltersTierConfig"] == {
        "TierName": config["content_filters_tier"]
    }
    filters = props["ContentPolicyConfig"]["FiltersConfig"]
    expected_filters = config["content_filters"]
    assert [item["Type"] for item in filters] == [item["type"] for item in expected_filters]
    for item, expected in zip(filters, expected_filters, strict=True):
        assert item["InputStrength"] == expected["input_strength"]
        assert item["OutputStrength"] == expected["output_strength"]
        assert "IMAGE" not in item.get("InputModalities", [])
        assert "IMAGE" not in item.get("OutputModalities", [])
    topics = props["TopicPolicyConfig"]["TopicsConfig"]
    assert [item["Name"] for item in topics] == [item["name"] for item in config["denied_topics"]]
    assert props["TopicPolicyConfig"]["TopicsTierConfig"] == {"TierName": config["topics_tier"]}
    pii = props["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]
    assert [(item["Type"], item["OutputAction"]) for item in pii] == [
        (item["type"], item["output_action"]) for item in config["pii_entities"]
    ]
    profile_id = config["guardrail_profile_identifier"]
    assert props["CrossRegionConfig"] == {
        "GuardrailProfileArn": (
            f"arn:aws:bedrock:{REGION}:{ACCOUNT}:guardrail-profile/{profile_id}"
        )
    }

    versions = template.find_resources("AWS::Bedrock::GuardrailVersion")
    assert len(versions) == 1
    version = next(iter(versions.values()))["Properties"]
    assert version["GuardrailIdentifier"]["Fn::GetAtt"][1] == "GuardrailId"

    functions = functions_by_name(template)
    for name, function in functions.items():
        variables = function["Environment"]["Variables"]
        if name == "oerf-evidence-analyzer":
            assert set(variables["GUARDRAIL_ID"]["Fn::GetAtt"]) >= {"GuardrailId"}
            assert variables["GUARDRAIL_VERSION"]["Fn::GetAtt"][1] == "Version"
        else:
            assert "GUARDRAIL_ID" not in variables
            assert "GUARDRAIL_VERSION" not in variables

    for name in functions:
        statements = statements_for_role(template, role_logical_id(functions[name]))
        apply = _statements_with_action(statements, "bedrock:ApplyGuardrail")
        if name == "oerf-evidence-analyzer":
            assert len(apply) == 1
            _assert_apply_guardrail(apply[0], profile_id)
            invoke = _statements_with_action(statements, "bedrock:InvokeModel")
            assert _resource_list(invoke[0]["Resource"]) == [
                f"arn:aws:bedrock:{REGION}::foundation-model/amazon.nova-lite-v1:0"
            ]
        else:
            assert apply == []


def test_bedrock_model_id_comes_from_context() -> None:
    saved = _without_synth_env()
    try:
        _stack, custom = synthesize(context={"bedrock:modelId": "example.model-v1:0"})
    finally:
        _restore_env(saved)
    vision = functions_by_name(custom)["oerf-evidence-analyzer"]["Environment"]["Variables"]
    assert vision["BEDROCK_MODEL_ID"] == "example.model-v1:0"
    assert vision["USE_MOCK_BEDROCK"] == "true"
    statements = statements_for_role(
        custom, role_logical_id(functions_by_name(custom)["oerf-evidence-analyzer"])
    )
    invoke = _statements_with_action(statements, "bedrock:InvokeModel")
    assert _resource_list(invoke[0]["Resource"]) == [
        f"arn:aws:bedrock:{REGION}::foundation-model/example.model-v1:0"
    ]


def test_ses_send_is_scoped_to_the_identity_arn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SES_SOURCE_EMAIL", "ops@example.com")
    _stack, scoped = synthesize()
    statements = statements_for_role(
        scoped, role_logical_id(functions_by_name(scoped)["oerf-channel-router"])
    )
    ses = [
        statement
        for statement in statements
        if statement.get("Action") == "ses:SendEmail"
        or (
            isinstance(statement.get("Action"), list) and "ses:SendEmail" in statement["Action"]
        )
    ]
    assert len(ses) == 1
    resource = ses[0]["Resource"]
    if isinstance(resource, str):
        resource = [resource]
    assert resource == [f"arn:aws:ses:{REGION}:{ACCOUNT}:identity/ops@example.com"]


def test_agentcore_runtime_stays_out_of_the_default_synth(template: Template) -> None:
    assert template.find_resources("AWS::BedrockAgentCore::Runtime") == {}
    assert template.find_resources("AWS::ECR::Repository") == {}
    assert "AgentRuntimeArn" not in template.to_json().get("Outputs", {})


def test_agentcore_flag_scopes_the_runtime_and_the_caller() -> None:
    saved = _without_synth_env()
    try:
        _stack, enabled = synthesize(context={"agentcore:enabled": "true"})
    finally:
        _restore_env(saved)

    runtimes = enabled.find_resources("AWS::BedrockAgentCore::Runtime")
    assert len(runtimes) == 1
    runtime = next(iter(runtimes.values()))["Properties"]
    assert runtime["AgentRuntimeName"] == "oerf_agent"
    assert runtime["NetworkConfiguration"]["NetworkMode"] == "PUBLIC"
    assert runtime["LifecycleConfiguration"]["IdleRuntimeSessionTimeout"] == 60
    assert runtime["LifecycleConfiguration"]["MaxLifetime"] == 300
    assert "ContainerUri" in runtime["AgentRuntimeArtifact"]["ContainerConfiguration"]
    variables = runtime["EnvironmentVariables"]
    assert variables["BEDROCK_MODEL_ID"] == "amazon.nova-lite-v1:0"
    assert variables["GUARDRAIL_ID"]["Fn::GetAtt"][1] == "GuardrailId"
    assert variables["GUARDRAIL_VERSION"]["Fn::GetAtt"][1] == "Version"

    assert enabled.find_resources("AWS::ECR::Repository") == {}

    execution = _role_by_name(enabled, "oerf-agent-runtime")
    actions = action_names(statements_for_role(enabled, execution))
    for prefix in ("states:", "social-messaging:", "sms-voice:", "ses:", "sns:"):
        matched = [name for name in actions if name.startswith(prefix)]
        assert matched == [], matched
    writes = {
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:BatchWriteItem",
    }
    assert actions.isdisjoint(writes)
    assert "bedrock:InvokeModel" in actions
    assert "bedrock:ApplyGuardrail" in actions
    statements = statements_for_role(enabled, execution)
    invoke = _statements_with_action(statements, "bedrock:InvokeModel")
    assert _resource_list(invoke[0]["Resource"]) == [
        f"arn:aws:bedrock:{REGION}::foundation-model/amazon.nova-lite-v1:0"
    ]
    apply = _statements_with_action(statements, "bedrock:ApplyGuardrail")
    _assert_apply_guardrail(apply[0], "us.guardrail.v1:0")
    for action in ("ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"):
        pull = _statements_with_action(statements, action)
        assert len(pull) == 1
        assert _resource_list(pull[0]["Resource"]) == [
            _asset_repository_arn(),
        ]
    assert "ecr:PutImage" not in actions
    token = _statements_with_action(statements, "ecr:GetAuthorizationToken")
    assert token[0]["Resource"] == "*"

    caller = _role_by_name(enabled, "oerf-agent-caller")
    caller_actions = action_names(statements_for_role(enabled, caller))
    assert caller_actions == {"bedrock-agentcore:InvokeAgentRuntime"}
    invoke_runtime = _statements_with_action(
        statements_for_role(enabled, caller), "bedrock-agentcore:InvokeAgentRuntime"
    )
    assert invoke_runtime[0]["Resource"]["Fn::GetAtt"][1] == "AgentRuntimeArn"
    output = enabled.to_json()["Outputs"]["AgentRuntimeArn"]
    assert output["Export"]["Name"] == "AGENT-RUNTIME-ARN"

    trust = enabled.find_resources("AWS::IAM::Role")[execution]["Properties"][
        "AssumeRolePolicyDocument"
    ]
    condition = trust["Statement"][0]["Condition"]
    assert condition["StringEquals"]["aws:SourceAccount"] == ACCOUNT
    source_arn = json.dumps(condition["ArnLike"]["aws:SourceArn"])
    assert "bedrock-agentcore" in source_arn
    assert ACCOUNT in source_arn

    exceptions = EXCEPTIONS.read_text(encoding="utf-8")
    assert "OerfColdChainStateMachineRole" in exceptions
    for fragment in (
        "log-group:*",
        "log-group:/aws/bedrock-agentcore/runtimes/*",
        "runtimes/*:log-stream:*",
    ):
        assert fragment in exceptions

    allowed = allowed_star_actions()
    offenders = []
    for statement in iter_allow_statements(enabled):
        if not resource_is_star(statement.get("Resource")):
            continue
        action = statement.get("Action", [])
        names = [action] if isinstance(action, str) else list(action)
        offenders.extend(name for name in names if name not in allowed)
    assert offenders == [], "Resource * is outside iam-exceptions.md: " + ", ".join(offenders)


def _asset_repository_arn() -> dict:
    """CDK bootstrap repository that DockerImageAsset publishes into."""
    name = f"cdk-hnb659fds-container-assets-{ACCOUNT}-{REGION}"
    return {
        "Fn::Join": [
            "",
            [
                "arn:",
                {"Ref": "AWS::Partition"},
                f":ecr:{REGION}:{ACCOUNT}:repository/{name}",
            ],
        ]
    }


def _assert_apply_guardrail(statement: dict, profile_id: str) -> None:
    resources = _resource_list(statement["Resource"])
    assert resources[0]["Fn::GetAtt"][1] == "GuardrailArn"
    assert resources[1:] == [
        f"arn:aws:bedrock:{region}:{ACCOUNT}:guardrail-profile/{profile_id}"
        for region in ("us-east-1", "us-east-2", "us-west-2")
    ]


def _role_by_name(template: Template, role_name: str) -> str:
    for logical_id, role in template.find_resources("AWS::IAM::Role").items():
        if role["Properties"].get("RoleName") == role_name:
            return logical_id
    raise AssertionError(f"no role named {role_name}")


def test_synth_without_cdk_default_account(tmp_path: Path) -> None:
    env = {key: value for key, value in os.environ.items() if key != "CDK_DEFAULT_ACCOUNT"}
    env["CDK_OUTDIR"] = str(tmp_path)
    env["CDK_DEFAULT_REGION"] = REGION
    result = subprocess.run(
        [sys.executable, "-m", "infra.app"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    message = "Refusing to deploy with the synth placeholder account 000000000000"
    for name in ("OerfDataStack.template.json", "OerfComputeStack.template.json"):
        assert message in (tmp_path / name).read_text()
    assert not (tmp_path / "OerfApiStack.template.json").exists()
