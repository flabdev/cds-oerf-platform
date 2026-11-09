"""Service-reference probe ARNs for handler permission simulation."""

from __future__ import annotations

from infra.cfn_handler_permission_tests import (
    default_probe_values,
    probe_resource_for_handler,
)


def test_cloudwatch_dashboard_action_uses_dashboard_arn() -> None:
    values = default_probe_values(
        "000000000000",
        "us-east-1",
        guardrail_arn="arn:aws:bedrock:us-east-1:000000000000:guardrail/abc",
    )
    arn = probe_resource_for_handler(
        "cloudwatch:PutDashboard",
        values,
        cfn_type="AWS::CloudWatch::Dashboard",
    )
    assert arn == "arn:aws:cloudwatch::000000000000:dashboard/oerf-dev-dashboard"


def test_s3_get_object_uses_bucket_object_not_access_point() -> None:
    values = default_probe_values(
        "000000000000",
        "us-east-1",
        guardrail_arn="arn:aws:bedrock:us-east-1:000000000000:guardrail/abc",
    )
    arn = probe_resource_for_handler(
        "s3:GetObject",
        values,
        cfn_type="AWS::Lambda::Function",
    )
    assert arn == "arn:aws:s3:::oerf-dev-assets/cdk/oerf-dev-key"


def test_unscoped_action_uses_star() -> None:
    values = default_probe_values(
        "000000000000",
        "us-east-1",
        guardrail_arn="arn:aws:bedrock:us-east-1:000000000000:guardrail/abc",
    )
    assert (
        probe_resource_for_handler("kms:ListAliases", values, cfn_type="AWS::DynamoDB::Table")
        == "*"
    )
