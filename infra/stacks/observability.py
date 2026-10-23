"""Alarms for dead-letter queues, state machines, and Lambda errors.

The topic has no email subscription unless context ``alarms:email`` is set.
Missing metrics do not alarm: a quiet queue is healthy.
"""

from __future__ import annotations

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
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if not functions or not dead_letter_queues or not state_machines:
            raise ValueError("observability stack needs functions, DLQs, and state machines")

        self.alarm_topic = sns.Topic(self, "AlarmTopic", display_name="oerf-alarms")
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
        error_metrics = [
            fn.metric_errors(period=_PERIOD, statistic="Sum") for fn in functions
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
        self.alarms.extend(
            self._alarm(f"{fn.node.id}Errors", metric)
            for fn, metric in zip(functions, error_metrics, strict=True)
        )
        for alarm in self.alarms:
            alarm.add_alarm_action(action)

        delivery_failures = self._delivery_failures(channel_router_log_group)
        delivery_alarm = self._alarm("ChannelDeliveryFailures", delivery_failures)
        delivery_alarm.add_alarm_action(action)
        self.alarms.append(delivery_alarm)
        by_channel = cloudwatch.MathExpression(
            expression=(
                "SEARCH('{OERF/Channels,channel} "
                'MetricName="DeliveryFailures"\', \'Sum\', 300)'
            ),
            period=_PERIOD,
            label="DeliveryFailures by channel",
        )

        dashboard = cloudwatch.Dashboard(self, "OerfOperations", dashboard_name="oerf-operations")
        dashboard.add_widgets(
            cloudwatch.GraphWidget(title="DLQ depth", left=dlq_metrics),
            cloudwatch.GraphWidget(title="State machine failures", left=machine_metrics),
            cloudwatch.GraphWidget(title="Lambda errors", left=error_metrics),
            cloudwatch.GraphWidget(title="Channel delivery failures", left=[delivery_failures]),
            cloudwatch.GraphWidget(
                title="Channel delivery failures by channel", left=[by_channel]
            ),
        )

    def _delivery_failures(self, log_group: logs.ILogGroup) -> cloudwatch.IMetric:
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
            metric_namespace="OERF/Channels",
            metric_name="DeliveryFailures",
            metric_value="1",
        )
        logs.MetricFilter(
            self,
            "ChannelDeliveryFailureByChannelFilter",
            log_group=log_group,
            filter_pattern=pattern,
            metric_namespace="OERF/Channels",
            metric_name="DeliveryFailures",
            metric_value="1",
            dimensions={"channel": "$.channel"},
        )
        return cloudwatch.Metric(
            namespace="OERF/Channels",
            metric_name="DeliveryFailures",
            statistic="Sum",
            period=_PERIOD,
        )

    def _alarm(self, construct_id: str, metric: cloudwatch.IMetric) -> cloudwatch.Alarm:
        return cloudwatch.Alarm(
            self,
            construct_id,
            metric=metric,
            threshold=0,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
