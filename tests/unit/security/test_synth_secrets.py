"""Synth values must not look like secrets.

Scans Lambda environment values, stack outputs, and parameter defaults for
every app variant. A hit fails the test. Exact allow-list entries are the
only exception, and each one names why it is safe.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

REPO = Path(__file__).resolve().parents[3]
ACCOUNT = "000000000000"
REGION = "us-east-1"

# Exact strings that match a pattern and are safe to ship. Add a value only
# when a synth emits it, with the reason beside it. Nothing emitted today.
ALLOWED: frozenset[str] = frozenset()

_AWS_KEY = re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_PASSWORD = re.compile(r"password\s*=", re.IGNORECASE)
_E164 = re.compile(r"\+[1-9]\d{7,14}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_TOKEN = re.compile(r"^[A-Za-z0-9+/=_-]{32,}$")
_VARIANTS = (
    ("default", []),
    ("dashboard", ["dashboard:enabled=true"]),
    ("waba", ["whatsapp:wabaId=waba-0000000000000000"]),
    ("agentcore", ["agentcore:enabled=true"]),
)


def secret_reasons(value: str, allowed: frozenset[str] = ALLOWED) -> list[str]:
    """Names of the secret patterns this string matches."""

    if not isinstance(value, str) or value in allowed:
        return []
    reasons: list[str] = []
    if _AWS_KEY.search(value):
        reasons.append("aws-access-key")
    if _PRIVATE_KEY.search(value):
        reasons.append("private-key")
    if _PASSWORD.search(value):
        reasons.append("password")
    if _E164.search(value):
        reasons.append("e164")
    if _EMAIL.search(value):
        reasons.append("email")
    if _high_entropy(value):
        reasons.append("high-entropy-token")
    return reasons


def test_secret_patterns_match_each_class_and_the_allow_list_is_exact() -> None:
    planted = {
        "aws-access-key": "AKIAIOSFODNN7EXAMPLE",
        "private-key": "-----BEGIN RSA PRIVATE KEY-----",
        "password": "password=not-committed",
        "e164": "+14155552671",
        "email": "person@gmail.com",
        "high-entropy-token": "aB3dEf7hJk2mNp5qRs8tUv1wXy4zAa0b",
    }
    for reason, value in planted.items():
        assert reason in secret_reasons(value)
        assert secret_reasons(value, frozenset({value})) == []
    assert secret_reasons("INFO") == []
    assert secret_reasons("amazon.nova-lite-v1:0") == []
    assert secret_reasons("/oerf/whatsapp/phone-number-id") == []
    assert secret_reasons("/cdk-bootstrap/hnb659fds/version") == []
    assert "high-entropy-token" in secret_reasons("0123456789abcdef" * 2)
    assert _strings({"Fn::GetAtt": ["EvidenceAnalyzerFunction5CEE69F8", "Arn"]}) == []
    assert _strings({"Ref": "AWS::AccountId"}) == []
    assert "password=secret" in _strings({"Fn::Join": ["", ["password=secret"]]})


def test_synthesized_stacks_have_no_secret_shaped_values(tmp_path: Path) -> None:
    hits: list[str] = []
    for name, context in _VARIANTS:
        outdir = tmp_path / name
        outdir.mkdir()
        _synth(outdir, context)
        for template in sorted(outdir.glob("*.template.json")):
            hits.extend(_scan(name, template))
    assert hits == []


def _high_entropy(value: str) -> bool:
    """A bare token, not a path, URL, or ARN.

    Hex strings cap near 4 bits per character, so those are matched by shape.
    Other tokens have to look random. The CDK bootstrap parameter default
    starts with ``/`` and is not a credential.
    """

    if len(value) < 32 or value.startswith(("/", "arn:")) or "://" in value:
        return False
    if _TOKEN.fullmatch(value) is None:
        return False
    if re.fullmatch(r"[0-9a-fA-F]{32,}", value):
        return True
    counts = Counter(value)
    total = len(value)
    entropy = -sum((count / total) * math.log2(count / total) for count in counts.values())
    return entropy >= 4.5


def _synth(outdir: Path, context: list[str]) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "CDK_DEFAULT_ACCOUNT",
            "SES_SOURCE_EMAIL",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
        }
    }
    env.update(
        {
            "CDK_OUTDIR": str(outdir),
            "CDK_DEFAULT_ACCOUNT": ACCOUNT,
            "CDK_DEFAULT_REGION": REGION,
            "AWS_EC2_METADATA_DISABLED": "true",
            "SES_SOURCE_EMAIL": "",
        }
    )
    command = [sys.executable, "-m", "infra.app", "--context", f"account={ACCOUNT}"]
    for item in context:
        command.extend(["--context", item])
    result = subprocess.run(
        command,
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def _scan(variant: str, path: Path) -> list[str]:
    template = json.loads(path.read_text(encoding="utf-8"))
    stack = path.name.removesuffix(".template.json")
    found: list[tuple[str, str]] = []
    for logical, resource in template.get("Resources", {}).items():
        if resource.get("Type") != "AWS::Lambda::Function":
            continue
        variables = resource.get("Properties", {}).get("Environment", {}).get("Variables", {})
        if not isinstance(variables, dict):
            continue
        for key, raw in variables.items():
            for text in _strings(raw):
                found.append((f"{variant} {stack} {logical} env {key}", text))
    for key, output in template.get("Outputs", {}).items():
        for text in _strings(output.get("Value")):
            found.append((f"{variant} {stack} output {key}", text))
    for key, parameter in template.get("Parameters", {}).items():
        if isinstance(parameter, dict) and "Default" in parameter:
            for text in _strings(parameter["Default"]):
                found.append((f"{variant} {stack} parameter {key}", text))
    hits: list[str] = []
    for where, text in found:
        reasons = secret_reasons(text)
        if reasons:
            hits.append(f"{where} ({', '.join(reasons)})")
    return hits


_SKIP_INTRINSIC = frozenset({"Ref", "Fn::GetAtt", "Fn::ImportValue"})


def _strings(value: object) -> list[str]:
    """String leaves, omitting CloudFormation resource references."""

    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        if len(value) == 1 and next(iter(value)) in _SKIP_INTRINSIC:
            return []
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []
