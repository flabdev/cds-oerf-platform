"""Compressed ``OerfDevWorkloadBoundary`` must match the pre-compression baseline."""

from __future__ import annotations

import json
from pathlib import Path

from infra.dev_boundary import (
    _GUARDRAIL_EXEMPT_ACTIONS,
    _MAIN_TOPIC_ALLOWED,
    _REQUEST_TAG,
    _RESOURCE_TAG,
    STAGE_TAG_VALUE,
    _glob,
    _outside_dev_not_actions,
    boundary_permits_from_statements,
    boundary_policy,
    evaluation_guardrail_arn,
    service_reference_iam_actions,
)
from infra.service_reference_probes import probe_arn_from_service_reference

_REPO = Path(__file__).resolve().parents[1]
_FIXTURE = _REPO / "fixtures" / "oerf_dev_workload_boundary_baseline.json"
_ACCOUNT = "000000000000"
_REGION = "us-east-1"
_DEV_GUARDRAIL = f"arn:aws:bedrock:{_REGION}:{_ACCOUNT}:guardrail/b7nun5huxbii"
_MAIN_TOPIC = (
    f"arn:aws:sns:{_REGION}:{_ACCOUNT}:OerfComputeStack-InboundTopicMAIN000"
)
_BASELINE_AGENTCORE_RUNTIME_LOG = (
    f"arn:aws:logs:{_REGION}:{_ACCOUNT}:log-group:"
    "/aws/bedrock-agentcore/runtimes/oerf_agent"
)
_MAIN_AGENTCORE_RUNTIME_LOG_STREAM = f"{_BASELINE_AGENTCORE_RUNTIME_LOG}:*"

_PROBE_VALUES: dict[str, str] = {
    "account": _ACCOUNT,
    "region": _REGION,
    "dev_guardrail_arn": _DEV_GUARDRAIL,
    "dev_rest_api_id": "devapi",
    "dev_http_api_id": "devhttp",
    "dev_distribution_arn": f"arn:aws:cloudfront::{_ACCOUNT}:distribution/EDEV",
    "dev_runtime_arn": (
        f"arn:aws:bedrock-agentcore:{_REGION}:{_ACCOUNT}:runtime/oerf_dev_agent"
    ),
}

# Documented intentional tightening (see test output and security report).
EXPECTED_STRICTER_DESCRIPTION = (
    "Removed NotResource "
    "arn:aws:logs:…:log-group:/aws/bedrock-agentcore/runtimes/*; "
    "main runtime log ARNs without ``_dev_`` are denied by NOD (dev paths still "
    "match ``logs:*_dev_*``)."
)


def _baseline_statements() -> list[dict]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))["Statement"]


def _compressed_statements() -> list[dict]:
    return boundary_policy(
        _ACCOUNT, _REGION, evaluation_guardrail_arn(_ACCOUNT, _REGION)
    )["Statement"]


def _probe_resources(action: str) -> list[str]:
    dev = probe_arn_from_service_reference(action, _PROBE_VALUES)
    main_values = {
        **_PROBE_VALUES,
        "TopicName": "OerfComputeStack-InboundTopicMAIN000",
    }
    main = probe_arn_from_service_reference(action, main_values)
    resources: list[str] = []
    for candidate in (dev, main, "*"):
        if candidate and candidate not in resources:
            resources.append(candidate)
    if action.startswith("sns:") and _MAIN_TOPIC not in resources:
        resources.append(_MAIN_TOPIC)
    if action.startswith("logs:"):
        for extra in (
            _BASELINE_AGENTCORE_RUNTIME_LOG,
            _MAIN_AGENTCORE_RUNTIME_LOG_STREAM,
        ):
            if extra not in resources:
                resources.append(extra)
    return resources


def _tag_contexts() -> list[dict[str, str]]:
    return [
        {},
        {_RESOURCE_TAG: STAGE_TAG_VALUE},
        {_REQUEST_TAG: STAGE_TAG_VALUE},
        {_RESOURCE_TAG: STAGE_TAG_VALUE, _REQUEST_TAG: STAGE_TAG_VALUE},
    ]


def _is_agentcore_runtime_log_tightening(
    action: str,
    resource: str,
    *,
    old_ok: bool,
    new_ok: bool,
) -> bool:
    if not action.startswith("logs:"):
        return False
    if not old_ok or new_ok:
        return False
    if not resource.startswith(
        f"arn:aws:logs:{_REGION}:{_ACCOUNT}:log-group:/aws/bedrock-agentcore/runtimes/"
    ):
        return False
    if "_dev_" in resource:
        return False
    return True


def _baseline_nod_not_action() -> list[str]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))["Statement"][1]["NotAction"]


