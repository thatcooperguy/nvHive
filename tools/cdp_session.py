#!/usr/bin/env python3
"""CDP-over-debug-port backend for Operator.

Connects to Chrome's built-in remote debugging port and dispatches input
events via the Chrome DevTools Protocol. Trusted input (`isTrusted=true`)
without an installed extension.

Two ways to get a Chrome with a debug port:

  # 1. Headless on a CI runner (Linux, no Accessibility, no user session)
  chrome --headless=new --remote-debugging-port=9222 --disable-gpu \\
         --user-data-dir=/tmp/chrome-ci

  # 2. Visible on a developer Mac (just restart Chrome with the flag)
  /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \\
      --remote-debugging-port=9222 \\
      --user-data-dir=/tmp/chrome-debug

Then point the wrapper at it:

  from tools.cdp_session import CDPSession
  s = CDPSession(debug_url="http://127.0.0.1:9222")
  s.attach_to_first_tab_matching("geforcenow.com")
  s.run("nvh --version")
  s.screenshot()  # base64 PNG

This module has zero non-stdlib deps so it runs in a vanilla CI Python.
For local dev install ``websocket-client`` if you want streaming
events; this MVP uses raw sockets.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
import urllib.request
from typing import Any

# ---------------------------------------------------------------------------
# Minimal WebSocket client (RFC 6455) — saves a dep on websocket-client.
# Implements just enough to talk to CDP: text frames + ping/pong + close.
# ---------------------------------------------------------------------------


class _MiniWS:
    def __init__(self, url: str) -> None:
        # url like ws://host:port/devtools/page/<id>
        if not url.startswith("ws://"):
            raise ValueError("only ws:// supported (Chrome local debug)")
        rest = url[len("ws://"):]
        host_port, _, path = rest.partition("/")
        host, _, port = host_port.partition(":")
        port_n = int(port or "80")
        self._sock = socket.create_connection((host, port_n), timeout=10.0)
        # Handshake
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {host}:{port_n}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._sock.sendall(req.encode("ascii"))
        # Read response headers
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("ws handshake EOF")
            buf += chunk
        head, _, leftover = buf.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"ws handshake refused: {head[:200]!r}")
        # Stash any frame bytes that came in after headers
        self._rxbuf = leftover

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        # FIN=1, opcode=0x1 (text)
        header = bytearray([0x81])
        # Mask bit set (client→server requires masking), then length
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytearray(payload)
        for i in range(len(masked)):
            masked[i] ^= mask[i % 4]
        self._sock.sendall(bytes(header) + bytes(masked))

    def _recv_exact(self, n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            if self._rxbuf:
                take = min(n - len(out), len(self._rxbuf))
                out += self._rxbuf[:take]
                self._rxbuf = self._rxbuf[take:]
                continue
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("ws recv EOF")
            self._rxbuf += chunk
        return bytes(out)

    def recv_text(self, timeout: float = 30.0) -> str:
        self._sock.settimeout(timeout)
        # Frame header
        b1 = self._recv_exact(1)[0]
        b2 = self._recv_exact(1)[0]
        # FIN bit assumed; opcode 0x1 text, 0x9 ping, 0xA pong, 0x8 close
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = bytearray(self._recv_exact(length))
        if masked:
            for i in range(len(payload)):
                payload[i] ^= mask[i % 4]
        if opcode == 0x9:  # ping → pong
            self._send_pong(bytes(payload))
            return self.recv_text(timeout=timeout)
        if opcode == 0x8:  # close
            raise ConnectionError("ws closed by peer")
        if opcode != 0x1:
            return self.recv_text(timeout=timeout)
        return bytes(payload).decode("utf-8", errors="replace")

    def _send_pong(self, data: bytes) -> None:
        header = bytearray([0x8A, 0x80 | len(data)])
        mask = os.urandom(4)
        header += mask
        masked = bytearray(data)
        for i in range(len(masked)):
            masked[i] ^= mask[i % 4]
        self._sock.sendall(bytes(header) + bytes(masked))

    def close(self) -> None:
        try:
            self._sock.sendall(bytes([0x88, 0x80]) + os.urandom(4))
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CDP session
# ---------------------------------------------------------------------------


class CDPSession:
    """Drive a Chrome tab via the DevTools Protocol over WebSocket."""

    def __init__(self, debug_url: str = "http://127.0.0.1:9222") -> None:
        self.debug_url = debug_url.rstrip("/")
        self._ws: _MiniWS | None = None
        self._next_id = 0

    # -- tab discovery -----------------------------------------------------

    def list_tabs(self) -> list[dict[str, Any]]:
        with urllib.request.urlopen(f"{self.debug_url}/json/list", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def attach_to_first_tab_matching(self, url_contains: str) -> dict[str, Any]:
        """Connect the WS to the first tab whose URL contains the substring."""
        tabs = self.list_tabs()
        match = next(
            (t for t in tabs if url_contains in (t.get("url") or "") and t.get("type") == "page"),
            None,
        )
        if not match:
            raise RuntimeError(
                f"no tab found containing {url_contains!r}. "
                f"Open URLs: {[t.get('url') for t in tabs][:5]}"
            )
        ws_url = match["webSocketDebuggerUrl"]
        self._ws = _MiniWS(ws_url)
        return match

    # -- CDP RPC -----------------------------------------------------------

    def _call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._ws is None:
            raise RuntimeError("attach to a tab first")
        self._next_id += 1
        rid = self._next_id
        self._ws.send_text(json.dumps({"id": rid, "method": method, "params": params or {}}))
        # Drain events until our id arrives.
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            text = self._ws.recv_text(timeout=max(0.5, deadline - time.monotonic()))
            msg = json.loads(text)
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"CDP error on {method}: {msg['error']}")
                return msg.get("result") or {}
            # event — ignore for now (could surface via callbacks later)
        raise TimeoutError(f"no response to CDP {method}")

    # -- high-level API ----------------------------------------------------

    def type(self, text: str) -> dict[str, Any]:  # noqa: A003
        for ch in text:
            if ch == "\n":
                self._call("Input.dispatchKeyEvent", {
                    "type": "keyDown", "key": "Enter", "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                })
                self._call("Input.dispatchKeyEvent", {
                    "type": "keyUp", "key": "Enter", "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                })
                continue
            self._call("Input.dispatchKeyEvent", {"type": "char", "text": ch})
        return {"ok": True, "chars": len(text)}

    def key(self, key: str) -> dict[str, Any]:
        vk = {"Enter": 13, "Tab": 9, "Escape": 27, "Backspace": 8}.get(key)
        opts = {"type": "keyDown", "key": key, "code": key}
        if vk:
            opts["windowsVirtualKeyCode"] = vk
        self._call("Input.dispatchKeyEvent", opts)
        self._call("Input.dispatchKeyEvent", {**opts, "type": "keyUp"})
        return {"ok": True}

    def run(self, command: str, wait_after: float = 0.5) -> dict[str, Any]:
        cmd = command.rstrip("\r\n")
        self.type(cmd)
        self.key("Enter")
        if wait_after > 0:
            time.sleep(min(wait_after, 30.0))
        return {"ok": True, "chars": len(cmd)}

    def click(self, x: int, y: int, button: str = "left") -> dict[str, Any]:
        self._call("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y, "button": button, "clickCount": 1,
        })
        self._call("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y, "button": button, "clickCount": 1,
        })
        return {"ok": True}

    def move(self, x: int, y: int) -> dict[str, Any]:
        self._call("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x, "y": y, "button": "none",
        })
        return {"ok": True}

    def screenshot(self) -> dict[str, Any]:
        r = self._call("Page.captureScreenshot", {"format": "png"})
        return {"ok": True, "data": r.get("data")}

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None


# ---------------------------------------------------------------------------
# CLI (for headless CI use without writing Python)
# ---------------------------------------------------------------------------


def _main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="CDP-over-debug-port operator CLI")
    parser.add_argument("--url", default=os.environ.get("CDP_URL", "http://127.0.0.1:9222"))
    parser.add_argument("--match", default="geforcenow.com",
                        help="substring of the tab URL to attach to")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tabs")
    p_run = sub.add_parser("run"); p_run.add_argument("command")
    p_type = sub.add_parser("type"); p_type.add_argument("text")
    p_key = sub.add_parser("key"); p_key.add_argument("key")
    sub.add_parser("screenshot")
    args = parser.parse_args()

    s = CDPSession(debug_url=args.url)
    if args.cmd == "tabs":
        for t in s.list_tabs():
            print(f"{t.get('id', '?')[:12]}  {t.get('type', '?'):8}  {t.get('url', '')[:80]}")
        return 0
    s.attach_to_first_tab_matching(args.match)
    try:
        if args.cmd == "run":
            print(json.dumps(s.run(args.command), indent=2))
        elif args.cmd == "type":
            print(json.dumps(s.type(args.text), indent=2))
        elif args.cmd == "key":
            print(json.dumps(s.key(args.key), indent=2))
        elif args.cmd == "screenshot":
            r = s.screenshot()
            print(f"screenshot bytes (base64, {len(r.get('data') or '')} chars)")
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
