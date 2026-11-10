"""One stage for every operator script.

The stage comes from ``--stage`` or the ``STAGE`` environment variable.
There is no default. ``main`` is refused unless ``ALLOW_MAIN=1``.

Physical names come from :class:`infra.naming.Stage`. Generated names
(tables, buckets, the user pool, app clients, API URLs) are read from that
stage's CloudFormation outputs. ``DEMO_NUMBERS_PREFIX`` is an explicit
override of the stage demo-number prefix and is logged when it is set.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from infra.naming import Stage
from oerf.platform.channels.address import demo_number_name, driver_demo_number_name

REGION = "us-east-1"
DATA_STACK = "OerfDataStack"
COMPUTE_STACK = "OerfComputeStack"
API_STACK = "OerfApiStack"
OBSERVABILITY_STACK = "OerfObservabilityStack"
STACK_BASES = (DATA_STACK, COMPUTE_STACK, API_STACK, OBSERVABILITY_STACK)
_LOGGED_OVERRIDES: set[str] = set()


def resolve_stage(
    explicit: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> OperatorStage:
    """The stage this process may touch. Missing and unknown values refuse."""

    env = os.environ if environ is None else environ
    raw = explicit.strip() if explicit is not None else ""
    if not raw:
        raw = env.get("STAGE", "").strip()
    if not raw:
        raise SystemExit("stage is required: pass --stage or set STAGE to main or dev")
    if raw == "main" and env.get("ALLOW_MAIN") != "1":
        raise SystemExit(
            "refusing stage main. Set ALLOW_MAIN=1 to operate on the main stage."
        )
    try:
        stage = Stage(name=raw)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    return OperatorStage(stage, env)


def add_stage_argument(parser: argparse.ArgumentParser) -> None:
    """``--stage`` is optional on the parser because ``STAGE`` can supply it."""

    parser.add_argument(
        "--stage",
        default=None,
        help="Deployment stage, main or dev. Defaults to STAGE. Required.",
    )


def remember_stage(target: OperatorStage) -> OperatorStage:
    """Keep later lookups in this process on the stage the command already accepted."""

    os.environ["STAGE"] = target.name
    if target.name == "main":
        os.environ["ALLOW_MAIN"] = "1"
    return target


@dataclass(frozen=True)
class OperatorStage:
    """Names for one stage. Nothing here calls AWS."""

    stage: Stage
    environ: Mapping[str, str]

    @property
    def name(self) -> str:
        return self.stage.name

    def stack(self, base: str) -> str:
        return self.stage.stack(base)

    @property
    def data_stack(self) -> str:
        return self.stage.stack(DATA_STACK)

    @property
    def compute_stack(self) -> str:
        return self.stage.stack(COMPUTE_STACK)

    @property
    def api_stack(self) -> str:
        return self.stage.stack(API_STACK)

    @property
    def observability_stack(self) -> str:
        return self.stage.stack(OBSERVABILITY_STACK)

    @property
    def stacks(self) -> tuple[str, ...]:
        return tuple(self.stage.stack(base) for base in STACK_BASES)

    @property
    def event_bus(self) -> str:
        return self.stage.res("events")

    @property
    def cold_chain(self) -> str:
        return self.stage.res("cold-chain")

    @property
    def engage_party(self) -> str:
        return self.stage.res("engage-party")

    @property
    def router_function(self) -> str:
        return self.stage.res("channel-router")

    @property
    def inbound_function(self) -> str:
        return self.stage.res("inbound")

    @property
    def waba_parameter(self) -> str:
        return self.stage.ssm("whatsapp/waba-id")

    @property
    def phone_number_id_parameter(self) -> str:
        return self.stage.ssm("whatsapp/phone-number-id")

    def demo_numbers_prefix(self) -> str:
        """The one prefix for every role. An env override is logged, not silent."""

        stage_prefix = self.stage.ssm("demo-numbers")
        override = self.environ.get("DEMO_NUMBERS_PREFIX", "").strip().rstrip("/")
        if not override:
            return stage_prefix
        _log_override(override)
        return override

    def demo_number_name(self, tenant_id: str, role: str) -> str:
        return demo_number_name(tenant_id, role, prefix=self.demo_numbers_prefix())

    def driver_demo_number_name(self, tenant_id: str, contact_id: str) -> str:
        return driver_demo_number_name(
            tenant_id, contact_id, prefix=self.demo_numbers_prefix()
        )

    def summary(self, *, tables: Mapping[str, str] | None = None) -> str:
        """One line. Stack names, the bus, and table names. No phone numbers."""

        parts = [
            f"stage={self.name}",
            f"stacks={','.join(self.stacks)}",
            f"bus={self.event_bus}",
        ]
        if tables:
            shown = ",".join(f"{key}={value}" for key, value in tables.items() if value)
            if shown:
                parts.append(f"tables={shown}")
        return " ".join(parts)

    def announce(self, *, tables: Mapping[str, str] | None = None) -> None:
        print(self.summary(tables=tables))


def stack_outputs(cloudformation: Any, stack_name: str) -> dict[str, str]:
    """Outputs of one stack. The caller names the stack; this does not guess."""

    response = cloudformation.describe_stacks(StackName=stack_name)
    stacks = response.get("Stacks") if isinstance(response, dict) else None
    if not isinstance(stacks, list) or not stacks:
        raise LookupError(stack_name)
    return {
        str(item.get("OutputKey")): str(item.get("OutputValue") or "").strip()
        for item in stacks[0].get("Outputs") or []
        if isinstance(item, dict) and item.get("OutputKey")
    }


def require_stack(requested: str | None, expected: str) -> str:
    """Use the stage stack. An explicit other name is a refusal, not a fallback."""

    if requested and requested != expected:
        raise SystemExit(f"stack {requested} is not {expected} for this stage")
    return expected


def _log_override(prefix: str) -> None:
    if prefix in _LOGGED_OVERRIDES:
        return
    _LOGGED_OVERRIDES.add(prefix)
    print(f"DEMO_NUMBERS_PREFIX override {prefix}", file=sys.stderr)
