"""Stage names for the one CDK app.

``main`` keeps every physical name the templates use today. ``dev`` inserts
``dev`` so both stages can exist in one account and region. Callers pass a
``Stage``; nothing here reads the process environment.
"""

from __future__ import annotations

from dataclasses import dataclass

from aws_cdk import Stack, aws_apigatewayv2 as apigwv2, aws_iam as iam
from constructs import Construct

_ALLOWED = ("main", "dev")
_STAGE_ERROR = (
    "CDK context 'stage' is required and must be 'main' or 'dev' "
    "(got {value!r}). Pass -c stage=main or -c stage=dev."
)


@dataclass(frozen=True)
class Stage:
    """One deployment stage. ``name`` is ``main`` or ``dev``."""

    name: str

    def __post_init__(self) -> None:
        if self.name not in _ALLOWED:
            raise ValueError(_STAGE_ERROR.format(value=self.name))

    @classmethod
    def from_context(cls, value: object) -> Stage:
        """Build a stage from the CDK ``stage`` context value. No default."""

        if value not in _ALLOWED:
            raise ValueError(_STAGE_ERROR.format(value=value))
        return cls(name=value)

    @property
    def is_main(self) -> bool:
        return self.name == "main"

    def dev_bootstrap_qualifier(self) -> str:
        """Bootstrap qualifier for dev. Main keeps the CLI default and must not call this."""

        if self.is_main:
            raise ValueError("main uses the default CDK bootstrap qualifier hnb659fds")
        return "oerfdev"

    def stack(self, base: str) -> str:
        """CloudFormation stack name. Main keeps ``base``."""

        if self.is_main:
            return base
        return f"{base}-dev"

    def res(self, name: str) -> str:
        """Physical name that is ``oerf-{name}`` on main."""

        if self.is_main:
            return f"oerf-{name}"
        return f"oerf-dev-{name}"

    def ssm(self, path: str) -> str:
        """SSM parameter path. ``path`` is relative, for example ``demo-numbers``."""

        relative = path.lstrip("/")
        if self.is_main:
            return f"/oerf/{relative}"
        return f"/oerf/dev/{relative}"

    def agent_runtime_name(self) -> str:
        """AgentCore runtime name. The API allows underscores, not hyphens."""

        if self.is_main:
            return "oerf_agent"
        return "oerf_dev_agent"

    def agent_log_group(self) -> str:
        return f"/aws/vendedlogs/bedrock-agentcore/{self.agent_runtime_name()}"

    def export_name(self, name: str) -> str:
        """CloudFormation export. Main keeps ``name``; exports are account-wide."""

        if self.is_main:
            return name
        return f"{name}-dev"

    def channels_namespace(self) -> str:
        """CloudWatch namespace for channel delivery metrics."""

        if self.is_main:
            return "OERF/Channels"
        return "OERF/Dev/Channels"


def qualify_dev_names(scope: Construct) -> None:
    """Give dev its own copy of names CDK derives from the construct path.

    Logical ids ignore the stack name, so a second stack would reuse the same
    IAM policy name. Policy names are unique in the account. The HTTP
    authorizer name is the construct id and is not unique across APIs in the
    overlap check. Main never calls this.
    """

    for child in scope.node.find_all():
        if isinstance(child, iam.CfnPolicy):
            logical = Stack.of(child).get_logical_id(child)
            staged = logical if logical.endswith("-dev") else f"{logical}-dev"
            if child.policy_name != staged:
                child.policy_name = staged
        if isinstance(child, apigwv2.CfnAuthorizer) and child.name == "OerfHttpAuthorizer":
            child.name = "OerfHttpAuthorizer-dev"
