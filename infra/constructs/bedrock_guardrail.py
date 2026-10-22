"""Bedrock guardrail for vision and, later, the agent.

Values come from ``infra/config/guardrail.json``. Content filters use the
Standard tier at high strength. That tier requires the US guardrail profile.
Image modalities are not set: Converse does not document evaluation of a Nova
Lite image content block.
"""

from __future__ import annotations

import json
from pathlib import Path

from aws_cdk import Stack, aws_bedrock as bedrock
from constructs import Construct

_CONFIG = Path(__file__).resolve().parents[1] / "config" / "guardrail.json"
# Source us-east-1, profile us.guardrail.v1:0. Destination regions from
# https://docs.aws.amazon.com/bedrock/latest/userguide/guardrail-profiles-permissions.html
_PROFILE_DESTINATIONS = ("us-east-1", "us-east-2", "us-west-2")


class BedrockGuardrail(Construct):
    """One guardrail and its published version."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config_path: Path | None = None,
    ) -> None:
        super().__init__(scope, construct_id)
        config = json.loads((config_path or _CONFIG).read_text(encoding="utf-8"))
        stack = Stack.of(self)
        profile_id = config["guardrail_profile_identifier"]
        self.profile_arns = [
            f"arn:aws:bedrock:{region}:{stack.account}:guardrail-profile/{profile_id}"
            for region in _PROFILE_DESTINATIONS
        ]
        profile_arn = (
            f"arn:aws:bedrock:{stack.region}:{stack.account}:guardrail-profile/{profile_id}"
        )
        self.guardrail = bedrock.CfnGuardrail(
            self,
            "Resource",
            name=config["name"],
            description=config["description"],
            blocked_input_messaging=config["blocked_input_message"],
            blocked_outputs_messaging=config["blocked_output_message"],
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[_content_filter(item) for item in config["content_filters"]],
                content_filters_tier_config=bedrock.CfnGuardrail.ContentFiltersTierConfigProperty(
                    tier_name=config["content_filters_tier"],
                ),
            ),
            topic_policy_config=bedrock.CfnGuardrail.TopicPolicyConfigProperty(
                topics_config=[_topic(item) for item in config["denied_topics"]],
                topics_tier_config=bedrock.CfnGuardrail.TopicsTierConfigProperty(
                    tier_name=config["topics_tier"],
                ),
            ),
            sensitive_information_policy_config=(
                bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                    pii_entities_config=[_pii(item) for item in config["pii_entities"]],
                )
            ),
            cross_region_config=bedrock.CfnGuardrail.GuardrailCrossRegionConfigProperty(
                guardrail_profile_arn=profile_arn,
            ),
        )
        self.version = bedrock.CfnGuardrailVersion(
            self,
            "Version",
            guardrail_identifier=self.guardrail.attr_guardrail_id,
            description="Published OERF guardrail configuration.",
        )
        self.guardrail_id = self.guardrail.attr_guardrail_id
        self.guardrail_version = self.version.attr_version
        self.guardrail_arn = self.guardrail.attr_guardrail_arn
        self.apply_resources = [self.guardrail_arn, *self.profile_arns]


def _content_filter(item: dict) -> bedrock.CfnGuardrail.ContentFilterConfigProperty:
    props = {
        "type": item["type"],
        "input_strength": item["input_strength"],
        "output_strength": item["output_strength"],
        "input_action": item["input_action"],
        "output_action": item["output_action"],
        "input_modalities": item["input_modalities"],
    }
    if "output_modalities" in item:
        props["output_modalities"] = item["output_modalities"]
    return bedrock.CfnGuardrail.ContentFilterConfigProperty(**props)


def _topic(item: dict) -> bedrock.CfnGuardrail.TopicConfigProperty:
    return bedrock.CfnGuardrail.TopicConfigProperty(
        name=item["name"],
        definition=item["definition"],
        type=item["type"],
        examples=item["examples"],
        input_action=item["input_action"],
        output_action=item["output_action"],
    )


def _pii(item: dict) -> bedrock.CfnGuardrail.PiiEntityConfigProperty:
    return bedrock.CfnGuardrail.PiiEntityConfigProperty(
        type=item["type"],
        action=item["action"],
        input_action=item["input_action"],
        output_action=item["output_action"],
    )
