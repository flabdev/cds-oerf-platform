"""Alarm topic, thresholds, and the operations dashboard. No AWS calls."""

import json
import re
from collections import Counter

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infra.stacks.api_stack import OerfApiStack
from infra.stacks.compute_stack import OerfComputeStack
from infra.stacks.data_stack import OerfDataStack
from infra.stacks.observability import OerfObservabilityStack, error_alarm_id, lambda_functions

ACCOUNT = "123456789012"
REGION = "us-east-1"
_NAMED_FUNCTIONS = {
    "oerf-channel-router",
    "oerf-evidence-analyzer",
    "oerf-report-generator",
    "oerf-ws-handler",
    "oerf-ws-authorizer",
    "oerf-stream-broadcaster",
    "oerf-action-handler",
    "oerf-inbound",
    "oerf-ingest",
    "oerf-pre-token",
    "oerf-tenant-attribute",
}


def _build(context: dict | None = None, *, dashboard: bool = False):
    app = cdk.App(context=context)
    env = cdk.Environment(account=ACCOUNT, region=REGION)
    data = OerfDataStack(app, "OerfDataStack", env=env)
    compute = OerfComputeStack(app, "OerfComputeStack", data_stack=data, env=env)
    scopes = [data, compute]
    if dashboard:
        api = OerfApiStack(
            app,
            "OerfApiStack",
            data_stack=data,
            inbound_queue=compute.inbound_queue,
            websocket_url=compute.ws_stage.url,
            env=env,
        )
        scopes.append(api)
    functions = lambda_functions(*scopes)
    queues = [compute.inbound_dlq, compute.ingest_dlq, compute.stream_dlq]
    machines = [data.cold_chain_state_machine, data.engage_party_state_machine]
    stack = OerfObservabilityStack(
        app,
        "OerfObservabilityStack",
        functions=functions,
        dead_letter_queues=queues,
        state_machines=machines,
        channel_router_log_group=compute.channel_router_fn.log_group,
        env=env,
    )
    names = {
        resource["Properties"]["FunctionName"]
        for scope in scopes
        for resource in Template.from_stack(scope).find_resources("AWS::Lambda::Function").values()
        if isinstance(resource["Properties"].get("FunctionName"), str)
    }
    return Template.from_stack(stack), functions, queues, machines, names


@pytest.fixture(scope="module")
def built():
    return _build()


def test_alarms_cover_every_dlq_machine_and_lambda(built) -> None:
    template, functions, queues, machines, names = built
    assert _NAMED_FUNCTIONS <= names
    expected = len(queues) + (2 * len(machines)) + len(functions) + 1
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    assert len(alarms) == expected
    metrics = [
        alarm["Properties"]["MetricName"]
        for alarm in alarms.values()
        if "MetricName" in alarm["Properties"]
    ]
    assert metrics.count("ApproximateNumberOfMessagesVisible") == len(queues)
    assert metrics.count("ExecutionsFailed") == len(machines)
    assert metrics.count("ExecutionsTimedOut") == len(machines)
    assert metrics.count("Errors") == len(functions)
    _assert_unique_function_alarms_keep_their_ids(alarms, functions)

    topic = next(iter(template.find_resources("AWS::SNS::Topic")))
    for alarm in alarms.values():
        props = alarm["Properties"]
        assert props["Threshold"] == 0
        assert props["ComparisonOperator"] == "GreaterThanThreshold"
        assert props["TreatMissingData"] == "notBreaching"
        assert props["EvaluationPeriods"] == 1
        assert _period(props) == 300
        assert props["AlarmActions"] == [{"Ref": topic}]

    template.resource_count_is("AWS::SNS::Subscription", 0)
    body = json.dumps(template.find_resources("AWS::CloudWatch::Dashboard"))
    for name in (
        "ApproximateNumberOfMessagesVisible",
        "ExecutionsFailed",
        "ExecutionsTimedOut",
        "Errors",
        "DeliveryFailures",
    ):
        assert name in body


def test_email_subscription_comes_only_from_context() -> None:
    template, _functions, _queues, _machines, _names = _build(
        context={"alarms:email": "ops@example.com"}
    )
    template.resource_count_is("AWS::SNS::Subscription", 1)
    template.has_resource_properties(
        "AWS::SNS::Subscription",
        {"Protocol": "email", "Endpoint": "ops@example.com"},
    )