def _baseline_notaction_verbatim() -> set[str]:
    """Every ``NotAction`` entry in the pre-compression boundary fixture."""

    verbatim: set[str] = set()
    for statement in _baseline_statements():
        if statement.get("Effect") != "Deny":
            continue
        for entry in statement.get("NotAction", []):
            verbatim.add(entry)
    return verbatim


def _pattern_expansion(pattern: str, actions: list[str]) -> list[str]:
    return sorted(action for action in actions if _glob(pattern, action))


def test_notaction_exemption_patterns_do_not_widen_baseline() -> None:
    """NotAction exemptions: no new wildcards; fixture expansion ⊆ baseline."""

    actions = service_reference_iam_actions()
    verbatim = _baseline_notaction_verbatim()
    all_patterns = [*_outside_dev_not_actions(), *_MAIN_TOPIC_ALLOWED]
    for pattern in all_patterns:
        if pattern in verbatim:
            continue
        if "*" not in pattern:
            continue
        raise AssertionError(
            f"NotAction wildcard {pattern!r} is not verbatim in baseline "
            f"(trimmed service-reference expansion is not sufficient)"
        )

    baseline_exempt = {
        action
        for action in actions
        for pattern in _baseline_nod_not_action()
        if _glob(pattern, action)
    }
    baseline_nmt = json.loads(_FIXTURE.read_text(encoding="utf-8"))["Statement"][2][
        "NotAction"
    ]
    nmt_baseline = {
        action
        for action in actions
        for pattern in baseline_nmt
        if _glob(pattern, action)
    }
    for pattern in _outside_dev_not_actions():
        matched = {action for action in actions if _glob(pattern, action)}
        if not matched:
            continue
        extra = matched - baseline_exempt
        assert not extra, f"NOD NotAction {pattern!r} widens by {sorted(extra)}"
    for pattern in _MAIN_TOPIC_ALLOWED:
        matched = {action for action in actions if _glob(pattern, action)}
        extra = matched - nmt_baseline
        assert not extra, f"NMT NotAction {pattern!r} widens by {sorted(extra)}"


def test_bedrock_nod_exemption_expansion_from_service_reference() -> None:
    """Document expansions; ``bedrock:*Guardrail*`` must not replace explicit NOD exempts."""

    actions = [a for a in service_reference_iam_actions() if a.startswith("bedrock:")]
    star = _pattern_expansion("bedrock:*Guardrail*", actions)
    ag = _pattern_expansion("bedrock:*agResource", actions)
    assert star == [
        "bedrock:CreateGuardrail",
        "bedrock:CreateGuardrailVersion",
        "bedrock:DeleteGuardrail",
        "bedrock:GetGuardrail",
        "bedrock:ListGuardrails",
        "bedrock:UpdateGuardrail",
    ]
    assert ag == ["bedrock:TagResource", "bedrock:UntagResource"]
    assert "bedrock:ApplyGuardrail" in _GUARDRAIL_EXEMPT_ACTIONS
    assert any(
        _glob(pattern, "bedrock:ListTagsForResource")
        for pattern in _outside_dev_not_actions()
    )


def test_boundary_equivalence_grid_against_baseline() -> None:
    baseline = _baseline_statements()
    compressed = _compressed_statements()
    actions = service_reference_iam_actions()
    cases = 0
    expected_stricter: list[tuple[str, str, dict[str, str]]] = []
    mismatches: list[str] = []
    widenings: list[str] = []

    for action in actions:
        for resource in _probe_resources(action):
            for context in _tag_contexts():
                cases += 1
                old_ok = boundary_permits_from_statements(
                    baseline, action, resource, context
                )
                new_ok = boundary_permits_from_statements(
                    compressed, action, resource, context
                )
                if not old_ok and new_ok:
                    widenings.append(
                        f"widen {action} {resource} context={context!r}"
                    )
                    continue
                if old_ok and not new_ok:
                    if _is_agentcore_runtime_log_tightening(
                        action, resource, old_ok=old_ok, new_ok=new_ok
                    ):
                        expected_stricter.append((action, resource, context))
                        continue
                    mismatches.append(
                        f"unexpected deny {action} {resource} context={context!r}"
                    )

    assert not widenings, "boundary must not allow more than baseline:\n" + "\n".join(
        widenings[:30]
    )
    assert not mismatches, (
        f"equivalence failed after {cases} cases "
        f"({len(actions)} actions):\n" + "\n".join(mismatches[:30])
    )
    print(
        f"boundary equivalence: {cases} cases "
        f"({len(actions)} service-reference actions × probes × 4 tag contexts); "
        f"{len(expected_stricter)} expected-stricter "
        f"({EXPECTED_STRICTER_DESCRIPTION})"
    )
    assert cases >= len(actions) * 4, cases
    assert len(expected_stricter) > 0, "expected AgentCore runtime log tightening cases"
