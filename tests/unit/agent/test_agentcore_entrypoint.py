"""The AgentCore placeholder accepts the envelope and returns only the fallback."""

from oerf.platform.agent.agentcore.app import handle

_ENVELOPE = {
    "tenant_id": "t1",
    "incident_id": "INC-1",
    "task": "diagnose",
    "inputs": {"note": "do-not-echo"},
    "trace_id": "trace-1",
}


def test_envelope_returns_the_placeholder() -> None:
    assert handle(_ENVELOPE) == {"status": "placeholder", "fallback": True}


def test_response_does_not_echo_the_envelope() -> None:
    result = handle(_ENVELOPE)
    assert "tenant_id" not in result
    assert "inputs" not in result
    assert "do-not-echo" not in str(result)


def test_a_bad_body_still_falls_back() -> None:
    assert handle({"task": "diagnose"}) == {"status": "placeholder", "fallback": True}
    assert handle("nope") == {"status": "placeholder", "fallback": True}
