#!/usr/bin/env python3
"""GeForce NOW input bridge for macOS.

GeForce NOW's streaming client filters out synthesized DOM events
(``event.isTrusted == false``) so a Chrome extension cannot type into
the streamed Ubuntu desktop. **OS-level synthesized keystrokes ARE marked
trusted** because they enter Chrome through the native input pipeline.

This little HTTP server runs locally on the Mac, accepts requests like
``POST /type {"text":"..."}``, and uses AppleScript's ``System Events``
to inject the keystrokes into whichever app is frontmost. Make sure the
GeForce NOW browser tab is the frontmost window when you send input.

Requirements
------------
* Python 3 (any version that ships with macOS works).
* Grant Accessibility permission to Terminal.app (or whatever shell you
  start this from) the first time you run it: System Settings →
  Privacy & Security → Accessibility → +Terminal. macOS will prompt.

Usage
-----
    python3 tools/gfn_input_bridge.py
    # Then from another shell on the same Mac:
    curl -s -X POST http://127.0.0.1:9876/type \\
        -H 'content-type: application/json' \\
        -d '{"text":"echo hello\\n"}'
    curl -s -X POST http://127.0.0.1:9876/key \\
        -H 'content-type: application/json' \\
        -d '{"key":"Return"}'

The server binds to ``127.0.0.1`` only — never exposed to the network.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT_DEFAULT = 9876
ALLOWED_FRONTMOST_APPS = {"Google Chrome", "Chromium", "Safari", "Arc", "Brave Browser"}

# AppleScript key-code map for non-printable keys.
APPLESCRIPT_KEY_CODES = {
    "Return": 36,
    "Enter": 36,
    "Tab": 48,
    "Space": 49,
    "Backspace": 51,
    "Delete": 117,
    "Escape": 53,
    "Up": 126,
    "Down": 125,
    "Left": 123,
    "Right": 124,
    "Home": 115,
    "End": 119,
    "PageUp": 116,
    "PageDown": 121,
    "F1": 122, "F2": 120, "F3": 99, "F4": 118, "F5": 96,
    "F6": 97, "F7": 98, "F8": 100, "F9": 101, "F10": 109,
    "F11": 103, "F12": 111,
}


def _frontmost_app_name() -> str:
    """Return the name of whichever app is frontmost. Empty on error."""
    try:
        out = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first process whose frontmost is true'],
            capture_output=True, text=True, timeout=2,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def _osascript(script: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False, result.stderr.strip() or "osascript failed"
        return True, result.stdout.strip()
    except Exception as exc:
        return False, str(exc)


def _escape_applescript_string(text: str) -> str:
    """Escape a Python string so it's safe inside an AppleScript string literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def type_text(
    text: str,
    allow_outside_browser: bool = False,
    activate: str | None = None,
) -> tuple[bool, str]:
    """Type ``text`` into the frontmost window via System Events.

    ``activate``: if set to an app name (e.g. ``"Google Chrome"``), bring
    that app to the front before typing. Eliminates the race where a
    remote caller (Claude Code, a CI runner) accidentally re-focuses
    itself in between the safety check and the keystrokes.
    """
    if activate:
        _osascript(f'tell application "{activate}" to activate')
        # Tiny settle so the OS finishes the focus switch before keystrokes fly.
        time.sleep(0.15)

    if not allow_outside_browser:
        app = _frontmost_app_name()
        if app and app not in ALLOWED_FRONTMOST_APPS:
            return False, (
                f"safety abort: frontmost app is {app!r}, not in allowed set "
                f"{sorted(ALLOWED_FRONTMOST_APPS)}. Bring the GeForce NOW "
                "browser window forward, or pass allow_outside_browser=true."
            )

    # Split on newlines so we can inject Return via key-code rather than
    # relying on AppleScript to interpret \n inside a keystroke string.
    segments = text.split("\n")
    lines = ['tell application "System Events"']
    for i, segment in enumerate(segments):
        if segment:
            escaped = _escape_applescript_string(segment)
            lines.append(f'    keystroke "{escaped}"')
            lines.append('    delay 0.02')
        if i < len(segments) - 1:
            lines.append("    key code 36")
            lines.append('    delay 0.05')
    lines.append("end tell")
    return _osascript("\n".join(lines))


