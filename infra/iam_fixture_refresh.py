"""Build ``infra/bootstrap/iam-fixtures/*.json`` from synth + AWS (or legacy dirs).

Used by ``scripts/refresh_iam_fixtures.sh``. Consolidating from the old per-file
layout does not call AWS.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE_DIR = _REPO_ROOT / "infra" / "bootstrap" / "iam-fixtures"
_CFN_OUT = _FIXTURE_DIR / "cfn-handler-permissions.json"
_SR_OUT = _FIXTURE_DIR / "service-reference.json"
_LEGACY_CFN = _REPO_ROOT / "infra" / "bootstrap" / "cfn-handler-permissions"
_LEGACY_SR = _REPO_ROOT / "infra" / "bootstrap" / "service-reference"

_HANDLERS = ("create", "read", "update", "delete", "list")
_SR_BASE = "https://servicereference.us-east-1.amazonaws.com/v1"

# IAM action prefix -> service-reference file stem.
_SERVICE_FILES: dict[str, str] = {
    "apigateway": "apigateway",
    "bedrock": "bedrock",
    "bedrock-agentcore": "bedrock-agentcore",
    "cloudfront": "cloudfront",
    "cloudwatch": "cloudwatch",
    "cognito-idp": "cognito-idp",
    "dynamodb": "dynamodb",
    "ec2": "ec2",
    "ecr": "ecr",
    "events": "events",
    "iam": "iam",
    "kms": "kms",
    "lambda": "lambda",
    "logs": "logs",
    "s3": "s3",
    "sns": "sns",
    "sqs": "sqs",
    "ssm": "ssm",
    "states": "states",
    "xray": "xray",
}


def _iam_action_name(action: str) -> tuple[str, str]:
    if ":" not in action:
        return action, action
    prefix, name = action.split(":", 1)
    if prefix == "s3" and name[:1].islower():
        name = name[0].upper() + name[1:]
    return prefix, name


def _type_filename(cfn_type: str) -> str:
    return cfn_type.replace("::", "__") + ".json"


def _dedupe_sorted(ops: list[str]) -> list[str]:
    return sorted({item for item in ops if isinstance(item, str)})


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")


def trim_service_reference(doc: dict[str, Any], action_names: set[str]) -> dict[str, Any]:
    """Keep only actions the handler check uses and their resource ARN formats."""

    actions_in = doc.get("Actions") or []
    trimmed_actions: list[dict[str, Any]] = []
    resource_names: set[str] = set()
    for entry in actions_in:
        name = entry.get("Name")
        if name not in action_names:
            continue
        item: dict[str, Any] = {"Name": name}
        rtypes = entry.get("Resources")
        if rtypes:
            rt_trim: list[dict[str, Any]] = []
            for rt in rtypes:
                if not isinstance(rt, dict) or not rt.get("Name"):
                    continue
                row: dict[str, Any] = {"Name": rt["Name"]}
                if "Required" in rt:
                    row["Required"] = rt["Required"]
                rt_trim.append(row)
                resource_names.add(rt["Name"])
            item["Resources"] = rt_trim
        keys = entry.get("ActionConditionKeys")
        if keys:
            item["ActionConditionKeys"] = sorted(keys)
        trimmed_actions.append(item)
    trimmed_actions.sort(key=lambda row: row["Name"])

    resources_in = doc.get("Resources") or []
    trimmed_resources: list[dict[str, Any]] = []
    for res in resources_in:
        name = res.get("Name")
        if name not in resource_names:
            continue
        fmts = res.get("ARNFormats")
        if not fmts:
            continue
        trimmed_resources.append({"Name": name, "ARNFormats": sorted(fmts)})
    trimmed_resources.sort(key=lambda row: row["Name"])
    return {"Actions": trimmed_actions, "Resources": trimmed_resources}


def actions_needed_for_handlers(cfn_doc: dict[str, Any]) -> dict[str, set[str]]:
    """Map service-reference stem -> IAM action names used in handler JSON."""

    needed: dict[str, set[str]] = {}
    for block in cfn_doc.values():
        if not isinstance(block, dict):
            continue
        for op in _HANDLERS:
            for action in block.get(op, []):
                if not isinstance(action, str):
                    continue
                prefix, name = _iam_action_name(action)
                stem = _SERVICE_FILES.get(prefix)
                if stem:
                    needed.setdefault(stem, set()).add(name)
    return needed


def consolidate_cfn_from_legacy(
    types: frozenset[str],
    *,
    legacy_dir: Path = _LEGACY_CFN,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for cfn_type in sorted(types):
        if cfn_type == "AWS::CDK::Metadata":
            continue
        path = legacy_dir / _type_filename(cfn_type)
        if not path.is_file() or path.stat().st_size == 0:
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        handlers = raw.get("handlers") or raw
        block: dict[str, list[str]] = {}
        for op in _HANDLERS:
            ops = handlers.get(op)
            block[op] = _dedupe_sorted(ops) if isinstance(ops, list) else []
        out[cfn_type] = block
    return out


def consolidate_service_reference_from_legacy(
    cfn_doc: dict[str, Any],
    *,
    legacy_dir: Path = _LEGACY_SR,
) -> dict[str, Any]:
    needed = actions_needed_for_handlers(cfn_doc)
    out: dict[str, Any] = {}
    for stem in sorted(needed):
        path = legacy_dir / f"{stem}.json"
        if not path.is_file():
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        out[stem] = trim_service_reference(doc, needed[stem])
    return out


def _cfn_types_from_synth(account: str, region: str) -> frozenset[str]:
    from infra.cfn_handler_permission_tests import (
        _DEV_APP_TEMPLATE_TYPES,
        dev_template_resource_types,
        synth_dev_application_templates,
    )

    templates = synth_dev_application_templates(account, region)
    found: set[str] = set()
    for template in templates:
        found.update(dev_template_resource_types(template))
    # Keep explicit allow-list in sync; synth must not introduce surprises without a list update.
    extra = found - _DEV_APP_TEMPLATE_TYPES
    if extra:
        raise SystemExit(
            "New AWS:: types in dev synth not in _DEV_APP_TEMPLATE_TYPES: "
            + ", ".join(sorted(extra))
        )
    return _DEV_APP_TEMPLATE_TYPES


def _permissions_from_handler(value: Any) -> list[str]:
    if isinstance(value, list):
        return _dedupe_sorted(value)
    if isinstance(value, dict):
        perms = value.get("permissions")
        if isinstance(perms, list):
            return _dedupe_sorted(perms)
    return []


def _fetch_cfn_handlers(cfn_type: str, region: str) -> dict[str, list[str]]:
    import boto3

    client = boto3.client("cloudformation", region_name=region)
    schema_text = client.describe_type(
        Type="RESOURCE",
        TypeName=cfn_type,
    )["Schema"]
    schema = json.loads(schema_text)
    handlers = schema.get("handlers") or {}
    return {op: _permissions_from_handler(handlers.get(op)) for op in _HANDLERS}


def _download_service_doc(stem: str) -> dict[str, Any]:
    url = f"{_SR_BASE}/{stem}/{stem}.json"
    with urllib.request.urlopen(url, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def refresh_from_aws(
    types: frozenset[str],
    *,
    region: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    cfn_doc: dict[str, Any] = {}
    for cfn_type in sorted(types):
        if cfn_type == "AWS::CDK::Metadata":
            continue
        if cfn_type == "AWS::CloudFormation::CustomResource":
            cfn_doc[cfn_type] = {op: [] for op in _HANDLERS}
            continue
        cfn_doc[cfn_type] = _fetch_cfn_handlers(cfn_type, region)
    needed = actions_needed_for_handlers(cfn_doc)
    sr_doc: dict[str, Any] = {}
    for stem in sorted(needed):
        full = _download_service_doc(stem)
        sr_doc[stem] = trim_service_reference(full, needed[stem])
    return cfn_doc, sr_doc


def dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write iam-fixtures JSON files.")
    parser.add_argument(
        "--from-legacy",
        action="store_true",
        help="Consolidate existing cfn-handler-permissions/ and service-reference/ (no AWS).",
    )
    parser.add_argument("--account", default="000000000000")
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args(argv)

    from infra.cfn_handler_permission_tests import _DEV_APP_TEMPLATE_TYPES

    if args.from_legacy:
        cfn_doc = consolidate_cfn_from_legacy(_DEV_APP_TEMPLATE_TYPES)
        sr_doc = consolidate_service_reference_from_legacy(cfn_doc)
        before_cfn = dir_bytes(_LEGACY_CFN)
        before_sr = dir_bytes(_LEGACY_SR)
    else:
        types = _cfn_types_from_synth(args.account, args.region)
        cfn_doc, sr_doc = refresh_from_aws(types, region=args.region)
        before_cfn = dir_bytes(_CFN_OUT)
        before_sr = dir_bytes(_SR_OUT)

    _write_json(_CFN_OUT, cfn_doc)
    _write_json(_SR_OUT, sr_doc)
    after_cfn = _CFN_OUT.stat().st_size
    after_sr = _SR_OUT.stat().st_size

    print(f"cfn-handler-permissions: {before_cfn} -> {after_cfn} bytes")
    print(f"service-reference: {before_sr} -> {after_sr} bytes")
    print(f"wrote {_CFN_OUT.relative_to(_REPO_ROOT)} ({len(cfn_doc)} types)")
    print(f"wrote {_SR_OUT.relative_to(_REPO_ROOT)} ({len(sr_doc)} services)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
