"""Handler probe ARNs for EventBus policy and SNS subscription topics."""

from __future__ import annotations

from infra.cfn_handler_permission_tests import (
    _SNS_SUBSCRIPTION_TOPIC_ACTIONS,
    check_handler_permissions,
    probe_resource_for_handler,
    resolve_sns_subscription_topic_arn,
    synth_dev_application_templates,
)
from infra.dev_boundary import intersection_allows
from tests.unit.test_dev_boundary import ACCOUNT, DEV_GUARDRAIL, REGION

_MAIN_INBOUND = f"arn:aws:sns:{REGION}:{ACCOUNT}:OerfComputeStack-InboundTopicMAIN"


def test_event_bus_put_permission_is_not_applicable_without_policy() -> None:
    dev_templates = synth_dev_application_templates(ACCOUNT, REGION)
    result = check_handler_permissions(
        account=ACCOUNT,
        region=REGION,
        guardrail_arn=DEV_GUARDRAIL,
        dev_templates=dev_templates,
    )
    assert result.failures == []
    assert (
        "AWS::Events::EventBus",
        "events:PutPermission",
        "not applicable: Policy absent on every dev resource",
    ) in result.skipped
    assert (
        "AWS::Events::EventBus",
        "events:RemovePermission",
        "not applicable: Policy absent on every dev resource",
    ) in result.skipped


def test_sns_subscription_probes_use_template_topic() -> None:
    default_templates = synth_dev_application_templates(ACCOUNT, REGION)
    shared_templates = synth_dev_application_templates(
        ACCOUNT,
        REGION,
        extra_context={"whatsapp:sharedInboundTopicArn": _MAIN_INBOUND},
    )
    dev_topic = resolve_sns_subscription_topic_arn(default_templates, ACCOUNT, REGION)
    main_topic = resolve_sns_subscription_topic_arn(shared_templates, ACCOUNT, REGION)
    assert "-dev-" in dev_topic
    assert main_topic == _MAIN_INBOUND
    values = {
        "account": ACCOUNT,
        "region": REGION,
        "sns_subscription_topic_arn": dev_topic,
        "boundary": f"arn:aws:iam::{ACCOUNT}:policy/OerfDevWorkloadBoundary",
    }
    for action in _SNS_SUBSCRIPTION_TOPIC_ACTIONS:
        resource = probe_resource_for_handler(
            action, values, cfn_type="AWS::SNS::Subscription"
        )
        assert resource == dev_topic
        assert intersection_allows(
            action, resource, ACCOUNT, REGION, guardrail_arn=DEV_GUARDRAIL
        )


def test_sns_subscription_on_main_topic_allowed_via_mt() -> None:
    values = {
        "account": ACCOUNT,
        "region": REGION,
        "sns_subscription_topic_arn": _MAIN_INBOUND,
        "boundary": f"arn:aws:iam::{ACCOUNT}:policy/OerfDevWorkloadBoundary",
    }
    for action in _SNS_SUBSCRIPTION_TOPIC_ACTIONS:
        resource = probe_resource_for_handler(
            action, values, cfn_type="AWS::SNS::Subscription"
        )
        assert resource == _MAIN_INBOUND
        assert intersection_allows(
            action, resource, ACCOUNT, REGION, guardrail_arn=DEV_GUARDRAIL
        )
