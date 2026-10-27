"""CDK assertion tests for the initial OERF data infrastructure.

These tests assert on the synthesized CloudFormation template only and never
make real AWS calls (see guide section 24).
"""

import json
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk.assertions import Template

from infra.stacks.data_stack import OerfDataStack
from infra.step_functions import (
    ASL_ENGAGE_PLACEHOLDERS,
    ASL_STEP_PLACEHOLDERS,
    CHANNEL_ROUTER_NAME,
    ENGAGE_FUNCTION_NAMES,
    STEP_FUNCTION_NAMES,
    WIRED_STATES,
    function_arn,
)

ACCOUNT = "123456789012"
REGION = "us-east-1"

EXPECTED_TABLE_NAMES = (
    "PlatformTable",
    "TenantTable",
    "IncidentTable",
    "RoutingTable",
)
EXPECTED_BUCKET_NAMES = ("EvidenceBucket", "ReportBucket")

COMPOSITE_KEY_SCHEMA = [
    {"AttributeName": "pk", "KeyType": "HASH"},
    {"AttributeName": "sk", "KeyType": "RANGE"},
]
COMPOSITE_ATTRIBUTES = [
    {"AttributeName": "pk", "AttributeType": "S"},
    {"AttributeName": "sk", "AttributeType": "S"},
]


@pytest.fixture(scope="module")
def stack() -> OerfDataStack:
    app = cdk.App()
    return OerfDataStack(
        app,
        "OerfDataStack",
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )


@pytest.fixture(scope="module")
def template(stack: OerfDataStack) -> Template:
    return Template.from_stack(stack)


def test_stack_exposes_construct_attributes(stack: OerfDataStack) -> None:
    assert isinstance(stack.platform_table, dynamodb.Table)
    assert isinstance(stack.tenant_table, dynamodb.Table)
    assert isinstance(stack.incident_table, dynamodb.Table)
    assert isinstance(stack.routing_table, dynamodb.Table)


def test_four_dynamodb_tables_are_created(
    template: Template,
    stack: OerfDataStack,
) -> None:
    template.resource_count_is("AWS::DynamoDB::Table", 4)

    tables = (
        stack.platform_table,
        stack.tenant_table,
        stack.incident_table,
        stack.routing_table,
    )
    logical_ids = [stack.get_logical_id(table.node.default_child) for table in tables]
    resource_ids = set(template.find_resources("AWS::DynamoDB::Table"))

    assert len(set(logical_ids)) == 4
    assert set(logical_ids) <= resource_ids
    # CDK appends a hash suffix to the logical id, so match on the prefix.
    assert all(
        logical_id.startswith(name)
        for logical_id, name in zip(logical_ids, EXPECTED_TABLE_NAMES)
    )


def test_two_s3_buckets_are_created(
    template: Template,
    stack: OerfDataStack,
) -> None:
    template.resource_count_is("AWS::S3::Bucket", 2)

    buckets = (stack.evidence_bucket, stack.report_bucket)
    logical_ids = [
        stack.get_logical_id(bucket.node.default_child) for bucket in buckets
    ]
    resource_ids = set(template.find_resources("AWS::S3::Bucket"))

    assert len(set(logical_ids)) == 2
    assert set(logical_ids) <= resource_ids
    assert all(
        logical_id.startswith(name)
        for logical_id, name in zip(logical_ids, EXPECTED_BUCKET_NAMES)
    )


def test_oerf_event_bus_is_created(template: Template) -> None:
    template.resource_count_is("AWS::Events::EventBus", 1)
    template.has_resource_properties(
        "AWS::Events::EventBus",
        {"Name": "oerf-events"},
    )


def test_cold_chain_state_machine_is_wired(template: Template) -> None:
    template.resource_count_is("AWS::StepFunctions::StateMachine", 2)
    template.has_resource_properties(
        "AWS::StepFunctions::StateMachine",
        {"StateMachineName": "oerf-cold-chain", "StateMachineType": "STANDARD"},
    )
    template.has_resource_properties(
        "AWS::StepFunctions::StateMachine",
        {"StateMachineName": "oerf-engage-party", "StateMachineType": "STANDARD"},
    )


