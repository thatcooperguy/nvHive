#!/usr/bin/env python3
"""Operator headless Chrome launcher — the fully hands-free QA harness.

Spawns a dedicated Chrome instance with everything PhantomInput needs:

  - ``--remote-debugging-port=<port>``  CDP available for trusted input
  - ``--user-data-dir=<isolated>``      separate profile (won't fight your
                                        normal Chrome; cookies persist
                                        across runs for GFN auto-login)
  - ``--load-extension=<path>``         PhantomInput extension auto-loads,
                                        no chrome://extensions clicks
  - GFN deep-link URL                   stream starts as soon as the
                                        ``Resume`` / ``Play`` button is hit

Why this exists:
  The OS-AppleScript path keeps failing because Chrome reports
  ``document.visibilityState === "hidden"`` for our streaming tab whenever
  another macOS window is in front. Chrome refuses to forward keyboard
  events to hidden tabs. CDP dispatches input straight to the renderer
  process and **bypasses visibility/focus entirely** — works headless,
  works while you're in another app, works on CI.

After this launches, drive the session through ``CDPSession`` (see
``tools/cdp_session.py``) — same API as everything else.

Usage:

    # First run (you log into GFN once; cookies persist):
    python3 tools/operator_chrome.py \\
        --gfn-url "https://play.geforcenow.com/games?game-id=21ca08a2-..."

    # Subsequent runs auto-pick up the cookies and go straight to play:
    python3 tools/operator_chrome.py --headless --gfn-url ...

    # Drive it:
    python3 tools/cdp_session.py --url http://127.0.0.1:9223 run "echo hi"

Or use ``GFNSession(backend="cdp")`` and point ``CDP_URL`` at 9223.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]


def _find_chrome() -> str:
    for p in CHROME_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    found = shutil.which("google-chrome") or shutil.which("chromium")
    if found:
        return found
    raise RuntimeError(
        "Chrome not found. Install Google Chrome or pass --chrome=PATH."
    )


def _port_ready(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def launch(
    *,
    chrome_path: str | None = None,
    debug_port: int = 9223,
    user_data_dir: str | Path | None = None,
    extension_path: str | Path | None = None,
    headless: bool = False,
    gfn_url: str | None = None,
    extra_args: list[str] | None = None,
    wait_seconds: float = 30.0,
) -> subprocess.Popen:
    """Launch the Chrome instance. Returns the Popen handle.

    The caller is responsible for killing the process when done. Cookies
    in ``user_data_dir`` persist across calls, so the second + Nth launch
    won't need user login for GFN.
    """
    chrome_path = chrome_path or _find_chrome()
    user_data_dir = Path(user_data_dir or Path.home() / ".operator-chrome")
    user_data_dir.mkdir(parents=True, exist_ok=True)
    extension_path = Path(extension_path or
        Path(__file__).resolve().parent / "phantominput-extension"
    )

    if _port_ready(debug_port):
        raise RuntimeError(
            f"port {debug_port} already busy. Either another Chrome is running "
            "with this debug port, or pick a different --debug-port."
        )

    cmd = [
        chrome_path,
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={user_data_dir}",
        f"--load-extension={extension_path}",
        # Quality-of-life: skip Chrome's first-run / what's-new dance.
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-default-apps",
        # Don't share state with the user's main Chrome (background sync etc.).
        "--disable-background-networking",
        # Allow the extension to attach chrome.debugger without warning the
        # user every session. (Still safe — only our extension is loaded
        # in this isolated profile.)
        "--silent-debugger-extension-api",
        # Tiny optimization: skip the password-manager prompt
        "--password-store=basic",
    ]
    if headless:
        # New Headless mode (Chrome 112+) — supports WebRTC, audio, GPU.
        cmd.append("--headless=new")
        cmd.append("--disable-gpu")  # software rendering for headless
        cmd.append("--window-size=1280,800")
    if extra_args:
        cmd.extend(extra_args)
    if gfn_url:
        cmd.append(gfn_url)

    # On macOS, launching the Chrome bundle binary directly (not via `open`)
    # avoids the "already running" check, so we don't conflict with the
    # user's normal Chrome.
    log_dir = user_data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    stdout_log = open(log_dir / f"chrome-{stamp}.log", "ab", buffering=0)
    stderr_log = stdout_log  # interleave

    proc = subprocess.Popen(
        cmd, stdout=stdout_log, stderr=stderr_log,
        env={**os.environ, "LANG": os.environ.get("LANG", "en_US.UTF-8")},
    )

    # Wait for debug port to be ready
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"Chrome exited early (code {proc.returncode}). "
                f"See {log_dir}/chrome-{stamp}.log"
            )
        if _port_ready(debug_port):
            break
        time.sleep(0.25)
    else:
        proc.terminate()
        raise TimeoutError(
            f"Chrome did not bind debug port {debug_port} within {wait_seconds}s"
        )

    # Confirm via the CDP /json/version endpoint
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{debug_port}/json/version", timeout=3
        ) as r:
            ver = r.read().decode("utf-8")
    except Exception as exc:
        proc.terminate()
        raise RuntimeError(f"debug port responded but /json/version failed: {exc}")

    print(f"Chrome launched: pid={proc.pid}", file=sys.stderr)
    print(f"  Debug port:    http://127.0.0.1:{debug_port}", file=sys.stderr)
    print(f"  Profile dir:   {user_data_dir}", file=sys.stderr)
    print(f"  Extension:     {extension_path}", file=sys.stderr)
    print(f"  Headless:      {headless}", file=sys.stderr)
    print(f"  Log:           {log_dir}/chrome-{stamp}.log", file=sys.stderr)
    print(f"  Version:       {ver[:80]}…", file=sys.stderr)
    return proc


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Launch a dedicated Chrome instance for Operator QA.",
    )
    parser.add_argument("--chrome", help="Path to the Chrome binary (auto-detected if omitted)")
    parser.add_argument("--debug-port", type=int, default=9223)
    parser.add_argument("--user-data-dir", default=str(Path.home() / ".operator-chrome"))
    parser.add_argument("--extension-path", default=str(
        Path(__file__).resolve().parent / "phantominput-extension",
    ))
    parser.add_argument("--headless", action="store_true",
                        help="Use Chrome's new headless mode (CI / no-display).")
    parser.add_argument("--gfn-url", help="If set, open this URL on launch.")
    parser.add_argument("--keep-running", action="store_true",
                        help="Block and keep Chrome alive until Ctrl-C.")
    args = parser.parse_args()

    proc = launch(
        chrome_path=args.chrome,
        debug_port=args.debug_port,
        user_data_dir=args.user_data_dir,
        extension_path=args.extension_path,
        headless=args.headless,
        gfn_url=args.gfn_url,
    )

    if args.keep_running:
        print("Press Ctrl-C to terminate the dedicated Chrome.", file=sys.stderr)
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
