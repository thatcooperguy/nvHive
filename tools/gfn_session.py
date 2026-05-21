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
CDP_URL = "http://127.0.0.1:9222"
# Default substring used to find the right tab when the CDP backend is
# in play. The caller can override per-Session.
CDP_DEFAULT_TAB_MATCH = "geforcenow.com"


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
    """Return preferred backend label or None if nothing is reachable.

    Preference order: extension (fastest, cross-platform, no Chrome
    restart) → CDP-over-debug-port (no install but Chrome must be
    launched with the flag) → AppleScript bridge (macOS-only fallback).
    """
    # 1. extension host
    try:
        r = _get(f"{EXTENSION_URL}/health", timeout=1.0)
        if r.get("ok"):
            return "extension"
    except Exception:
        pass
    # 2. CDP-over-debug-port — check /json/version which Chrome always serves
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=1.0) as r:
            data = json.loads(r.read().decode("utf-8"))
            if "Browser" in data:
                return "cdp"
    except Exception:
        pass
    # 3. AppleScript bridge
    try:
        r = _get(f"{BRIDGE_URL}/health", timeout=1.0)
        if r.get("ok"):
            return "bridge"
    except Exception:
        pass
    return None


class GFNSession:
    """Backend-agnostic GFN session controller.

    Three backends, auto-detected in priority order:
      - "extension": PhantomInput Chrome extension + native host (best,
        works while user is in other apps, cross-platform)
      - "cdp":       Chrome relaunched with --remote-debugging-port=9222
                     (no extension install, ideal for headless CI)
      - "bridge":    macOS AppleScript bridge (fallback)
    """

    def __init__(
        self,
        backend: str | None = None,
        *,
        cdp_tab_match: str = CDP_DEFAULT_TAB_MATCH,
    ) -> None:
        if backend is None:
            backend = _detect_backend()
            if backend is None:
                raise RuntimeError(
                    "no input backend reachable. Start one of:\n"
                    f"  - phantominput extension + host  ({EXTENSION_URL})\n"
                    f"  - Chrome with --remote-debugging-port=9222  ({CDP_URL})\n"
                    f"  - applescript bridge  ({BRIDGE_URL})\n"
                    "See tools/phantominput-extension/README.md, tools/cdp_session.py,\n"
                    "or tools/gfn_input_bridge.py."
                )
        if backend not in {"extension", "cdp", "bridge"}:
            raise ValueError(f"unknown backend {backend!r}")
        self.backend = backend
        self._cdp_tab_match = cdp_tab_match
        self._cdp = None  # lazy
        if backend == "extension":
            self._url = EXTENSION_URL
        elif backend == "bridge":
            self._url = BRIDGE_URL
        else:
            self._url = CDP_URL

    def _ensure_cdp(self):
        """Lazy-construct the CDPSession for the cdp backend."""
        if self._cdp is None:
            from tools.cdp_session import CDPSession  # local import: optional dep path
            self._cdp = CDPSession(debug_url=self._url)
            self._cdp.attach_to_first_tab_matching(self._cdp_tab_match)
        return self._cdp

    # ------------------------------------------------------------------
    # Health + introspection
    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        if self.backend == "cdp":
            return {"ok": True, "backend": "cdp", "url": self._url}
        return _get(f"{self._url}/health")

    def status(self) -> dict[str, Any]:
        if self.backend == "extension":
            return _get(f"{self._url}/status")
        if self.backend == "cdp":
            return {"ok": True, "backend": "cdp", "tabs": self._ensure_cdp().list_tabs()}
        return self.health()

    # ------------------------------------------------------------------
    # Input ops — same shape across backends
    # ------------------------------------------------------------------

    def run(self, command: str, wait_after: float = 0.5) -> dict[str, Any]:
        """Type a command line + Enter, return when the call completes."""
        if self.backend == "cdp":
            return self._ensure_cdp().run(command, wait_after=wait_after)
        payload = {"command": command, "wait_after": wait_after}
        if self.backend == "bridge":
            payload["activate"] = "Google Chrome"
        return _post(f"{self._url}/run", payload)

    def type(self, text: str) -> dict[str, Any]:  # noqa: A003 — clearer name
        if self.backend == "cdp":
            return self._ensure_cdp().type(text)
        payload = {"text": text}
        if self.backend == "bridge":
            payload["activate"] = "Google Chrome"
        return _post(f"{self._url}/type", payload)

    def key(self, key: str, modifiers: list[str] | int | None = None) -> dict[str, Any]:
        if self.backend == "cdp":
            return self._ensure_cdp().key(key)
        payload = {"key": key}
        if modifiers is not None:
            payload["modifiers"] = modifiers
        if self.backend == "bridge":
            payload["activate"] = "Google Chrome"
        return _post(f"{self._url}/key", payload)

    def click(self, x: int, y: int, button: str = "left") -> dict[str, Any]:
        if self.backend == "cdp":
            return self._ensure_cdp().click(x, y, button=button)
        payload = {"x": x, "y": y, "button": button}
        if self.backend == "bridge":
            payload["activate"] = "Google Chrome"
        return _post(f"{self._url}/click", payload)

    def move(self, x: int, y: int) -> dict[str, Any]:
        if self.backend == "extension":
            return _post(f"{self._url}/move", {"x": x, "y": y})
        if self.backend == "cdp":
            return self._ensure_cdp().move(x, y)
        return {"ok": False, "error": "absolute move requires extension or cdp backend"}

    def screenshot(self) -> dict[str, Any]:
        """Return {"ok": True, "data": "<base64 PNG>"} on success."""
        if self.backend == "extension":
            return _post(f"{self._url}/screenshot")
        if self.backend == "cdp":
            return self._ensure_cdp().screenshot()
        return {"ok": False, "error": "screenshot requires extension or cdp backend"}

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