def test_nested_engagement_state_machine_is_deployed_as_standard(template: Template) -> None:
    machines = template.find_resources("AWS::StepFunctions::StateMachine")
    assert len(machines) == 2
    definitions = [json.dumps(item["Properties"]["DefinitionString"]) for item in machines.values()]
    assert any("engage_party_v1" in value for value in definitions)
    assert all("__ENGAGEMENT_STUB_ARN__" not in value for value in definitions)
    parent = next(value for value in definitions if "cold_chain_excursion_v1" in value)
    assert "states:startExecution.sync:2" in parent
    assert "__ENGAGE_PARTY_ARN__" not in parent


def test_cold_chain_workflow_has_a_stub_lambda(template: Template) -> None:
    functions = template.find_resources(
        "AWS::Lambda::Function",
        {"Properties": {"Handler": "workflow_stub.handler"}},
    )
    assert len(functions) == 1
    assert functions[next(iter(functions))]["Properties"]["Runtime"] == "python3.11"


def test_cold_chain_state_machine_wires_real_steps_and_keeps_the_stub(
    template: Template,
) -> None:
    machines = template.find_resources("AWS::StepFunctions::StateMachine")
    definition = next(
        item["Properties"]["DefinitionString"]
        for item in machines.values()
        if "cold_chain_excursion_v1" in json.dumps(item["Properties"]["DefinitionString"])
    )
    definition_string = json.dumps(definition)

    assert "__STUB_ARN__" not in definition_string
    assert "__STEP_" not in definition_string
    assert "cold_chain_excursion_v1" in definition_string
    for name in STEP_FUNCTION_NAMES.values():
        assert function_arn(REGION, ACCOUNT, name) in definition_string
    for state in (
        "OPEN",
        "GatherContext",
        "Diagnose",
        "Assess",
        "SecureFacility",
        "Act",
        "VerifyLoop",
        "GenerateReport",
        "QA",
        "Close",
        "CancelRequested?",
        "Cancel",
        "NoResponse",
        "FailureRoute",
        "RecordFailure",
        "Escalate",
    ):
        assert state in definition_string, state
    for state in WIRED_STATES:
        assert function_arn(REGION, ACCOUNT, STEP_FUNCTION_NAMES[WIRED_STATES[state]]) in (
            definition_string
        )


def _invoke_resources(template: Template, role_prefix: str) -> list:
    resources: list = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        roles = json.dumps(policy["Properties"].get("Roles", []))
        if role_prefix not in roles:
            continue
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = statement.get("Action", [])
            names = [action] if isinstance(action, str) else list(action)
            if "lambda:InvokeFunction" not in names:
                continue
            found = statement["Resource"]
            resources.extend(found if isinstance(found, list) else [found])
    return resources


@pytest.mark.security
def test_cold_chain_role_invokes_only_the_wired_steps_and_the_stub(template: Template) -> None:
    """InvokeFunction stays on the fixed step names plus the stub. No Resource *."""

    expected = set()
    for name in STEP_FUNCTION_NAMES.values():
        arn = function_arn(REGION, ACCOUNT, name)
        expected.add(arn)
        expected.add(f"{arn}:*")
    resources = _invoke_resources(template, "OerfColdChainStateMachineRole")
    strings = {item for item in resources if isinstance(item, str)}
    assert strings == expected
    assert "*" not in strings
    assert len(resources) == len(expected) + 2
    rendered = json.dumps(resources)
    assert "ColdChainWorkflowStub" in rendered
    assert rendered.count(":*") == len(STEP_FUNCTION_NAMES) + 1

    engagement = json.dumps(_invoke_resources(template, "EngagePartyStateMachineRole"))
    assert "oerf-step-" not in engagement
    assert "ColdChainWorkflowStub" in engagement
    assert "oerf-engage-cadence" in engagement
    assert "oerf-engage-interpret" in engagement
    assert "oerf-channel-router" in engagement
    assert "*" not in engagement or '":*"' in engagement
    assert '"*"' not in engagement


