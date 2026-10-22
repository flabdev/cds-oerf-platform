"""Incident read and deterministic decision support for the AgentCore runtime.

Design principle 1 of the spec is "AI understands, rules decide, people
approve". This module is the rules half: it reads the incident and the tenant's
configuration with the runtime's read-only role and returns a recommendation
that is advisory only. Nothing here writes, and no number the model produced is
trusted:

* the tenant is taken from the incident record, never from the request body;
* the tenant's own policy sets what counts as verified evidence;
* a model note that names something outside the sanitized vocabulary is dropped
  rather than surfaced.

The runtime has no incident-table write and no step-function permission, so
even a compromised model cannot act through it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from oerf.platform.evidence.policy import EvidencePolicy, resolve_policy
from oerf.platform.tenancy.config_resolver import ConfigError, ResolvedConfig, get_config
from oerf.platform.tenancy.context import TenantContext

#: The domain whose configuration the advisory reads.
DOMAIN = "cold_chain_excursion"
#: How many actions the advisory ever recommends.
MAX_ACTIONS = 3
#: The vocabulary a model note may use. Anything else is dropped.
ACTIONS = (
    "ASK_DRIVER_FOR_EVIDENCE",
    "CONFIRM_TRANSFER",
    "PICK_OPTION",
    "ESCALATE_TO_SUPERVISOR",
    "REOPEN_EXCURSION",
    "PROCEED_WITH_DECISION",
    "WAIT_FOR_APPROVAL",
)


class IncidentMissing(LookupError):
    """The incident the request names does not exist for that tenant."""


class TenantMismatch(PermissionError):
    """The envelope tenant and the stored incident tenant disagree."""


class IncidentReader(Protocol):
    """The read-only incident access the runtime is granted."""

    def incident(self, tenant_id: str, incident_id: str) -> dict[str, Any] | None:
        """Return the incident item, or ``None`` when this tenant has none."""


class DynamoIncidentReader:
    """Read-only incident access over one low-level DynamoDB client.

    The runtime's role grants ``dynamodb:GetItem`` on the incident table and
    nothing else, so the only method here is a read.
    """

    def __init__(self, client: Any, table_name: str) -> None:
        self._client = client
        self._table = table_name

    def incident(self, tenant_id: str, incident_id: str) -> dict[str, Any] | None:
        for shape in incident_key_shapes(tenant_id, incident_id):
            response = self._client.get_item(
                TableName=self._table,
                Key={"pk": _s(shape["pk"]), "sk": _s(shape["sk"])},
            )
            item = response.get("Item")
            if item:
                return _document(item)
        return None


@dataclass(frozen=True)
class Advisory:
    """A recommendation for the Interpret step. Advisory only."""

    tenant_id: str
    incident_id: str
    state: str
    recommended_actions: list[str]
    missing_information: list[str]
    requires_human_approval: bool
    confidence: float
    config_version: int | None
    notes: list[str] = field(default_factory=list)
    model_assisted: bool = False

    def to_event(self) -> dict[str, Any]:
        """The sanitized response body. No prompt, image, or raw text."""

        return {
            "status": "advisory",
            "fallback": False,
            "advisory": {
                "tenant_id": self.tenant_id,
                "incident_id": self.incident_id,
                "state": self.state,
                "recommended_actions": list(self.recommended_actions),
                "missing_information": list(self.missing_information),
                "requires_human_approval": self.requires_human_approval,
                "confidence": self.confidence,
                "config_version": self.config_version,
                "notes": list(self.notes),
                "model_assisted": self.model_assisted,
            },
        }


def tenant_of(incident: dict[str, Any]) -> str:
    """The tenant the stored incident belongs to, or raise."""

    value = incident.get("tenant_id")
    if not isinstance(value, str) or not value.strip():
        raise TenantMismatch("the stored incident has no tenant_id")
    return value.strip()


def advise(
    *,
    incident: dict[str, Any],
    policy: EvidencePolicy,
    config: ResolvedConfig | None,
    model_note: str | None = None,
) -> Advisory:
    """Turn one incident read into an advisory recommendation.

    ``config`` is the tenant's resolved configuration when it is readable. A
    missing config degrades to the conservative branch: the advisory asks for
    evidence and asks for a human.
    """

    incident_id = str(incident.get("incident_id") or incident.get("sk", ""))
    tenant_id = tenant_of(incident)
    state = str(incident.get("state") or incident.get("status") or "UNKNOWN")
    actions: list[str] = []
    missing: list[str] = []
    notes: list[str] = []

    evidence = incident.get("evidence_analysis")
    verification = incident.get("evidence_verification")
    if not isinstance(evidence, dict) or not evidence:
        missing.append("evidence")
        actions.append("ASK_DRIVER_FOR_EVIDENCE")
    else:
        if isinstance(verification, dict) and verification.get("requires_confirmation"):
            missing.append("evidence_confirmation")
            actions.append("ASK_DRIVER_FOR_EVIDENCE")
        notes.extend(_evidence_note(evidence, policy))

    if config is None:
        missing.append("configuration")
        notes.append("tenant configuration unreadable; nothing unverified is recommended")
        actions.append("WAIT_FOR_APPROVAL")
    elif state == "AWAITING_APPROVAL":
        actions.append("WAIT_FOR_APPROVAL")
    elif state in {"VERIFYING", "REOPENED"}:
        actions.append("REOPEN_EXCURSION")
    elif state in {"CONTAINED", "AWAITING_DISPOSITION"}:
        actions.append("CONFIRM_TRANSFER")
    elif config.approval.auto_proceed_if_within_limits:
        actions.append("PROCEED_WITH_DECISION")
    else:
        actions.append("WAIT_FOR_APPROVAL")

    if model_note:
        actions.extend(code for code in _model_actions(model_note) if code not in actions)
        notes.append("model note: " + _sanitize_note(model_note))

    return Advisory(
        tenant_id=tenant_id,
        incident_id=incident_id,
        state=state,
        recommended_actions=_dedupe(actions)[:MAX_ACTIONS],
        missing_information=missing,
        requires_human_approval=config is None
        or not config.approval.auto_proceed_if_within_limits,
        confidence=_confidence(evidence, verification, config is not None, policy),
        config_version=_version(incident, config),
        notes=notes,
        model_assisted=bool(model_note),
    )


def resolve_config(tenant_id: str, *, repository: Any | None = None) -> ResolvedConfig | None:
    """The tenant's configuration, or ``None`` when it cannot be resolved."""

    try:
        return get_config(tenant_id, DOMAIN, repository=repository)
    except ConfigError:
        return None


