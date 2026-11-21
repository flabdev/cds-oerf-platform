"""Map a resumed reply onto ``$.response``. Rules decide; the model proposes."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from botocore.config import Config

from oerf.platform.evidence.policy import EvidencePolicy

Reply = tuple[str, bool, dict[str, str]]
MAX_REPLY_TEXT = 4_000
#: The numeric floor OERF-211 chose, matching the tenant evidence default
#: ``evidence.min_confidence``. A proposal below it is unusable and maps to
#: ``clarify``. ``None`` still fails closed, so a caller can refuse outright.
CLASSIFIER_CONFIDENCE_FLOOR: float | None = 0.8
#: Worst case for the AgentCore hop is ``2 * (1 + 10) = 22`` seconds, under the
#: 30-second Interpret Lambda. Values outside these bounds fall back to the
#: default so an environment override cannot outlive that budget.
_AGENTCORE_CONNECT_TIMEOUT = 1
_AGENTCORE_READ_TIMEOUT = 10
_AGENTCORE_MAX_ATTEMPTS = 2
#: Jev answers in well under a second. The tenant read (3 s), the key read
#: (3 s), and one Jev call (at most 5 s) stay far under the 30-second Lambda.
_JEV_TIMEOUT = 3
CATEGORIES = frozenset({"answer", "decline", "help", "concern", "call-me", "unrelated"})
INTENTS = frozenset({"unit_running", "unit_stopped", "unknown", "decline", "call_me", "clarify"})
_PROPOSAL_KEYS = frozenset({"category", "intent", "fields", "confidence"})
_FIELD_KEYS = frozenset({"fault_code", "setpoint_temp_c"})


class Classifier(Protocol):
    """Injectable boundary for bounded free-text classification."""

    def __call__(
        self, tenant_id: str, incident_id: str, reply_text: str, policy_id: str | None
    ) -> object: ...


#: Driver diagnose (``critical_field``). A facility word is not a diagnosis.
_CRITICAL_FIELD: dict[str, Reply] = {
    "running": ("unit_running", True, {"unit": "running"}),
    "stopped": ("unit_stopped", True, {"unit": "stopped"}),
    "not sure": ("unknown", False, {}),
    "call me": ("call_me", False, {}),
}

#: Facility accept/decline (``partner_request``). A unit-status word is not an answer.
_PARTNER_REQUEST: dict[str, Reply] = {
    "accept": ("accept", True, {}),
    "accepted": ("accept", True, {}),
    "decline": ("decline", True, {}),
    "declined": ("decline", True, {}),
    "call me": ("call_me", False, {}),
}

_REPLIES_BY_POLICY: dict[str, dict[str, Reply]] = {
    "critical_field": _CRITICAL_FIELD,
    "partner_request": _PARTNER_REQUEST,
}

#: Every other policy keeps the combined map these two used to share.
_REPLIES: dict[str, Reply] = {**_CRITICAL_FIELD, **_PARTNER_REQUEST}

Consult = Callable[[str, str], dict[str, Any] | None]


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Replace the execution with the same document plus a validated response."""

    workflow = event.get("workflow")
    if not isinstance(workflow, dict):
        raise ValueError("interpret needs a workflow")
    backend = classifier_backend()
    enabled = _backend_enabled(backend)
    tenant_id = workflow.get("tenant_id")
    reply = workflow.get("reply")
    text = reply.get("text") if isinstance(reply, dict) else None
    policy_id = workflow.get("policy_id")
    policy_id = policy_id if isinstance(policy_id, str) else None
    # Buttons and known words never call the model, so they never read the catalog.
    # Only driver diagnose (``critical_field``) classifies free text. Any other
    # policy, or a missing one, clarifies without a model call or a tenant read.
    needs_policy = (
        enabled
        and policy_id == "critical_field"
        and isinstance(tenant_id, str)
        and map_reply(text if isinstance(text, str) else None, policy_id) is None
    )
    policy = _tenant_policy(tenant_id) if needs_policy else None

    def knows_fault_code(checked: str, code: str) -> bool:
        return _policy_knows(policy, checked, code)

    classify: Classifier = (
        _jev_classifier(knows_fault_code) if backend == "jev" else _runtime_classifier
    )
    return interpret_workflow(
        workflow,
        enabled=enabled,
        classify=classify,
        confidence_floor=_floor_from_policy(policy),
        knows_fault_code=knows_fault_code,
    )