_ASL_DIR = Path(__file__).resolve().parents[2] / "domains" / "cold_chain_excursion"
_SYNC_RESOURCES = {
    "arn:aws:states:::states:startExecution.sync",
    "arn:aws:states:::states:startExecution.sync:2",
}
_LAMBDA_WAIT = "arn:aws:states:::lambda:invoke.waitForTaskToken"
_MANAGED_RULE = "rule/StepFunctionsGetEventsForStepFunctionsExecutionRule"
_ASL_TO_MACHINE = (
    ("statemachine.asl.json", "cold_chain_excursion_v1"),
    ("engage_party.asl.json", "engage_party_v1"),
)


def _function_marker(token: str) -> str:
    if token in {"__STUB_ARN__", "__ENGAGEMENT_STUB_ARN__"}:
        return "ColdChainWorkflowStub"
    if token == "__CHANNEL_ROUTER_ARN__":
        return f"function:{CHANNEL_ROUTER_NAME}"
    if token in ASL_STEP_PLACEHOLDERS:
        return f"function:{STEP_FUNCTION_NAMES[ASL_STEP_PLACEHOLDERS[token]]}"
    if token in ASL_ENGAGE_PLACEHOLDERS:
        return f"function:{ENGAGE_FUNCTION_NAMES[ASL_ENGAGE_PLACEHOLDERS[token]]}"
    if token.startswith("arn:aws:lambda:"):
        return token
    raise AssertionError(f"unknown Lambda token {token}")


def _state_machine_name(token: str) -> str:
    if token == "__ENGAGE_PARTY_ARN__":
        return "oerf-engage-party"
    marker = ":stateMachine:"
    if marker in token:
        return token.rsplit(marker, 1)[1]
    raise AssertionError(f"unknown state machine token {token}")


def _required_grants(state: dict) -> list[tuple[str, str]] | None:
    """Actions this integration needs on the state machine role.

    ``None`` means the resource is not a .sync or .waitForTaskToken task.
    An unknown integration raises so a new service cannot pass uncoded.
    """

    resource = state.get("Resource", "")
    if resource in _SYNC_RESOURCES:
        # https://docs.aws.amazon.com/step-functions/latest/dg/connect-stepfunctions.html
        name = _state_machine_name(state["Parameters"]["StateMachineArn"])
        return [
            ("states:StartExecution", f"stateMachine:{name}"),
            ("states:DescribeExecution", f":execution:{name}:"),
            ("states:StopExecution", f":execution:{name}:"),
            ("events:PutTargets", _MANAGED_RULE),
            ("events:PutRule", _MANAGED_RULE),
            ("events:DescribeRule", _MANAGED_RULE),
        ]
    if resource == _LAMBDA_WAIT:
        # https://docs.aws.amazon.com/step-functions/latest/dg/connect-lambda.html
        # Callback invoke needs lambda:InvokeFunction. SendTask* is the caller's.
        marker = _function_marker(state["Parameters"]["FunctionName"])
        return [("lambda:InvokeFunction", marker)]
    if (
        resource.endswith(".sync:2")
        or resource.endswith(".sync")
        or resource.endswith(".waitForTaskToken")
    ):
        raise AssertionError(f"no permission set encoded for {resource}")
    return None


def _iter_tasks(machine: dict):
    for name, state in machine.get("States", {}).items():
        if not isinstance(state, dict):
            continue
        if state.get("Type") == "Task":
            yield name, state
        branches = state.get("Branches")
        if isinstance(branches, list):
            for branch in branches:
                if isinstance(branch, dict):
                    yield from _iter_tasks(branch)
        for field in ("ItemProcessor", "Iterator"):
            nested = state.get(field)
            if isinstance(nested, dict) and "States" in nested:
                yield from _iter_tasks(nested)


