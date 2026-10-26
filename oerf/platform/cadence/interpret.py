"""Map a resumed reply onto ``$.response``. Rules decide. The model only advises."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

#: Button labels and ids, after case and underscore folding.
_REPLIES: dict[str, tuple[str, bool, dict[str, str]]] = {
    "running": ("unit_running", True, {"unit": "running"}),
    "stopped": ("unit_stopped", True, {"unit": "stopped"}),
    "not sure": ("unknown", False, {}),
    "call me": ("call_me", False, {}),
}

Consult = Callable[[str, str], dict[str, Any] | None]


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Replace the execution with the same document plus ``response``."""

    workflow = event.get("workflow")
    if not isinstance(workflow, dict):
        raise ValueError("interpret needs a workflow")
    enabled = os.environ.get("AGENTCORE_ENABLED", "false").strip().lower() == "true"
    return interpret_workflow(workflow, enabled=enabled, consult=_runtime_advisory)


def interpret_workflow(
    workflow: dict[str, Any],
    *,
    enabled: bool,
    consult: Consult | None = None,
) -> dict[str, Any]:
    """Deterministic intent first. A free-form note never becomes the intent."""

    reply = workflow.get("reply")
    text = reply.get("text") if isinstance(reply, dict) else None
    mapped = map_reply(text if isinstance(text, str) else None)
    if mapped is None:
        intent, valid, fields = "clarify", False, {}
        advisory = _consult(workflow, enabled=enabled, consult=consult)
    else:
        intent, valid, fields = mapped
        advisory = None
    response: dict[str, Any] = {
        "intent": intent,
        "valid": valid,
        "fields": fields,
        "evidence_refs": [],
    }
    if advisory is not None:
        response["advisory"] = advisory
    updated = dict(workflow)
    updated["response"] = response
    return updated


def map_reply(text: str | None) -> tuple[str, bool, dict[str, str]] | None:
    """A known button or keyword, or ``None`` when the text is free-form."""

    if text is None:
        return None
    folded = " ".join(text.strip().casefold().replace("_", " ").split())
    if not folded:
        return None
    return _REPLIES.get(folded)


def _consult(
    workflow: dict[str, Any],
    *,
    enabled: bool,
    consult: Consult | None,
) -> dict[str, Any] | None:
    if not enabled or consult is None:
        return None
    tenant_id = workflow.get("tenant_id")
    incident_id = workflow.get("incident_id")
    if not isinstance(tenant_id, str) or not isinstance(incident_id, str):
        return None
    try:
        note = consult(tenant_id, incident_id)
    except Exception:  # noqa: BLE001 - a model failure stays on the rule result
        return None
    return _status_only(note)


def _status_only(note: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep the runtime status. Drop any action the model recommended."""

    if not isinstance(note, dict):
        return None
    status = note.get("status")
    if not isinstance(status, str) or not status.strip():
        return None
    return {"status": status.strip(), "fallback": bool(note.get("fallback"))}


def _runtime_advisory(tenant_id: str, incident_id: str) -> dict[str, Any] | None:
    """Invoke the AgentCore runtime when it is configured. The reply text stays here."""

    arn = os.environ.get("AGENT_RUNTIME_ARN", "").strip()
    if not arn:
        return None
    import boto3

    payload = {
        "tenant_id": tenant_id,
        "incident_id": incident_id,
        "task": "interpret",
        "inputs": {},
        "trace_id": incident_id,
    }
    session = f"oerf-interpret-{incident_id}".ljust(33, "0")
    response = boto3.client("bedrock-agentcore").invoke_agent_runtime(
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
        return {"status": "placeholder", "fallback": True}
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {"status": "placeholder", "fallback": True}