def classifier_backend() -> str:
    """``agentcore`` (the default) or ``jev``. Any other value disables free text."""

    return os.environ.get("INTERPRET_CLASSIFIER", "agentcore").strip().lower() or "agentcore"


def _backend_enabled(backend: str) -> bool:
    if backend == "agentcore":
        return os.environ.get("AGENTCORE_ENABLED", "false").strip().lower() == "true"
    if backend == "jev":
        return bool(os.environ.get("JEV_API_KEY_PARAM", "").strip())
    return False


def _jev_classifier(knows_fault_code: Callable[[str, str], bool]) -> Classifier:
    """The Jev classifier for this invocation, with the tenant catalog check."""

    from oerf.platform.agent.jev import DEFAULT_ENDPOINT, DEFAULT_MODEL
    from oerf.platform.cadence.jev_classifier import JevReplyClassifier

    return JevReplyClassifier(
        api_key=_jev_api_key,
        knows_fault_code=knows_fault_code,
        model=os.environ.get("JEV_MODEL", "").strip() or DEFAULT_MODEL,
        endpoint=os.environ.get("JEV_ENDPOINT", "").strip() or DEFAULT_ENDPOINT,
        timeout=float(_bounded_int("JEV_TIMEOUT_SECONDS", _JEV_TIMEOUT, low=1, high=5)),
    )


_jev_key_cache: dict[str, str] = {}


def _jev_api_key() -> str:
    """The Jev key from the SecureString ``JEV_API_KEY_PARAM`` names, cached per process.

    A failed or empty read raises and is not cached, so Interpret clarifies and
    the next invocation reads SSM again.
    """

    name = os.environ.get("JEV_API_KEY_PARAM", "").strip()
    if not name:
        raise RuntimeError("JEV_API_KEY_PARAM is not set")
    cached = _jev_key_cache.get(name)
    if cached:
        return cached
    import boto3

    response = boto3.client("ssm", config=ssm_client_config()).get_parameter(
        Name=name, WithDecryption=True
    )
    value = (response.get("Parameter") or {}).get("Value")
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("the Jev API key parameter is empty")
    _jev_key_cache[name] = value.strip()
    return _jev_key_cache[name]


def ssm_client_config() -> Config:
    """One short read for the Jev key, inside the Interpret budget."""

    return Config(
        connect_timeout=1,
        read_timeout=2,
        retries={"max_attempts": 1, "mode": "standard"},
    )


def interpret_workflow(
    workflow: dict[str, Any],
    *,
    enabled: bool,
    classify: Classifier | None = None,
    confidence_floor: float | None = CLASSIFIER_CONFIDENCE_FLOOR,
    knows_fault_code: Callable[[str, str], bool] | None = None,
) -> dict[str, Any]:
    """Map known words deterministically; validate any free-text proposal."""

    reply = workflow.get("reply")
    text = reply.get("text") if isinstance(reply, dict) else None
    policy_id = workflow.get("policy_id")
    policy_id = policy_id if isinstance(policy_id, str) else None
    mapped = map_reply(text if isinstance(text, str) else None, policy_id)
    if mapped is not None:
        intent, valid, fields = mapped
        response: dict[str, Any] = {
            "intent": intent,
            "valid": valid,
            "fields": fields,
            "evidence_refs": [],
        }
    else:
        response = _classify_reply(
            workflow,
            text,
            policy_id,
            enabled=enabled,
            classify=classify,
            confidence_floor=confidence_floor,
            knows_fault_code=knows_fault_code,
        )
    updated = dict(workflow)
    updated["response"] = response
    return updated


def map_reply(text: str | None, policy_id: str | None = None) -> Reply | None:
    """A known button or keyword for this policy, or ``None`` when it is not."""

    if text is None:
        return None
    folded = " ".join(text.strip().casefold().replace("_", " ").split())
    if not folded:
        return None
    named = policy_id.strip() if isinstance(policy_id, str) else ""
    return _REPLIES_BY_POLICY.get(named, _REPLIES).get(folded)