def _role_logical_id(machine: dict) -> str:
    role = machine["Properties"]["RoleArn"]
    attribute = role["Fn::GetAtt"]
    if isinstance(attribute, str):
        return attribute.split(".", 1)[0]
    return attribute[0]


def _resource_labels(logical: str, resources: dict) -> list[str]:
    labels = [logical]
    props = resources[logical]["Properties"]
    if "StateMachineName" in props:
        labels.append(f"stateMachine:{props['StateMachineName']}")
    function_name = props.get("FunctionName")
    if isinstance(function_name, str):
        labels.append(f"function:{function_name}")
    return labels


def _flatten_resource(value: object, resources: dict) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        found: list[str] = []
        for item in value:
            found.extend(_flatten_resource(item, resources))
        return found
    if not isinstance(value, dict):
        return [json.dumps(value)]
    if "Ref" in value and value["Ref"] in resources:
        return _resource_labels(value["Ref"], resources)
    if "Fn::GetAtt" in value:
        attribute = value["Fn::GetAtt"]
        logical = attribute.split(".", 1)[0] if isinstance(attribute, str) else attribute[0]
        return _resource_labels(logical, resources)
    if "Fn::Join" in value:
        separator, parts = value["Fn::Join"]
        rendered: list[str] = []
        for part in parts:
            if isinstance(part, str):
                rendered.append(part)
            elif isinstance(part, dict) and "Ref" in part:
                rendered.append(
                    {"AWS::Region": REGION, "AWS::AccountId": ACCOUNT}.get(part["Ref"], "")
                )
            else:
                rendered.append("".join(_flatten_resource(part, resources)))
        return [separator.join(rendered)]
    return [json.dumps(value)]


def _grant_covers(statements: list[dict], action: str, marker: str, resources: dict) -> bool:
    for statement in statements:
        if statement.get("Effect", "Allow") == "Deny":
            continue
        names = _as_list(statement.get("Action", []))
        if action not in names:
            continue
        for resource in _flatten_resource(statement.get("Resource"), resources):
            if marker in resource:
                return True
    return False


@pytest.mark.security
def test_sync_and_task_token_tasks_have_integration_permissions(template: Template) -> None:
    """Each .sync and .waitForTaskToken task is granted what that integration needs."""

    resources = template.to_json()["Resources"]
    machines = template.find_resources("AWS::StepFunctions::StateMachine")
    missing: list[str] = []
    seen = 0
    for filename, comment in _ASL_TO_MACHINE:
        definition = json.loads((_ASL_DIR / filename).read_text(encoding="utf-8"))
        matches = [
            machine
            for machine in machines.values()
            if comment in json.dumps(machine["Properties"].get("DefinitionString", ""))
        ]
        assert len(matches) == 1, comment
        statements = _statements_for_role(template, _role_logical_id(matches[0]))
        for name, state in _iter_tasks(definition):
            required = _required_grants(state)
            if required is None:
                continue
            seen += 1
            resource = state["Resource"]
            for action, marker in required:
                if not _grant_covers(statements, action, marker, resources):
                    missing.append(f"{filename} {name} ({resource}) missing {action} on {marker}")
    assert seen > 0
    assert not missing, "\n".join(missing)


def test_cold_chain_workflow_tasks_retry_and_catch_failures(
    template: Template,
) -> None:
    machines = template.find_resources("AWS::StepFunctions::StateMachine")
    definition = next(
        item["Properties"]["DefinitionString"]
        for item in machines.values()
        if "cold_chain_excursion_v1" in json.dumps(item["Properties"]["DefinitionString"])
    )
    definition_string = json.dumps(definition)

    assert "Lambda.TooManyRequestsException" in definition_string
    assert "States.ALL" in definition_string
    assert "RecordFailure" in definition_string
    assert "waitForTaskToken" in definition_string
    assert "$$.Task.Token" in definition_string
    assert "NoResponse" in definition_string
    assert "Cancel" in definition_string


