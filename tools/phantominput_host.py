#!/usr/bin/env python3
"""PhantomInput native messaging host.

Chrome extensions can't host network servers in MV3. So the architecture
is: the extension speaks Native Messaging (stdio) to *this* process,
which exposes a localhost HTTP server. External callers (Claude Code, a
CI runner, a QA script) hit the HTTP server; this host forwards each
request to the extension service worker over stdio; the extension uses
chrome.debugger + CDP to inject trusted input into the target tab; the
response flows back through stdio → HTTP.

Native Messaging wire protocol:
  Each message is a 4-byte little-endian length header followed by a
  JSON UTF-8 payload. Both directions. Chrome enforces this.

Installation:
  See `tools/phantominput-extension/install-host.sh` for a one-shot
  script that writes the host manifest to the correct directory.
"""

from __future__ import annotations

import json
import struct
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

HTTP_PORT = 9877
_msg_id = 0
_pending: dict[int, Any] = {}
_pending_lock = threading.Lock()


def _next_id() -> int:
    global _msg_id  # noqa: PLW0603
    _msg_id += 1
    return _msg_id


# ---------------------------------------------------------------------------
# Native messaging stdio loop
# ---------------------------------------------------------------------------


def _send_to_extension(payload: dict[str, Any]) -> None:
    """Frame and write a message to Chrome over stdout."""
    encoded = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def _read_from_extension() -> dict[str, Any] | None:
    """Read one framed message from stdin. Returns None on EOF."""
    raw_len = sys.stdin.buffer.read(4)
    if not raw_len or len(raw_len) < 4:
        return None
    msg_len = struct.unpack("<I", raw_len)[0]
    raw = sys.stdin.buffer.read(msg_len)
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def _stdio_reader() -> None:
    """Read responses from the extension and route to pending requests."""
    while True:
        msg = _read_from_extension()
        if msg is None:
            return
        request_id = msg.get("requestId")
        if request_id is None:
            continue
        with _pending_lock:
            event = _pending.pop(request_id, None)
        if event is not None:
            event["response"] = msg.get("response")
            event["ready"].set()


def call_extension(op: str, **kwargs: Any) -> dict[str, Any]:
    """Send a request to the extension and wait for the matching response."""
    req_id = _next_id()
    event = {"ready": threading.Event(), "response": None}
    with _pending_lock:
        _pending[req_id] = event
    payload = {"requestId": req_id, "op": op, **kwargs}
    _send_to_extension(payload)
    # Cap wait at 30s — most ops are sub-second.
    ok = event["ready"].wait(timeout=30.0)
    if not ok:
        with _pending_lock:
            _pending.pop(req_id, None)
        return {"ok": False, "error": "extension timeout"}
    return event["response"] or {"ok": False, "error": "no response"}


# ---------------------------------------------------------------------------
# Local HTTP surface
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "PhantomInputHost/0.1"

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("content-length", 0))
        if length <= 0 or length > 1_000_000:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return None

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("access-control-allow-origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._send(200, {"ok": True, "host_alive": True})
            return
        if self.path == "/status":
            self._send(200, call_extension("status"))
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        payload = self._read_json() or {}
        if self.path == "/attach":
            self._send(200, call_extension("attach", **payload))
            return
        if self.path == "/detach":
            self._send(200, call_extension("detach", **payload))
            return
        if self.path == "/type":
            self._send(200, call_extension("type", **payload))
            return
        if self.path == "/run":
            self._send(200, call_extension("run", **payload))
            return
        if self.path == "/key":
            self._send(200, call_extension("key", **payload))
            return
        if self.path == "/click":
            self._send(200, call_extension("click", **payload))
            return
        if self.path == "/move":
            self._send(200, call_extension("move", **payload))
            return
        if self.path == "/screenshot":
            self._send(200, call_extension("screenshot", **payload))
            return
        self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):  # quiet
        pass


def main() -> int:
    # Start stdio reader in a daemon thread
    reader = threading.Thread(target=_stdio_reader, daemon=True, name="ext-reader")
    reader.start()

    # Run local HTTP server in main thread
    httpd = HTTPServer(("127.0.0.1", HTTP_PORT), Handler)
    # No prints — Chrome reads stdout. Anything we write that isn't
    # length-prefixed JSON will crash the extension. Use stderr for logs.
    print(f"phantominput host: http://127.0.0.1:{HTTP_PORT}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
