"""Alarms for dead-letter queues, state machines, and Lambda errors.

The topic has no email subscription unless context ``alarms:email`` is set.
Missing metrics do not alarm: a quiet queue is healthy.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from aws_cdk import (
    Duration,
    Stack,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_sns as sns,
    aws_sns_subscriptions as subscriptions,
    aws_sqs as sqs,
    aws_stepfunctions as sfn,
)
from constructs import Construct

from infra.naming import Stage

_PERIOD = Duration.minutes(5)
# router.py logs these outcome values when a send or the route fails.
# "sent" and "simulated" are not failures. There is no outcome "failed".
_DELIVERY_FAILURE_OUTCOMES = ("error", "not_sent")


def lambda_functions(*scopes: Construct) -> list[lambda_.Function]:
    """Every Lambda construct under ``scopes``, including nested ones."""

    found: list[lambda_.Function] = []
    seen: set[str] = set()
    for scope in scopes:
        for child in scope.node.find_all():
            if not isinstance(child, lambda_.Function):
                continue
            if child.node.addr in seen:
                continue
            seen.add(child.node.addr)
            found.append(child)
    return found


def is_flag_gated(fn: lambda_.IFunction) -> bool:
    """True when a context flag is the only reason this function exists.

    ``demo:seedDevices`` creates the device-seed function and its provider.
    ``whatsapp:wabaId`` creates the WABA destination function and its provider.
    ``dashboard:enabled`` creates the API stack, and ``desk:publish`` adds the
    desk upload handler inside that stack. Referencing any of these from
    another stack exports a value that CloudFormation cannot delete on the
    next deploy once the flag is off, because the producer stack updates first.
    """

    if "OerfApiStack" in Stack.of(fn).stack_name:
        return True
    path = fn.node.path
    return "/DeviceSeed" in path or "/WabaEventDestination/" in path


def error_alarm_id(fn: lambda_.IFunction, functions: Sequence[lambda_.IFunction]) -> str:
    """Construct id for one function's errors alarm.

    A unique ``node.id`` keeps ``{id}Errors``, the id already in the default
    template. Provider framework handlers are all named ``framework-onEvent``.
    Those take the construct path so two providers can synth together.
    """

    if sum(item.node.id == fn.node.id for item in functions) == 1:
        return f"{fn.node.id}Errors"
    sanitized = re.sub(r"[^A-Za-z0-9]", "", fn.node.path)
    if not sanitized:
        raise ValueError("function path produced an empty alarm id")
    return f"{sanitized}Errors"


def _alarm_beside_function(
    fn: lambda_.IFunction,
    peers: Sequence[lambda_.IFunction],
    stage: Stage,
) -> None:
    """Alarm a flag-gated function in its own stack.

    The metric uses the function name directly, so no export is required.
    The action targets the observability topic by its name.
    """

    stack = Stack.of(fn)
    alarm = cloudwatch.Alarm(
        stack,
        error_alarm_id(fn, peers),
        metric=fn.metric_errors(period=_PERIOD, statistic="Sum"),
        threshold=0,
        comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        evaluation_periods=1,
        treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
    )
    cfn_alarm = alarm.node.default_child
    if not isinstance(cfn_alarm, cloudwatch.CfnAlarm):
        raise TypeError("alarm default child is not CfnAlarm")
    cfn_alarm.alarm_actions = [stack.format_arn(service="sns", resource=stage.res("alarms"))]
    _retain_function_export(fn)


def _retain_function_export(fn: lambda_.IFunction) -> None:
    """Keep the function-name export observability used to import.

    TODO(export-removal): Delete this call after main and dev have each
    deployed the release that stops importing the export. The producer
    stacks update before observability, so the export has to survive that
    first deploy or CloudFormation cancels the delete. Remove it in the
    following release, not in the same one that drops the import.
    """

    Stack.of(fn).export_value(fn.function_name)


class OerfObservabilityStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        functions: Sequence[lambda_.IFunction],
        dead_letter_queues: Sequence[sqs.IQueue],
        state_machines: Sequence[sfn.IStateMachine],
        channel_router_log_group: logs.ILogGroup,
        stage: Stage,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if not functions or not dead_letter_queues or not state_machines:
            raise ValueError("observability stack needs functions, DLQs, and state machines")

        # Fixed name so an alarm in the function's own stack can target this
        # topic without importing it. Importing the topic from here would
        # cycle, because this stack already imports the producer stacks, and
        # those stacks deploy first. Setting the name replaces the topic once.
        self.alarm_topic = sns.Topic(
            self,
            "AlarmTopic",
            display_name=stage.res("alarms"),
            topic_name=stage.res("alarms"),
        )
        email = self.node.try_get_context("alarms:email")
        if isinstance(email, str) and email.strip():
            self.alarm_topic.add_subscription(subscriptions.EmailSubscription(email.strip()))

        action = cw_actions.SnsAction(self.alarm_topic)
        dlq_metrics = [
            queue.metric_approximate_number_of_messages_visible(
                period=_PERIOD, statistic="Maximum"
            )
            for queue in dead_letter_queues
        ]
        machine_metrics = [
            metric
            for machine in state_machines
            for metric in (
                machine.metric_failed(period=_PERIOD, statistic="Sum"),
                machine.metric_timed_out(period=_PERIOD, statistic="Sum"),
            )
        ]
        stable = [fn for fn in functions if not is_flag_gated(fn)]
        if not stable:
            raise ValueError("observability stack needs functions that exist for every flag")
        error_metrics = [
            fn.metric_errors(period=_PERIOD, statistic="Sum") for fn in stable
        ]
        self.alarms = [
            self._alarm(f"{queue.node.id}Visible", metric)
            for queue, metric in zip(dead_letter_queues, dlq_metrics, strict=True)
        ]
        self.alarms.extend(
            self._alarm(f"{machine.node.id}{metric.metric_name}", metric)
            for machine, metric in zip(
                (machine for machine in state_machines for _ in range(2)),
                machine_metrics,
                strict=True,
            )
        )
        # Ids stay based on the full list, so a provider alarm keeps today's
        # construct id when another provider is added.
        self.alarms.extend(
            self._alarm(error_alarm_id(fn, functions), metric)
            for fn, metric in zip(stable, error_metrics, strict=True)
        )
        for alarm in self.alarms:
            alarm.add_alarm_action(action)

        namespace = stage.channels_namespace()
        delivery_failures = self._delivery_failures(channel_router_log_group, namespace)
        delivery_alarm = self._alarm("ChannelDeliveryFailures", delivery_failures)
        delivery_alarm.add_alarm_action(action)
        self.alarms.append(delivery_alarm)
        ingest = next((fn for fn in functions if fn.node.id == "IngestFunction"), None)
        ingest_p95 = None
        if ingest is not None:
            ingest_p95 = ingest.metric_duration(statistic="p95", period=_PERIOD)
            ingest_alarm = self._alarm(
                "IngestReadingToIncidentP95",
                ingest_p95,
                threshold=3000,
            )
            ingest_alarm.add_alarm_action(action)
            self.alarms.append(ingest_alarm)
        by_channel = cloudwatch.MathExpression(
            expression=(
                f"SEARCH('{{{namespace},channel}} "
                "MetricName=\"DeliveryFailures\"', 'Sum', 300)"
            ),
            period=_PERIOD,
            label="DeliveryFailures by channel",
        )

        dashboard = cloudwatch.Dashboard(
            self, "OerfOperations", dashboard_name=stage.res("operations")
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(title="DLQ depth", left=dlq_metrics),
            cloudwatch.GraphWidget(title="State machine failures", left=machine_metrics),
            cloudwatch.GraphWidget(title="Lambda errors", left=error_metrics),
            cloudwatch.GraphWidget(title="Channel delivery failures", left=[delivery_failures]),
            cloudwatch.GraphWidget(
                title="Channel delivery failures by channel", left=[by_channel]
            ),
            *(
                [cloudwatch.GraphWidget(title="Ingest duration p95", left=[ingest_p95])]
                if ingest_p95 is not None
                else []
            ),
        )
        for fn in functions:
            if is_flag_gated(fn):
                _alarm_beside_function(fn, functions, stage)

    def _delivery_failures(
        self, log_group: logs.ILogGroup, namespace: str
    ) -> cloudwatch.IMetric:
        """Count router JSON lines whose outcome is a delivery failure.

        The alarm metric has no dimension, so a line with no channel still
        counts. A second filter keeps ``channel`` for the dashboard graph.
        """

        pattern = logs.FilterPattern.any(
            *(
                logs.FilterPattern.string_value("$.outcome", "=", outcome)
                for outcome in _DELIVERY_FAILURE_OUTCOMES
            )
        )
        logs.MetricFilter(
            self,
            "ChannelDeliveryFailureFilter",
            log_group=log_group,
            filter_pattern=pattern,
            metric_namespace=namespace,
            metric_name="DeliveryFailures",
            metric_value="1",
        )
        logs.MetricFilter(
            self,
            "ChannelDeliveryFailureByChannelFilter",
            log_group=log_group,
            filter_pattern=pattern,
            metric_namespace=namespace,
            metric_name="DeliveryFailures",
            metric_value="1",
            dimensions={"channel": "$.channel"},
        )
        return cloudwatch.Metric(
            namespace=namespace,
            metric_name="DeliveryFailures",
            statistic="Sum",
            period=_PERIOD,
        )

    def _alarm(
        self,
        construct_id: str,
        metric: cloudwatch.IMetric,
        *,
        threshold: int = 0,
    ) -> cloudwatch.Alarm:
        return cloudwatch.Alarm(
            self,
            construct_id,
            metric=metric,
            threshold=threshold,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