def test_dynamodb_tables_use_on_demand_billing(template: Template) -> None:
    for logical_id, table in template.find_resources("AWS::DynamoDB::Table").items():
        assert table["Properties"]["BillingMode"] == "PAY_PER_REQUEST", logical_id


def test_dynamodb_tables_use_pk_sk_composite_key(template: Template) -> None:
    gsi_attributes = [
        *COMPOSITE_ATTRIBUTES,
        {"AttributeName": "GSI1PK", "AttributeType": "S"},
        {"AttributeName": "GSI1SK", "AttributeType": "S"},
    ]
    for logical_id, table in template.find_resources("AWS::DynamoDB::Table").items():
        assert table["Properties"]["KeySchema"] == COMPOSITE_KEY_SCHEMA, logical_id
        expected = (
            gsi_attributes if logical_id.startswith("IncidentTable") else COMPOSITE_ATTRIBUTES
        )
        assert table["Properties"]["AttributeDefinitions"] == expected, logical_id


def test_dynamodb_tables_have_point_in_time_recovery(template: Template) -> None:
    for logical_id, table in template.find_resources("AWS::DynamoDB::Table").items():
        assert table["Properties"]["PointInTimeRecoverySpecification"] == {
            "PointInTimeRecoveryEnabled": True
        }, logical_id


def test_ttl_expires_at_is_enabled_only_on_incident_and_routing(
    template: Template,
) -> None:
    """INGEST#, DEBOUNCE#, INMSG# and OUTMSG# expire. Platform and tenant do not."""
    specification = {"AttributeName": "expires_at", "Enabled": True}
    enabled: list[str] = []
    for logical_id, table in template.find_resources("AWS::DynamoDB::Table").items():
        spec = table["Properties"].get("TimeToLiveSpecification")
        if logical_id.startswith(("IncidentTable", "RoutingTable")):
            assert spec == specification, logical_id
            enabled.append(logical_id)
        else:
            assert spec is None, logical_id
    assert len(enabled) == 2


