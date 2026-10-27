"""WhatsApp Business Account event destination for the inbound topic.

The WABA resource ARN is
``arn:aws:social-messaging:{region}:{account}:waba/{id}``. ``{id}`` is the
context value with a leading ``waba-`` removed. The custom resource still
passes the prefixed id to the API.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import jsii
from aws_cdk import (
    BundlingOptions,
    CustomResource,
    Duration,
    ILocalBundling,
    RemovalPolicy,
    Stack,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_sns as sns,
    custom_resources as cr,
)
from constructs import Construct

from infra.constructs.social_messaging_arns import waba_arn

_ASSET = Path(__file__).resolve().parents[1] / "custom_resources"
_HANDLER = _ASSET / "waba_destination.py"
_REQUIREMENTS = _ASSET / "requirements.txt"
_ACTIONS = [
    "social-messaging:PutWhatsAppBusinessAccountEventDestinations",
    "social-messaging:GetLinkedWhatsAppBusinessAccount",
]


@jsii.implements(ILocalBundling)
class _BundleBoto3:
    """Install the pinned boto3 next to the handler. Docker is the fallback."""

    def try_bundle(self, output_dir: str, *, image: object, **_kwargs: object) -> bool:
        _ = image
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-compile",
                "-r",
                str(_REQUIREMENTS),
                "--target",
                output_dir,
            ],
            check=True,
        )
        shutil.copyfile(_HANDLER, Path(output_dir) / "waba_destination.py")
        return True


class WabaEventDestination(Construct):
    """Topic policy and custom resource for one ``waba-...`` id."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        topic: sns.ITopic,
        waba_id: str,
    ) -> None:
        super().__init__(scope, construct_id)
        if not waba_id.startswith("waba-") or "/" in waba_id:
            raise ValueError("whatsapp:wabaId must look like waba-...")
        stack = Stack.of(self)
        resource_arn = waba_arn(stack.region, stack.account, waba_id)
        _allow_publish(topic, resource_arn, stack.account)
        _grant_topic_key(topic, resource_arn, stack.account)
        function = _handler(self, resource_arn)
        provider_logs = logs.LogGroup(
            self,
            "ProviderLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        provider = cr.Provider(
            self,
            "Provider",
            on_event_handler=function,
            log_group=provider_logs,
        )
        CustomResource(
            self,
            "Resource",
            service_token=provider.service_token,
            properties={"WabaId": waba_id, "TopicArn": topic.topic_arn},
        )


def _allow_publish(topic: sns.ITopic, waba_arn: str, account: str) -> None:
    seen: set[str] = getattr(topic, "_oerf_waba_sources", set())
    if waba_arn in seen:
        return
    topic.add_to_resource_policy(
        iam.PolicyStatement(
            principals=[iam.ServicePrincipal("social-messaging.amazonaws.com")],
            actions=["sns:Publish"],
            resources=[topic.topic_arn],
            conditions={
                "StringEquals": {
                    "aws:SourceAccount": account,
                    "aws:SourceArn": waba_arn,
                }
            },
        )
    )
    seen.add(waba_arn)
    topic._oerf_waba_sources = seen  # type: ignore[attr-defined]


def _grant_topic_key(topic: sns.ITopic, waba_arn: str, account: str) -> None:
    """Grant the service the topic key when the topic uses a customer key.

    ``inbound_topic`` is created without ``master_key``. This branch does not
    run for that topic. An AWS owned key has no key policy to edit.
    """
    key = topic.master_key
    if key is None:
        return
    key.add_to_resource_policy(
        iam.PolicyStatement(
            principals=[iam.ServicePrincipal("social-messaging.amazonaws.com")],
            actions=["kms:GenerateDataKey*", "kms:Decrypt"],
            resources=["*"],
            conditions={
                "StringEquals": {
                    "aws:SourceAccount": account,
                    "aws:SourceArn": waba_arn,
                }
            },
        )
    )


def _handler(scope: Construct, waba_arn: str) -> lambda_.Function:
    log_group = logs.LogGroup(
        scope,
        "LogGroup",
        retention=logs.RetentionDays.ONE_MONTH,
        removal_policy=RemovalPolicy.DESTROY,
    )
    role = iam.Role(
        scope,
        "Role",
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
    )
    log_group.grant_write(role)
    role.add_to_policy(iam.PolicyStatement(actions=_ACTIONS, resources=[waba_arn]))
    return lambda_.Function(
        scope,
        "Function",
        function_name="oerf-waba-event-destination",
        runtime=lambda_.Runtime.PYTHON_3_11,
        handler="waba_destination.on_event",
        code=lambda_.Code.from_asset(
            str(_ASSET),
            exclude=["__pycache__", "*.pyc"],
            bundling=BundlingOptions(
                image=lambda_.Runtime.PYTHON_3_11.bundling_image,
                local=_BundleBoto3(),
                command=[
                    "bash",
                    "-c",
                    "pip install --no-compile -r requirements.txt -t /asset-output"
                    " && cp waba_destination.py /asset-output/",
                ],
            ),
        ),
        role=role,
        log_group=log_group,
        timeout=Duration.seconds(30),
        memory_size=128,
        # tracing ACTIVE adds xray:PutTraceSegments and xray:PutTelemetryRecords
        # on Resource *. X-Ray rejects a resource ARN. See iam-exceptions.md.
        tracing=lambda_.Tracing.ACTIVE,
    )
