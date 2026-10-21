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
        "env": {"INCIDENT_TABLE_NAME", "BEDROCK_MODEL_ID", "USE_MOCK_BEDROCK"},
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
            "STATE_MACHINE_ARN",
            "INBOUND_QUEUE_URL",
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
    assert set(functions) == set(EXPECTED_FUNCTIONS)
    template.resource_count_is("AWS::Lambda::Function", len(EXPECTED_FUNCTIONS))

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
    assert len(log_groups) == len(EXPECTED_FUNCTIONS)
    for group in log_groups.values():
        assert group["Properties"]["RetentionInDays"] == 30

    referenced = set()
    for props in functions.values():
        assert props["TracingConfig"] == {"Mode": "Active"}
        log_group = props["LoggingConfig"]["LogGroup"]
        assert "Ref" in log_group
        referenced.add(log_group["Ref"])
    assert referenced == set(log_groups)


def test_star_resources_are_listed_in_the_exceptions_file(template: Template) -> None:
    allowed = allowed_star_actions()
    assert allowed, f"{EXCEPTIONS} does not list any IAM actions"
    offenders = []
    for statement in iter_allow_statements(template):
        if not resource_is_star(statement.get("Resource")):
            continue
        action = statement.get("Action", [])
        names = [action] if isinstance(action, str) else list(action)
        for name in names:
            if name not in allowed:
                offenders.append(name)
    assert offenders == [], (
        "Resource * is outside docs/security/iam-exceptions.md: " + ", ".join(offenders)
    )


def test_send_task_success_is_on_inbound_and_the_compat_action_role(template: Template) -> None:
    inbound = role_actions(template, "oerf-inbound")
    action = role_actions(template, "oerf-action-handler")
    assert "states:SendTaskSuccess" in inbound
    assert "states:SendTaskFailure" in inbound
    assert "states:SendTaskSuccess" in action
    assert "states:SendTaskFailure" in action
    assert "states:SendTaskSuccess" not in role_actions(template, "oerf-ingest")
    assert "sqs:SendMessage" in action


def test_action_role_loses_task_token_when_compat_flag_is_false() -> None:
    saved = _without_synth_env()
    try:
        _stack, flagged = synthesize(context={"compat:actionHandlerTaskToken": "false"})
    finally:
        _restore_env(saved)

    action = role_actions(flagged, "oerf-action-handler")
    inbound = role_actions(flagged, "oerf-inbound")
    assert "states:SendTaskSuccess" not in action
    assert "states:SendTaskFailure" not in action
    assert "states:SendTaskSuccess" in inbound
    assert "sqs:SendMessage" in action


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


def test_inbound_event_source_reports_batch_item_failures(template: Template) -> None:
    mappings = template.find_resources("AWS::Lambda::EventSourceMapping")
    partial = [
        mapping["Properties"]
        for mapping in mappings.values()
        if "ReportBatchItemFailures" in mapping["Properties"].get("FunctionResponseTypes", [])
    ]
    assert len(partial) == 1
    assert "dynamodb" not in json.dumps(partial[0]).lower()


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