def press_key(
    key: str,
    modifiers: list[str] | None = None,
    allow_outside_browser: bool = False,
    activate: str | None = None,
) -> tuple[bool, str]:
    """Press a single named key, optionally with modifiers (command/shift/control/option)."""
    if activate:
        _osascript(f'tell application "{activate}" to activate')
        time.sleep(0.15)

    if not allow_outside_browser:
        app = _frontmost_app_name()
        if app and app not in ALLOWED_FRONTMOST_APPS:
            return False, f"safety abort: frontmost app is {app!r}"

    keycode = APPLESCRIPT_KEY_CODES.get(key)
    if keycode is None:
        # Single-character "key" — just type it
        if len(key) == 1:
            return type_text(key, allow_outside_browser=allow_outside_browser)
        return False, f"unknown key: {key!r} (and not a single character)"

    mods = modifiers or []
    mod_str = ""
    if mods:
        cleaned = []
        for m in mods:
            m_low = m.lower()
            if m_low in {"cmd", "command", "meta"}: cleaned.append("command down")
            elif m_low in {"ctrl", "control"}: cleaned.append("control down")
            elif m_low in {"shift"}: cleaned.append("shift down")
            elif m_low in {"alt", "opt", "option"}: cleaned.append("option down")
        if cleaned:
            mod_str = f' using {{{", ".join(cleaned)}}}'

    script = (
        'tell application "System Events"\n'
        f'    key code {keycode}{mod_str}\n'
        'end tell'
    )
    return _osascript(script)


def mouse_move(dx: int, dy: int, activate: str | None = None) -> tuple[bool, str]:
    """Move the mouse cursor by ``(dx, dy)`` pixels via Cliclick-style AppleScript.

    Used for two things: the keepalive jiggle (1px nudge that GFN's idle
    detector counts as activity), and clicking when we know coordinates
    relative to the current cursor position.
    """
    if activate:
        _osascript(f'tell application "{activate}" to activate')
        time.sleep(0.05)
    # AppleScript "do shell script" with cliclick would be cleaner, but
    # cliclick isn't bundled with macOS. Use Python's Quartz instead via
    # a one-line osascript that calls do shell script with python -c. This
    # adds ~50ms but keeps zero-dependency.
    script = f'''
do shell script "/usr/bin/python3 -c \\"
from Quartz import CGEventCreateMouseEvent, CGEventPost, CGEventGetLocation, CGEventCreate, kCGHIDEventTap, kCGMouseButtonLeft, kCGEventMouseMoved
loc = CGEventGetLocation(CGEventCreate(None))
CGEventPost(kCGHIDEventTap, CGEventCreateMouseEvent(None, kCGEventMouseMoved, (loc.x + {dx}, loc.y + {dy}), kCGMouseButtonLeft))
\\""
'''
    return _osascript(script)


def click_at(x: int, y: int, button: str = "left",
             activate: str | None = None) -> tuple[bool, str]:
    """Click at absolute screen coordinates ``(x, y)``."""
    if activate:
        _osascript(f'tell application "{activate}" to activate')
        time.sleep(0.1)
    btn_const = "kCGMouseButtonLeft" if button == "left" else "kCGMouseButtonRight"
    down = "kCGEventLeftMouseDown" if button == "left" else "kCGEventRightMouseDown"
    up = "kCGEventLeftMouseUp" if button == "left" else "kCGEventRightMouseUp"
    script = f'''
do shell script "/usr/bin/python3 -c \\"
from Quartz import CGEventCreateMouseEvent, CGEventPost, kCGHIDEventTap, {btn_const}, {down}, {up}
pos = ({x}, {y})
CGEventPost(kCGHIDEventTap, CGEventCreateMouseEvent(None, {down}, pos, {btn_const}))
CGEventPost(kCGHIDEventTap, CGEventCreateMouseEvent(None, {up}, pos, {btn_const}))
\\""
'''
    return _osascript(script)