def _classify_reply(
    workflow: dict[str, Any],
    text: object,
    policy_id: str | None,
    *,
    enabled: bool,
    classify: Classifier | None,
    confidence_floor: float | None,
    knows_fault_code: Callable[[str, str], bool] | None,
) -> dict[str, Any]:
    # Facility, approval, and every unnamed policy stay on the keyword map.
    if policy_id != "critical_field":
        return _clarification()
    tenant_id, incident_id = workflow.get("tenant_id"), workflow.get("incident_id")
    if (
        not isinstance(text, str)
        or not text.strip()
        or len(text) > MAX_REPLY_TEXT
        or not enabled
        or classify is None
        or not isinstance(tenant_id, str)
        or not tenant_id.strip()
        or not isinstance(incident_id, str)
        or not incident_id.strip()
    ):
        return _clarification()
    try:
        proposal = classify(tenant_id, incident_id, text, policy_id)
    except Exception:  # noqa: BLE001 - failures never fail the engagement
        return _clarification()
    return validate_proposal(
        proposal,
        tenant_id=tenant_id,
        confidence_floor=confidence_floor,
        knows_fault_code=knows_fault_code,
    )


def validate_proposal(
    proposal: object,
    *,
    tenant_id: str,
    confidence_floor: float | None,
    knows_fault_code: Callable[[str, str], bool] | None = None,
) -> dict[str, Any]:
    """Validate untrusted output. The model never supplies the final ``valid`` flag."""

    category = proposal.get("category") if isinstance(proposal, Mapping) else None
    safe_category = category if isinstance(category, str) and category in CATEGORIES else None
    raw_confidence = proposal.get("confidence") if isinstance(proposal, Mapping) else None
    confidence = (
        raw_confidence if _finite_number(raw_confidence) and 0 <= raw_confidence <= 1 else None
    )
    fallback = _clarification(safe_category, confidence)
    if not isinstance(proposal, Mapping) or set(proposal) != _PROPOSAL_KEYS:
        return fallback
    intent, fields = proposal.get("intent"), proposal.get("fields")
    if (
        safe_category is None
        or not isinstance(intent, str)
        or intent not in INTENTS
        or confidence is None
        or not _finite_number(confidence_floor)
        or not 0 <= confidence_floor <= 1
        or confidence < confidence_floor
        or not isinstance(fields, Mapping)
        or not set(fields).issubset(_FIELD_KEYS)
        or not _valid_fields(fields, tenant_id, knows_fault_code)
    ):
        return fallback
    # ``critical_field`` accepts a unit answer or a callback. A decline proposal
    # is not a diagnosis, so it clarifies instead of taking the answer path.
    expected = {
        "answer": {"unit_running", "unit_stopped", "unknown"},
        "call-me": {"call_me"},
    }
    if intent not in expected.get(safe_category, set()):
        return fallback
    return {
        "category": safe_category,
        "intent": intent,
        "valid": True,
        "fields": dict(fields),
        "confidence": confidence,
        "evidence_refs": [],
    }


def _valid_fields(
    fields: Mapping[str, Any],
    tenant_id: str,
    knows_fault_code: Callable[[str, str], bool] | None,
) -> bool:
    fault_code = fields.get("fault_code")
    if fault_code is not None:
        if not isinstance(fault_code, str) or not fault_code.strip() or knows_fault_code is None:
            return False
        try:
            if not knows_fault_code(tenant_id, fault_code):
                return False
        except Exception:  # noqa: BLE001 - unreadable tenant policy fails closed
            return False
    value = fields.get("setpoint_temp_c")
    return value is None or _finite_number(value)