def policy_for(tenant_id: str, *, tenant: Any | None = None, config: Any | None = None):
    """The tenant's evidence policy, degrading instead of raising."""

    return resolve_policy(tenant_id, tenant=tenant, config=config)


def _evidence_note(evidence: dict[str, Any], policy: EvidencePolicy) -> list[str]:
    """Which observed fault codes the tenant catalog knows and which it does not."""

    codes = evidence.get("fault_codes")
    if not isinstance(codes, list):
        return []
    known = [code for code in codes if isinstance(code, str) and policy.knows_fault_code(code)]
    unknown = [
        code for code in codes if isinstance(code, str) and not policy.knows_fault_code(code)
    ]
    notes: list[str] = []
    if known:
        notes.append("tenant catalog has: " + ", ".join(sorted(known)))
    if unknown:
        notes.append("not in the tenant catalog, ask a human: " + ", ".join(sorted(unknown)))
    return notes


def _confidence(
    evidence: Any,
    verification: Any,
    has_config: bool,
    policy: EvidencePolicy,
) -> float:
    """The advisory's own confidence. It never exceeds what the evidence shows.

    A claimed fault code the tenant catalog does not contain, an evidence record
    this invocation cannot confirm, or an unreadable configuration caps the
    score below a coin flip, so the caller asks a human.
    """

    score = 0.0
    if isinstance(evidence, dict):
        raw = evidence.get("confidence_score")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            score = float(raw)
    codes = evidence.get("fault_codes") if isinstance(evidence, dict) else None
    unverified = [
        code
        for code in codes or []
        if isinstance(code, str) and not policy.knows_fault_code(code)
    ]
    if isinstance(verification, dict) and verification.get("requires_confirmation"):
        score = min(score, 0.49)
    if unverified or not has_config:
        score = min(score, 0.49)
    return round(score, 4)


def _version(incident: dict[str, Any], config: ResolvedConfig | None) -> int | None:
    raw = incident.get("config_version")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw)
    snapshot = incident.get("config_snapshot")
    if isinstance(snapshot, dict):
        version = snapshot.get("version")
        if isinstance(version, (int, float)) and not isinstance(version, bool):
            return int(version)
    return config.version if config is not None else None


def _model_actions(note: str) -> list[str]:
    """Whitelist a model note onto the action vocabulary."""

    upper = note.upper()
    return [code for code in ACTIONS if code in upper]


def _sanitize_note(note: str) -> str:
    """Keep a note short and free of line breaks, so it cannot smuggle a prompt."""

    return " ".join(note.split())[:200]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _document(item: dict[str, Any]) -> dict[str, Any]:
    """Turn one low-level item into a plain document. Numbers and JSON stay typed."""

    document: dict[str, Any] = {}
    for name, attribute in item.items():
        if not isinstance(attribute, dict):
            continue
        if "S" in attribute:
            raw = attribute["S"]
            if raw.startswith("{") or raw.startswith("["):
                try:
                    document[name] = json.loads(raw)
                except json.JSONDecodeError:
                    document[name] = raw
            else:
                document[name] = raw
        elif "N" in attribute:
            raw_number = attribute["N"]
            document[name] = int(raw_number) if "." not in raw_number else float(raw_number)
        elif "BOOL" in attribute:
            document[name] = bool(attribute["BOOL"])
        elif "NULL" in attribute:
            document[name] = None
        elif "M" in attribute:
            document[name] = _document(attribute["M"])
    return document


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def incident_key_shapes(tenant_id: str, incident_id: str) -> list[dict[str, str]]:
    """Both key shapes the incident table uses, contract first.

    ``docs/interfaces.md`` freezes ``PK=TENANT#{tid}#INC#{iid}``. The deployed
    action handler writes ``PK=TENANT#{tid}``, ``SK=INCIDENT#{iid}``, so the
    runtime reads either while that deviation is open.
    """

    context = TenantContext(tenant_id=tenant_id)
    return [
        {"pk": f"TENANT#{context.tenant_id}#INC#{incident_id}", "sk": "META"},
        {"pk": f"TENANT#{context.tenant_id}", "sk": f"INCIDENT#{incident_id}"},
    ]