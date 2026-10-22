"""AgentCore runtime for the OERF decision-support advisory (OERF-206).

The runtime is invoked by the Interpret step when context ``agentcore:enabled``
is true. It reads the incident and the tenant tables with a read-only role and
returns an advisory. It writes nothing, sends nothing, and holds no task token.

``POST /invocations`` accepts ``tenant_id``, ``incident_id``, ``task``,
``inputs``, and ``trace_id``. It never echoes them. The response is either an
advisory (``{"status": "advisory", "fallback": false, ...}``) or the documented
fallback (``{"status": "placeholder", "fallback": true}``); a caller always
keeps its deterministic path either way.
"""

from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from oerf.platform.agent.agentcore.decision import (
    Advisory,
    DynamoIncidentReader,
    IncidentMissing,
    IncidentReader,
    TenantMismatch,
    advise,
    policy_for,
    resolve_config,
)
from oerf.platform.evidence.policy import dynamo_readers

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_ENVELOPE = ("tenant_id", "incident_id", "task", "inputs", "trace_id")
_PLACEHOLDER: dict[str, object] = {"status": "placeholder", "fallback": True}
_PORT = 8080
_MAX_BODY_BYTES = 64 * 1024

_client: Any = None
_incidents: IncidentReader | None = None


def handle(event: object) -> dict[str, object]:
    """Read one incident and return an advisory, or the documented fallback.

    Anything unrecognized, unavailable, or inconsistent returns the fallback:
    the caller's deterministic path then owns the decision. No error message
    echoes the body.
    """

    envelope = _envelope(event)
    if envelope is None:
        return dict(_PLACEHOLDER)
    try:
        return _advisory(envelope)
    except (IncidentMissing, TenantMismatch) as exc:
        logger.warning("agent advisory refused for incident %s: %s", envelope["incident_id"], exc)
    except Exception as exc:  # noqa: BLE001 - the runtime never 500s a calling workflow
        logger.exception("agent advisory failed for incident %s: %s", envelope["incident_id"], exc)
    return dict(_PLACEHOLDER)


def _advisory(envelope: dict[str, str]) -> dict[str, object]:
    """Build the advisory for one validated envelope."""

    reader, client = _readers()
    incident_table = os.environ.get("INCIDENT_TABLE_NAME", "")
    tenant_table = os.environ.get("TENANT_TABLE_NAME", "")
    if not incident_table:
        raise IncidentMissing("INCIDENT_TABLE_NAME is not configured")
    if client is None and reader is None:
        return dict(_PLACEHOLDER)

    stored = (reader or DynamoIncidentReader(client, incident_table)).incident(
        envelope["tenant_id"], envelope["incident_id"]
    )
    if stored is None:
        raise IncidentMissing(f"incident {envelope['incident_id']} does not exist")

    # The stored row owns the tenant. The envelope only names it.
    if str(stored.get("tenant_id", "")) != envelope["tenant_id"]:
        raise TenantMismatch("the envelope tenant does not own that incident")

    readable = tenant_table and client is not None
    tenant_reader, config_reader = (
        dynamo_readers(client, tenant_table) if readable else (None, None)
    )
    policy = policy_for(envelope["tenant_id"], tenant=tenant_reader, config=config_reader)
    config = resolve_config(envelope["tenant_id"], repository=config_reader)

    note = _model_note(stored, policy)
    advisory = advise(incident=stored, policy=policy, config=config, model_note=note)
    return _response(advisory)


def _response(advisory: Advisory) -> dict[str, object]:
    return advisory.to_event()


def _model_note(incident: dict[str, Any], policy: Any) -> str | None:
    """Ask the agent for one note, when the SDK is installed.

    The prompt carries the state, the reason token, and the verified fault
    codes only. An unverified code the model claims stays unverified, because
    ``decision.advise`` compares every note against the tenant catalog.
    """

    from oerf.platform.agent.agentcore import advisory as advisory_module

    evidence = incident.get("evidence_analysis")
    codes: list[str] = []
    if isinstance(evidence, dict):
        raw = evidence.get("fault_codes")
        if isinstance(raw, list):
            codes = [
                code for code in raw if isinstance(code, str) and policy.knows_fault_code(code)
            ]
    state = str(incident.get("state") or incident.get("status") or "UNKNOWN")
    reason = str(incident.get("reason") or "UNKNOWN")
    agent = advisory_module.build_agent()
    return advisory_module.note_for(state, reason, codes, agent=agent)


def _envelope(event: object) -> dict[str, str] | None:
    """Validate the invocation body, or return ``None`` for the fallback."""

    if not isinstance(event, dict):
        return None
    if not all(name in event for name in _ENVELOPE):
        return None
    tenant_id = event.get("tenant_id")
    incident_id = event.get("incident_id")
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        return None
    if not isinstance(incident_id, str) or not incident_id.strip():
        return None
    return {"tenant_id": tenant_id.strip(), "incident_id": incident_id.strip()}


def _readers() -> tuple[IncidentReader | None, Any]:
    """The injected readers, or the module client the runtime built."""

    return _incidents, _client


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/ping":
            self._send(404, {"status": "placeholder", "fallback": True})
            return
        self._send(200, {"status": "Healthy"})

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/invocations":
            self._send(404, _PLACEHOLDER)
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length < 0 or length > _MAX_BODY_BYTES:
            self._send(200, dict(_PLACEHOLDER))
            return
        raw = self.rfile.read(length) if length else b""
        try:
            event = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            event = {}
        self._send(200, handle(event))

    def log_message(self, fmt: str, *args: object) -> None:
        """Drop request logs. The body can carry tenant data."""

        return

    def _send(self, status: int, body: dict[str, object]) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def serve(port: int = _PORT) -> None:
    """Build the read-only readers once, then listen for invocations."""

    global _client
    if os.environ.get("INCIDENT_TABLE_NAME"):
        import boto3

        _client = boto3.client("dynamodb")
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    server.serve_forever()


if __name__ == "__main__":
    serve()