def _finite_number(value: object) -> bool:
    """Accept finite JSON numbers, including very large Python integers safely."""

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _clarification(category: str | None = None, confidence: float | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {
        "intent": "clarify",
        "valid": False,
        "fields": {},
        "evidence_refs": [],
    }
    if category is not None:
        response["category"] = category
    if confidence is not None:
        response["confidence"] = confidence
    return response


def _floor_from_policy(policy: EvidencePolicy | None) -> float | None:
    """The tenant ``evidence.min_confidence``, or ``None`` when it cannot be read.

    ``None`` fails closed in ``validate_proposal``. A fallback policy is not a
    tenant choice, so it does not widen acceptance down to the packaged 0.8.
    """

    if policy is None or policy.source != "tenant_config":
        return None
    value = policy.min_confidence
    if not _finite_number(value) or not 0 <= float(value) <= 1:
        return None
    return float(value)


def _policy_knows(policy: EvidencePolicy | None, tenant_id: str, code: str) -> bool:
    """True only when this invocation's tenant catalog contains ``code``.

    A missing policy, a different tenant, or a reader error is unverified.
    """

    if policy is None or policy.tenant_id != tenant_id:
        return False
    try:
        return bool(policy.knows_fault_code(code))
    except Exception:  # noqa: BLE001 - an unreadable catalog fails closed
        return False


def _tenant_policy(tenant_id: str) -> EvidencePolicy | None:
    """Read one tenant's evidence policy, or ``None`` when that read cannot be trusted."""

    table = os.environ.get("TENANT_TABLE_NAME", "").strip()
    if not table or not tenant_id.strip():
        return None
    try:
        import boto3

        from oerf.platform.evidence.policy import dynamo_readers, resolve_policy

        tenant_reader, config_reader = dynamo_readers(
            boto3.client("dynamodb", config=tenant_client_config()), table
        )
        return resolve_policy(tenant_id, tenant=tenant_reader, config=config_reader)
    except Exception:  # noqa: BLE001 - an unreadable tenant config fails closed
        return None


def tenant_client_config() -> Config:
    """One short read. Added to the AgentCore budget, the worst case stays under 30 seconds."""

    return Config(
        connect_timeout=1,
        read_timeout=2,
        retries={"max_attempts": 1, "mode": "standard"},
    )


def agentcore_client_config() -> Config:
    """Bound ``InvokeAgentRuntime`` so a hang returns before the Lambda is killed."""

    return Config(
        connect_timeout=_bounded_int(
            "AGENTCORE_CONNECT_TIMEOUT", _AGENTCORE_CONNECT_TIMEOUT, low=1, high=2
        ),
        read_timeout=_bounded_int(
            "AGENTCORE_READ_TIMEOUT", _AGENTCORE_READ_TIMEOUT, low=1, high=10
        ),
        retries={
            "max_attempts": _bounded_int(
                "AGENTCORE_MAX_ATTEMPTS", _AGENTCORE_MAX_ATTEMPTS, low=1, high=2
            ),
            "mode": "standard",
        },
    )


def _bounded_int(name: str, default: int, *, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < low or value > high:
        return default
    return value


def _runtime_classifier(
    tenant_id: str, incident_id: str, reply_text: str, policy_id: str | None
) -> object:
    """Invoke the classifier with only the bounded reply and routing context."""

    del policy_id  # policy remains caller-side context; only reply_text is classifier input
    arn = os.environ.get("AGENT_RUNTIME_ARN", "").strip()
    if not arn:
        raise RuntimeError("classifier runtime is not configured")
    import boto3

    payload = {
        "tenant_id": tenant_id,
        "incident_id": incident_id,
        "task": "interpret",
        "inputs": {"reply_text": reply_text},
        "trace_id": incident_id,
    }
    prefix = os.environ.get("AGENT_SESSION_PREFIX", "oerf-interpret").strip() or "oerf-interpret"
    session = f"{prefix}-{incident_id}".ljust(33, "0")
    response = boto3.client(
        "bedrock-agentcore", config=agentcore_client_config()
    ).invoke_agent_runtime(
        agentRuntimeArn=arn,
        runtimeSessionId=session[:100],
        payload=json.dumps(payload).encode("utf-8"),
        contentType="application/json",
    )
    raw = response.get("response")
    if hasattr(raw, "read"):
        raw = raw.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("classifier returned no result")
    result = json.loads(raw)
    if not isinstance(result, dict) or result.get("fallback"):
        # The documented fallback (missing, malformed, blocked, or unusable).
        raise ValueError("classifier returned no usable proposal")
    proposal = result.get("proposal")
    if not isinstance(proposal, dict):
        raise ValueError("classifier returned no proposal")
    return proposal