# ---------------------------------------------------------------------------
# Keepalive thread
# ---------------------------------------------------------------------------
#
# GFN ends a session after ~30 seconds with no trusted input. The bridge
# can nudge the mouse one pixel back and forth every N seconds while we're
# between commands so the session stays alive. The jiggle is invisible to
# the user but counts as trusted activity from GFN's perspective.

_keepalive_thread: threading.Thread | None = None
_keepalive_stop = threading.Event()
_keepalive_interval = 20.0
_keepalive_target: str | None = None
_keepalive_log: list[dict] = []


def _keepalive_loop() -> None:
    """Background tick: 1px right then 1px left every `_keepalive_interval` s.

    Smart-skip: if the target app (Chrome / browser) is not currently
    frontmost, we don't steal focus — the keepalive just records a skip
    and the user can decide whether to bring Chrome back forward. This
    means if the user is mid-task in another app, we don't yank them away
    every 20 seconds.
    """
    while not _keepalive_stop.is_set():
        # Wait first, so the very first call to /keepalive/start doesn't
        # kick the mouse (in case the user is interacting right then).
        if _keepalive_stop.wait(_keepalive_interval):
            return
        try:
            target = _keepalive_target
            frontmost = _frontmost_app_name()
            # Only jiggle when our target (or any allowed browser) is
            # already frontmost. We never force-activate to avoid
            # interrupting work in other apps.
            should_jiggle = (
                (target and frontmost == target)
                or (not target and frontmost in ALLOWED_FRONTMOST_APPS)
            )
            if not should_jiggle:
                _keepalive_log.append({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "ok": True, "skipped": True, "frontmost": frontmost,
                })
            else:
                mouse_move(1, 0)
                time.sleep(0.05)
                mouse_move(-1, 0)
                _keepalive_log.append({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "ok": True, "skipped": False, "frontmost": frontmost,
                })
        except Exception as exc:
            _keepalive_log.append({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "ok": False, "error": str(exc),
            })
        # Cap the log so it doesn't grow forever.
        if len(_keepalive_log) > 200:
            del _keepalive_log[:-200]


def keepalive_start(interval: float, target: str | None) -> tuple[bool, str]:
    """Spin up the keepalive thread if it's not already running."""
    global _keepalive_thread, _keepalive_interval, _keepalive_target  # noqa: PLW0603
    if _keepalive_thread is not None and _keepalive_thread.is_alive():
        return True, f"already running every {_keepalive_interval}s"
    _keepalive_interval = max(2.0, float(interval))
    _keepalive_target = target
    _keepalive_stop.clear()
    _keepalive_thread = threading.Thread(
        target=_keepalive_loop, daemon=True, name="gfn-keepalive",
    )
    _keepalive_thread.start()
    return True, f"started: every {_keepalive_interval}s, target={target!r}"


def keepalive_stop() -> tuple[bool, str]:
    global _keepalive_thread  # noqa: PLW0603
    if _keepalive_thread is None or not _keepalive_thread.is_alive():
        return True, "not running"
    _keepalive_stop.set()
    _keepalive_thread.join(timeout=3.0)
    _keepalive_thread = None
    return True, "stopped"


