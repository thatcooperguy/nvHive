#!/usr/bin/env python3
"""High-level GFN session controller.

Wraps the low-level transports (AppleScript bridge OR PhantomInput
Chrome extension over its native-messaging host) behind one ergonomic
Python API:

    sess = GFNSession()         # auto-detects which backend is up
    sess.keepalive()            # background mouse-jiggle every 20s
    sess.run("nvh --version")   # type + Enter into the focused terminal
    sess.click(100, 200)        # mouse click
    sess.screenshot()           # base64 PNG of the GFN tab

Backends, in preferred order:
  1. PhantomInput extension host  → http://127.0.0.1:9877
     CDP-dispatched input, isTrusted=true, cross-platform.
  2. AppleScript bridge           → http://127.0.0.1:9876
     macOS-only fallback. Requires Accessibility permission.

Pick a specific backend with ``GFNSession(backend="extension")`` or
``backend="bridge"`` if you want to force one.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from typing import Any

EXTENSION_URL = "http://127.0.0.1:9877"
BRIDGE_URL = "http://127.0.0.1:9876"


def _post(url: str, payload: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(url: str, timeout: float = 5.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _detect_backend() -> str | None:
    """Return 'extension' or 'bridge' or None depending on what's reachable."""
    for label, url in (("extension", EXTENSION_URL), ("bridge", BRIDGE_URL)):
        try:
            r = _get(f"{url}/health", timeout=1.5)
            if r.get("ok"):
                return label
        except Exception:
            continue
    return None


class GFNSession:
    """Backend-agnostic GFN session controller."""

    def __init__(self, backend: str | None = None) -> None:
        if backend is None:
            backend = _detect_backend()
            if backend is None:
                raise RuntimeError(
                    "no GFN input backend reachable. Start either:\n"
                    f"  - phantominput extension host on {EXTENSION_URL}\n"
                    f"  - applescript bridge on {BRIDGE_URL}\n"
                    "(see tools/phantominput-extension/README.md or tools/gfn_input_bridge.py)"
                )
        if backend not in {"extension", "bridge"}:
            raise ValueError(f"unknown backend {backend!r}")
        self.backend = backend
        self._url = EXTENSION_URL if backend == "extension" else BRIDGE_URL

    # ------------------------------------------------------------------
    # Health + introspection
    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return _get(f"{self._url}/health")

    def status(self) -> dict[str, Any]:
        if self.backend == "extension":
            return _get(f"{self._url}/status")
        return self.health()

    # ------------------------------------------------------------------
    # Input ops — same shape across backends
    # ------------------------------------------------------------------

    def run(self, command: str, wait_after: float = 0.5) -> dict[str, Any]:
        """Type a command line + Enter, return when the call completes."""
        payload = {"command": command, "wait_after": wait_after}
        if self.backend == "bridge":
            payload["activate"] = "Google Chrome"
        return _post(f"{self._url}/run", payload)

    def type(self, text: str) -> dict[str, Any]:  # noqa: A003 — clearer name
        payload = {"text": text}
        if self.backend == "bridge":
            payload["activate"] = "Google Chrome"
        return _post(f"{self._url}/type", payload)

    def key(self, key: str, modifiers: list[str] | int | None = None) -> dict[str, Any]:
        payload = {"key": key}
        if modifiers is not None:
            payload["modifiers"] = modifiers
        if self.backend == "bridge":
            payload["activate"] = "Google Chrome"
        return _post(f"{self._url}/key", payload)

    def click(self, x: int, y: int, button: str = "left") -> dict[str, Any]:
        payload = {"x": x, "y": y, "button": button}
        if self.backend == "bridge":
            payload["activate"] = "Google Chrome"
        return _post(f"{self._url}/click", payload)

    def move(self, x: int, y: int) -> dict[str, Any]:
        if self.backend == "extension":
            return _post(f"{self._url}/move", {"x": x, "y": y})
        # Bridge takes deltas, not absolute; translation isn't possible
        # without knowing current cursor. Caller should use absolute
        # click() instead.
        return {"ok": False, "error": "absolute move requires extension backend"}

    def screenshot(self) -> dict[str, Any]:
        """Return {"ok": True, "data": "<base64 PNG>"} on success."""
        if self.backend == "extension":
            return _post(f"{self._url}/screenshot")
        return {"ok": False, "error": "screenshot requires extension backend"}

    # ------------------------------------------------------------------
    # Keepalive (bridge-only; extension doesn't need it)
    # ------------------------------------------------------------------

    def keepalive(self, interval: float = 20.0) -> dict[str, Any]:
        """Start the bridge's background mouse-jiggle to keep GFN session alive.

        The extension backend doesn't need this — CDP attachments don't
        trigger GFN's idle timer the same way streamed input does.
        """
        if self.backend == "bridge":
            return _post(
                f"{self._url}/keepalive/start",
                {"interval": interval, "target": "Google Chrome"},
            )
        return {"ok": True, "detail": "extension backend: keepalive not required"}

    def keepalive_stop(self) -> dict[str, Any]:
        if self.backend == "bridge":
            return _post(f"{self._url}/keepalive/stop")
        return {"ok": True}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="High-level GFN session controller")
    parser.add_argument(
        "--backend", choices=("auto", "extension", "bridge"), default="auto",
        help="Which input backend to use. 'auto' picks whichever is reachable.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    sub.add_parser("status")
    p_run = sub.add_parser("run")
    p_run.add_argument("command")
    p_run.add_argument("--wait", type=float, default=0.5)
    p_type = sub.add_parser("type")
    p_type.add_argument("text")
    p_key = sub.add_parser("key")
    p_key.add_argument("key")
    p_click = sub.add_parser("click")
    p_click.add_argument("x", type=int)
    p_click.add_argument("y", type=int)
    p_click.add_argument("--button", default="left")
    p_ka = sub.add_parser("keepalive")
    p_ka.add_argument("--interval", type=float, default=20.0)
    sub.add_parser("keepalive-stop")
    sub.add_parser("screenshot")

    args = parser.parse_args()
    backend = None if args.backend == "auto" else args.backend
    try:
        sess = GFNSession(backend=backend)
    except RuntimeError as exc:
        print(str(exc))
        return 2

    if args.cmd == "health":
        _print(sess.health())
    elif args.cmd == "status":
        _print(sess.status())
    elif args.cmd == "run":
        _print(sess.run(args.command, wait_after=args.wait))
    elif args.cmd == "type":
        _print(sess.type(args.text))
    elif args.cmd == "key":
        _print(sess.key(args.key))
    elif args.cmd == "click":
        _print(sess.click(args.x, args.y, button=args.button))
    elif args.cmd == "keepalive":
        _print(sess.keepalive(interval=args.interval))
    elif args.cmd == "keepalive-stop":
        _print(sess.keepalive_stop())
    elif args.cmd == "screenshot":
        r = sess.screenshot()
        if r.get("ok") and r.get("data"):
            print(f"screenshot bytes (base64, {len(r['data'])} chars):")
            print(r["data"][:80] + "…")
        else:
            _print(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