def test_incident_table_defines_exactly_one_gsi_by_status(
    template: Template,
) -> None:
    gsi1 = {
        "IndexName": "GSI1",
        "KeySchema": [
            {"AttributeName": "GSI1PK", "KeyType": "HASH"},
            {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "ALL"},
    }
    found: list[tuple[str, dict]] = []
    for logical_id, table in template.find_resources("AWS::DynamoDB::Table").items():
        indexes = table["Properties"].get("GlobalSecondaryIndexes", [])
        assert len(indexes) <= 1, logical_id
        found.extend((logical_id, index) for index in indexes)

    assert len(found) == 1
    logical_id, index = found[0]
    assert logical_id.startswith("IncidentTable"), logical_id
    assert index == gsi1


@pytest.mark.security
def test_s3_buckets_block_public_access(template: Template) -> None:
    for logical_id, bucket in template.find_resources("AWS::S3::Bucket").items():
        assert bucket["Properties"]["PublicAccessBlockConfiguration"] == {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }, logical_id


def test_s3_buckets_have_versioning_enabled(template: Template) -> None:
    for logical_id, bucket in template.find_resources("AWS::S3::Bucket").items():
        assert bucket["Properties"]["VersioningConfiguration"] == {
            "Status": "Enabled"
        }, logical_id


def test_s3_buckets_use_aws_managed_kms_and_bucket_keys(template: Template) -> None:
    """Evidence and report use aws/s3. No customer-managed key id is set."""

    buckets = template.find_resources("AWS::S3::Bucket")
    assert len(buckets) == 2
    for logical_id, bucket in buckets.items():
        rule = bucket["Properties"]["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
        default = rule["ServerSideEncryptionByDefault"]
        assert default["SSEAlgorithm"] == "aws:kms", logical_id
        assert "KMSMasterKeyID" not in default, logical_id
        assert rule["BucketKeyEnabled"] is True, logical_id


def test_tables_omit_encryption_by_default(template: Template) -> None:
    """The default leaves the AWS owned key. No SSESpecification is added."""

    tables = template.find_resources("AWS::DynamoDB::Table")
    assert len(tables) == 4
    for logical_id, table in tables.items():
        assert "SSESpecification" not in table["Properties"], logical_id


def test_tables_use_the_aws_managed_key_when_kms_managed() -> None:
    """dynamodb:kmsManaged=true sets aws/dynamodb. A key id would be a CMK."""

    app = cdk.App(context={"dynamodb:kmsManaged": "true"})
    kms_stack = OerfDataStack(
        app,
        "OerfDataStack",
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )
    kms_template = Template.from_stack(kms_stack)
    tables = kms_template.find_resources("AWS::DynamoDB::Table")
    assert len(tables) == 4
    for logical_id, table in tables.items():
        spec = table["Properties"]["SSESpecification"]
        assert spec["SSEEnabled"] is True, logical_id
        assert spec.get("SSEType") == "KMS", logical_id
        assert "KMSMasterKeyId" not in spec, logical_id


@pytest.mark.security
def test_no_role_is_granted_kms(template: Template) -> None:
    """aws/dynamodb and aws/s3 do not need a kms grant on the caller."""

    assert _allow_actions(template, "kms:") == []


def test_user_pool_keeps_its_id_and_adds_tenant_claim_outside_schema(
    template: Template,
) -> None:
    pools = template.find_resources("AWS::Cognito::UserPool")
    assert len(pools) == 1
    logical_id, pool = next(iter(pools.items()))
    properties = pool["Properties"]

    assert logical_id.startswith("OerfUserPool")
    assert properties["UserPoolName"] == "oerf-users"
    assert "Schema" not in properties
    assert properties["UserPoolTier"] == "ESSENTIALS"
    assert pool["DeletionPolicy"] == "Retain"
    assert pool["UpdateReplacePolicy"] == "Retain"
    pre_token = properties["LambdaConfig"]["PreTokenGenerationConfig"]
    assert pre_token["LambdaVersion"] == "V2_0"
    assert "PreTokenGeneration" not in properties["LambdaConfig"]
    policy = properties["Policies"]["PasswordPolicy"]
    assert policy["MinimumLength"] == 12
    assert policy["RequireUppercase"] is True
    assert policy["RequireLowercase"] is True
    assert policy["RequireNumbers"] is True
    assert policy["RequireSymbols"] is True
    assert policy["TemporaryPasswordValidityDays"] == 3
    assert "UnusedAccountValidityDays" not in json.dumps(properties)
    assert properties["MfaConfiguration"] == "OPTIONAL"
    assert properties["EnabledMfas"] == ["SOFTWARE_TOKEN_MFA"]
    assert "SmsConfiguration" not in properties
    assert "UserPoolAddOns" not in properties


def test_tenant_attribute_custom_resource_targets_the_existing_pool(
    template: Template,
) -> None:
    resources = template.find_resources("AWS::CloudFormation::CustomResource")
    assert len(resources) == 1
    properties = next(iter(resources.values()))["Properties"]
    assert "UserPoolId" in properties
    assert set(properties) >= {"ServiceToken", "UserPoolId"}


@pytest.mark.security
def test_tenant_attribute_role_is_limited_to_the_user_pool(template: Template) -> None:
    statements = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if "cognito-idp:AddCustomAttributes" in actions:
                statements.append(statement)

    assert len(statements) == 1
    statement = statements[0]
    assert statement["Action"] == [
        "cognito-idp:DescribeUserPool",
        "cognito-idp:AddCustomAttributes",
    ]
    assert statement["Resource"] != "*"
    assert "Fn::GetAtt" in statement["Resource"] or isinstance(statement["Resource"], dict)


def test_persistent_resources_use_retain_removal_policy(
    template: Template,
) -> None:
    persistent_resources = {
        **template.find_resources("AWS::DynamoDB::Table"),
        **template.find_resources("AWS::S3::Bucket"),
    }
    assert len(persistent_resources) == 6
    for logical_id, resource in persistent_resources.items():
        assert resource["DeletionPolicy"] == "Retain", logical_id
        assert resource["UpdateReplacePolicy"] == "Retain", logical_id

def test_workflow_stub_traces_and_keeps_logs_for_thirty_days(template: Template) -> None:
    functions = [
        resource["Properties"]
        for resource in template.find_resources("AWS::Lambda::Function").values()
        if resource["Properties"].get("Handler") == "workflow_stub.handler"
    ]
    assert len(functions) == 1
    props = functions[0]
    assert props["TracingConfig"] == {"Mode": "Active"}
    log_group_id = props["LoggingConfig"]["LogGroup"]["Ref"]
    group = template.find_resources("AWS::Logs::LogGroup")[log_group_id]
    assert group["Properties"]["RetentionInDays"] == 30


def test_cold_chain_state_machine_has_tracing(template):
    template.has_resource_properties(
        "AWS::StepFunctions::StateMachine",
        {"StateMachineName": "oerf-cold-chain", "TracingConfiguration": {"Enabled": True}},
    )


@pytest.mark.security
def test_data_buckets_deny_insecure_transport_and_expire(template: Template) -> None:
    """Evidence expires. Reports stay. Both buckets refuse plain HTTP."""

    buckets = template.find_resources("AWS::S3::Bucket")
    evidence = _bucket(buckets, "EvidenceBucket")
    report = _bucket(buckets, "ReportBucket")
    evidence_rule = evidence["LifecycleConfiguration"]["Rules"][0]
    report_rule = report["LifecycleConfiguration"]["Rules"][0]
    assert evidence_rule["ExpirationInDays"] == 90
    assert evidence_rule["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}
    assert "ExpirationInDays" not in report_rule
    assert "ExpirationDate" not in report_rule
    assert report_rule["NoncurrentVersionExpiration"] == {"NoncurrentDays": 90}

    policies = template.find_resources("AWS::S3::BucketPolicy")
    assert len(policies) == 2
    for policy in policies.values():
        statements = policy["Properties"]["PolicyDocument"]["Statement"]
        assert any(_denies_insecure_transport(item) for item in statements)


@pytest.mark.security
def test_cold_chain_sampling_is_an_iam_exception(template: Template) -> None:
    path = Path(__file__).resolve().parents[2] / "docs" / "security" / "iam-exceptions.md"
    text = path.read_text(encoding="utf-8")
    assert "OerfColdChainStateMachineRole" in text
    found = False
    for policy in template.find_resources("AWS::IAM::Policy").values():
        roles = json.dumps(policy["Properties"].get("Roles", []))
        if "OerfColdChainStateMachineRole" not in roles:
            continue
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = statement.get("Action", [])
            names = [action] if isinstance(action, str) else list(action)
            if "xray:GetSamplingRules" in names and "xray:GetSamplingTargets" in names:
                assert statement["Resource"] == "*"
                found = True
    assert found


def test_both_state_machines_trace_and_log_errors_without_payloads(
    template: Template,
) -> None:
    machines = template.find_resources("AWS::StepFunctions::StateMachine")
    assert len(machines) == 2
    log_groups = {
        logical_id: group
        for logical_id, group in template.find_resources("AWS::Logs::LogGroup").items()
        if "StateMachine" in logical_id
    }
    assert len(log_groups) == 2
    for group in log_groups.values():
        assert group["Properties"]["RetentionInDays"] == 30

    for machine in machines.values():
        props = machine["Properties"]
        assert props["TracingConfiguration"] == {"Enabled": True}
        logging = props["LoggingConfiguration"]
        assert logging["Level"] == "ERROR"
        assert logging["IncludeExecutionData"] is False
        destination = logging["Destinations"][0]["CloudWatchLogsLogGroup"]["LogGroupArn"]
        assert isinstance(destination, dict)


_GITHUB_OIDC_SUBS = [
    "repo:flabdev@25605151/cds-oerf-platform@1408373494:ref:refs/heads/main",
    "repo:flabdev/cds-oerf-platform:ref:refs/heads/main",
]
_BOOTSTRAP_ROLE_ARN = (
    f"arn:aws:iam::{ACCOUNT}:role/cdk-hnb659fds-*-{ACCOUNT}-{REGION}"
)


def _allow_actions(template: Template, prefix: str) -> list[str]:
    found: list[str] = []
    documents = [
        resource["Properties"]["PolicyDocument"]
        for resource in template.find_resources("AWS::IAM::Policy").values()
    ]
    for resource in template.find_resources("AWS::IAM::Role").values():
        for inline in resource["Properties"].get("Policies", []):
            documents.append(inline["PolicyDocument"])
    for document in documents:
        for statement in document.get("Statement", []):
            if statement.get("Effect") == "Deny":
                continue
            action = statement.get("Action", [])
            names = [action] if isinstance(action, str) else list(action)
            found.extend(name for name in names if name.startswith(prefix))
    return found


def _bucket(buckets: dict, prefix: str) -> dict:
    matches = [
        resource["Properties"]
        for logical_id, resource in buckets.items()
        if logical_id.startswith(prefix)
    ]
    assert len(matches) == 1
    return matches[0]


def _denies_insecure_transport(statement: dict) -> bool:
    if statement.get("Effect") != "Deny":
        return False
    flag = statement.get("Condition", {}).get("Bool", {}).get("aws:SecureTransport")
    return flag in ("false", False)


def _github_deploy_role(template: Template) -> tuple[str, dict]:
    found = [
        (logical_id, role)
        for logical_id, role in template.find_resources("AWS::IAM::Role").items()
        if role["Properties"].get("RoleName") == "OerfGitHubDeployRole"
    ]
    assert len(found) == 1
    return found[0]


def _statements_for_role(template: Template, role_logical_id: str) -> list[dict]:
    statements: list[dict] = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        roles = policy["Properties"].get("Roles", [])
        if {"Ref": role_logical_id} not in roles:
            continue
        document = policy["Properties"]["PolicyDocument"]["Statement"]
        if isinstance(document, dict):
            statements.append(document)
        else:
            statements.extend(document)
    return statements


def _as_list(value: str | list) -> list:
    return value if isinstance(value, list) else [value]


@pytest.mark.security
def test_github_deploy_role_trusts_only_main(template: Template) -> None:
    _logical_id, role = _github_deploy_role(template)
    document = role["Properties"]["AssumeRolePolicyDocument"]
    encoded = json.dumps(document)
    assert "StringLike" not in encoded
    assert "*" not in encoded
    statements = document["Statement"]
    assert len(statements) == 1
    condition = statements[0]["Condition"]
    assert set(condition) == {"StringEquals"}
    string_equals = condition["StringEquals"]
    assert string_equals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    assert string_equals["token.actions.githubusercontent.com:sub"] == _GITHUB_OIDC_SUBS


@pytest.mark.security
def test_github_deploy_role_assumes_only_bootstrap_roles(template: Template) -> None:
    logical_id, role = _github_deploy_role(template)
    assert "ManagedPolicyArns" not in role["Properties"]
    assert "AdministratorAccess" not in json.dumps(template.to_json())

    statements = _statements_for_role(template, logical_id)
    assert len(statements) == 1
    statement = statements[0]
    assert statement["Effect"] == "Allow"
    assert _as_list(statement["Action"]) == ["sts:AssumeRole"]
    assert _as_list(statement["Resource"]) == [_BOOTSTRAP_ROLE_ARN]