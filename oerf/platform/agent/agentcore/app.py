"""placeholder, behaviour owned by OERF-206"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_ENVELOPE = ("tenant_id", "incident_id", "task", "inputs", "trace_id")
_PLACEHOLDER = {"status": "placeholder", "fallback": True}
_PORT = 8080


def handle(event: object) -> dict[str, object]:
    """placeholder, behaviour owned by OERF-206"""

    if isinstance(event, dict) and all(name in event for name in _ENVELOPE):
        return dict(_PLACEHOLDER)
    return dict(_PLACEHOLDER)


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
        raw = self.rfile.read(length) if length else b""
        try:
            event = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
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
    """Listen for AgentCore invocations. The handler stays a placeholder."""

    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    server.serve_forever()


if __name__ == "__main__":
    serve()