def test_waba_providers_each_get_one_errors_alarm() -> None:
    """Two Provider framework-onEvent functions must synth as two alarms."""

    template, functions, _queues, _machines, _names = _build(
        context={"whatsapp:wabaId": "waba-0000000000000000"}
    )
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    errors = {
        logical_id: alarm
        for logical_id, alarm in alarms.items()
        if alarm["Properties"].get("MetricName") == "Errors"
    }
    assert len(errors) == len(functions)
    assert len({_function_dimension(alarm["Properties"]) for alarm in errors.values()}) == len(
        functions
    )
    framework = [fn for fn in functions if fn.node.id == "framework-onEvent"]
    assert len(framework) == 2
    prefixes = {
        re.sub(r"[^A-Za-z0-9]", "", error_alarm_id(fn, functions)) for fn in framework
    }
    assert len(prefixes) == 2
    for prefix in prefixes:
        assert any(logical_id.startswith(prefix) for logical_id in errors)


def test_dashboard_lambdas_get_error_alarms() -> None:
    _default, default_functions, _queues, _machines, _names = _build()
    template, functions, _queues, _machines, _names = _build(dashboard=True)
    assert len(functions) == len(default_functions) + 2
    errors = [
        alarm["Properties"]["MetricName"]
        for alarm in template.find_resources("AWS::CloudWatch::Alarm").values()
        if "MetricName" in alarm["Properties"]
    ]
    assert errors.count("Errors") == len(functions)


def _assert_unique_function_alarms_keep_their_ids(alarms: dict, functions: list) -> None:
    """A function whose node id is unique keeps the ``{id}Errors`` alarm."""

    counts = Counter(fn.node.id for fn in functions)
    for fn in functions:
        if counts[fn.node.id] != 1:
            continue
        prefix = re.sub(r"[^A-Za-z0-9]", "", error_alarm_id(fn, functions))
        assert any(logical_id.startswith(prefix) for logical_id in alarms), fn.node.path


def _function_dimension(props: dict) -> str:
    names = [
        dimension["Value"]
        for dimension in props.get("Dimensions", [])
        if dimension.get("Name") == "FunctionName"
    ]
    assert len(names) == 1
    return json.dumps(names[0], sort_keys=True)


def _period(props: dict) -> int:
    if "Period" in props:
        return props["Period"]
    returned = [item for item in props["Metrics"] if item.get("ReturnData", True)]
    return returned[0]["Period"]


def test_channel_delivery_filter_counts_failure_outcomes(built) -> None:
    """Router failure outcomes are error and not_sent. sent and simulated are not."""

    template, _functions, _queues, _machines, _names = built
    filters = list(template.find_resources("AWS::Logs::MetricFilter").values())
    assert len(filters) == 2
    pattern = '{ ($.outcome = "error") || ($.outcome = "not_sent") }'
    transformations = []
    for resource in filters:
        props = resource["Properties"]
        assert props["FilterPattern"] == pattern
        assert "ChannelRouterFunctionLogGroup" in json.dumps(props["LogGroupName"])
        transformations.append(props["MetricTransformations"][0])
    for transformation in transformations:
        assert transformation["MetricNamespace"] == "OERF/Channels"
        assert transformation["MetricName"] == "DeliveryFailures"
        assert transformation["MetricValue"] == "1"
    plain = [item for item in transformations if "Dimensions" not in item]
    by_channel = [item for item in transformations if "Dimensions" in item]
    assert len(plain) == 1
    assert len(by_channel) == 1
    assert by_channel[0]["Dimensions"] == [{"Key": "channel", "Value": "$.channel"}]

    alarms = [
        alarm["Properties"]
        for alarm in template.find_resources("AWS::CloudWatch::Alarm").values()
        if alarm["Properties"].get("MetricName") == "DeliveryFailures"
    ]
    assert len(alarms) == 1
    alarm = alarms[0]
    assert alarm["Namespace"] == "OERF/Channels"
    assert alarm["Statistic"] == "Sum"
    assert alarm["Threshold"] == 0
    assert alarm["ComparisonOperator"] == "GreaterThanThreshold"
    assert alarm["TreatMissingData"] == "notBreaching"
    assert alarm["Period"] == 300
    assert "Dimensions" not in alarm
    topic = next(iter(template.find_resources("AWS::SNS::Topic")))
    assert alarm["AlarmActions"] == [{"Ref": topic}]
    body = json.dumps(template.find_resources("AWS::CloudWatch::Dashboard"))
    assert "OERF/Channels,channel" in body