class Handler(BaseHTTPRequestHandler):
    server_version = "GFNBridge/0.2"

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("content-length", 0))
        if length <= 0 or length > 100_000:
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
            self._send(200, {
                "ok": True,
                "frontmost": _frontmost_app_name(),
                "keepalive_running": (
                    _keepalive_thread is not None and _keepalive_thread.is_alive()
                ),
                "keepalive_interval": _keepalive_interval,
                "keepalive_recent": _keepalive_log[-3:],
            })
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        payload = self._read_json()
        if payload is None:
            self._send(400, {"error": "invalid json"})
            return

        allow_outside = bool(payload.get("allow_outside_browser", False))
        activate = payload.get("activate")
        if activate is not None and not isinstance(activate, str):
            self._send(400, {"error": "'activate' must be a string app name"})
            return

        if self.path == "/type":
            text = payload.get("text")
            if not isinstance(text, str):
                self._send(400, {"error": "missing 'text' string"})
                return
            ok, detail = type_text(
                text, allow_outside_browser=allow_outside, activate=activate,
            )
            self._send(200 if ok else 409, {"ok": ok, "detail": detail, "chars": len(text)})
            return

        if self.path == "/key":
            key = payload.get("key")
            mods = payload.get("modifiers") or []
            if not isinstance(key, str) or not isinstance(mods, list):
                self._send(400, {"error": "missing 'key' string (and optional 'modifiers' list)"})
                return
            ok, detail = press_key(
                key, mods, allow_outside_browser=allow_outside, activate=activate,
            )
            self._send(200 if ok else 409, {"ok": ok, "detail": detail})
            return

        if self.path == "/run":
            # Convenience: type a command, then press Return. Optional
            # post-run sleep so the next call doesn't fire before the
            # remote terminal has caught up.
            cmd = payload.get("command")
            wait_after = float(payload.get("wait_after", 0.5))
            if not isinstance(cmd, str):
                self._send(400, {"error": "missing 'command' string"})
                return
            # Strip trailing newlines — we add the Return separately.
            cmd_stripped = cmd.rstrip("\r\n")
            ok, detail = type_text(
                cmd_stripped, allow_outside_browser=allow_outside, activate=activate,
            )
            if not ok:
                self._send(409, {"ok": False, "detail": detail, "stage": "type"})
                return
            ok, detail = press_key("Return", allow_outside_browser=allow_outside)
            if not ok:
                self._send(409, {"ok": False, "detail": detail, "stage": "return"})
                return
            if wait_after > 0:
                time.sleep(min(wait_after, 30.0))
            self._send(200, {"ok": True, "detail": f"ran {len(cmd_stripped)} chars + Return"})
            return

        if self.path == "/mouse_move":
            dx = int(payload.get("dx", 0))
            dy = int(payload.get("dy", 0))
            ok, detail = mouse_move(dx, dy, activate=activate)
            self._send(200 if ok else 409, {"ok": ok, "detail": detail})
            return

        if self.path == "/click":
            x = payload.get("x")
            y = payload.get("y")
            button = payload.get("button", "left")
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                self._send(400, {"error": "x and y (numbers) required"})
                return
            ok, detail = click_at(int(x), int(y), button=button, activate=activate)
            self._send(200 if ok else 409, {"ok": ok, "detail": detail})
            return

        if self.path == "/keepalive/start":
            interval = float(payload.get("interval", 20.0))
            target = payload.get("activate") or payload.get("target")
            ok, detail = keepalive_start(interval, target)
            self._send(200 if ok else 409, {"ok": ok, "detail": detail})
            return

        if self.path == "/keepalive/stop":
            ok, detail = keepalive_stop()
            self._send(200, {"ok": ok, "detail": detail})
            return

        self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        # Quiet by default; uncomment if debugging.
        # print(self.address_string() + " - " + (fmt % args))
        pass


def main():
    parser = argparse.ArgumentParser(description="GeForce NOW input bridge for macOS.")
    parser.add_argument("--port", type=int, default=PORT_DEFAULT,
                        help=f"localhost port to bind (default: {PORT_DEFAULT})")
    args = parser.parse_args()

    if shutil.which("osascript") is None:
        print("ERROR: osascript not found. This bridge requires macOS.")
        return 1

    httpd = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"GFN input bridge listening on http://127.0.0.1:{args.port}")
    print("Endpoints:")
    print(f"  GET  /health                                  — readiness check")
    print(f"  POST /type   {{\"text\":\"echo hello\\n\"}}      — type a string")
    print(f"  POST /key    {{\"key\":\"Return\"}}              — press a single key")
    print(f"            optional: \"modifiers\":[\"shift\",\"command\"]")
    print("\nSafety: requests are refused unless the frontmost app is one of "
          f"{sorted(ALLOWED_FRONTMOST_APPS)}.")
    print("Override per-request with \"allow_outside_browser\": true if you really mean it.\n")
    print("Bring your GFN tab forward in Chrome, then send a request to test:")
    print("  curl -s -X POST http://127.0.0.1:9876/type -H 'content-type: application/json' "
          "-d '{\"text\":\"echo hello from claude\\n\"}'\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
