"""CLI checks for scripts.check_dev_boundary."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

_REPO = Path(__file__).resolve().parents[2]
os.environ.setdefault(
    "JSII_RUNTIME_PACKAGE_CACHE_ROOT",
    str(_REPO / ".jsii-cache"),
)

from infra.dev_boundary import intersection_allows  # noqa: E402
from scripts.check_dev_boundary import main  # noqa: E402

_ACCOUNT = "000000000000"
_REGION = "us-east-1"
_DEV_GUARDRAIL = f"arn:aws:bedrock:{_REGION}:{_ACCOUNT}:guardrail/b7nun5huxbii"


def _context_from_entries(entries: list[dict] | None) -> dict[str, str]:
    if not entries:
        return {}
    out: dict[str, str] = {}
    for entry in entries:
        out[entry["ContextKeyName"]] = entry["ContextKeyValues"][0]
    return out


def test_handler_permissions_mode_runs_with_only_dev_guardrail(monkeypatch) -> None:
    """``--handler-permissions`` must not require ``--main-guardrail-arn`` or CI synth."""

    class FakeIam:
        def simulate_principal_policy(
            self,
            *,
            ActionNames: list[str],
            ResourceArns: list[str],
            ContextEntries: list[dict] | None = None,
            **_: object,
        ) -> dict:
            action = ActionNames[0]
            resource = ResourceArns[0]
            ctx = _context_from_entries(ContextEntries)
            allowed = intersection_allows(
                action,
                resource,
                _ACCOUNT,
                _REGION,
                ctx,
                guardrail_arn=_DEV_GUARDRAIL,
            )
            decision = "allowed" if allowed else "implicitDeny"
            return {"EvaluationResults": [{"EvalDecision": decision}]}

        def simulate_custom_policy(
            self,
            *,
            ActionNames: list[str],
            ResourceArns: list[str],
            ContextEntries: list[dict] | None = None,
            **_: object,
        ) -> dict:
            return self.simulate_principal_policy(
                ActionNames=ActionNames,
                ResourceArns=ResourceArns,
                ContextEntries=ContextEntries,
            )

    session = MagicMock()
    session.client.return_value = FakeIam()
    monkeypatch.setattr("scripts.check_dev_boundary.boto3.Session", lambda **_: session)

    code = main(
        [
            "--profile",
            "unit-test",
            "--region",
            _REGION,
            "--account",
            _ACCOUNT,
            "--dev-guardrail-arn",
            _DEV_GUARDRAIL,
            "--handler-permissions",
        ]
    )
    assert code == 0


def test_handler_permissions_repo_only_skips_live_iam(monkeypatch) -> None:
    """``--handler-repo-only`` must not call ``simulate_principal_policy``."""

    class FakeIam:
        def simulate_principal_policy(self, **_: object) -> dict:
            raise AssertionError("live-exec should be skipped")

        def simulate_custom_policy(self, **_: object) -> dict:
            raise AssertionError("repo-exec is local; no SimulateCustomPolicy")

    session = MagicMock()
    session.client.return_value = FakeIam()
    monkeypatch.setattr("scripts.check_dev_boundary.boto3.Session", lambda **_: session)

    code = main(
        [
            "--profile",
            "unit-test",
            "--region",
            _REGION,
            "--account",
            _ACCOUNT,
            "--dev-guardrail-arn",
            _DEV_GUARDRAIL,
            "--handler-permissions",
            "--handler-repo-only",
        ]
    )
    assert code == 0
