"""Service-pipeline orchestration helpers and the ``nvh services`` command.

nvHive runs three local services that have a strict startup order:

1. **Ollama** (:11434) — the local LLM daemon. No dependencies.
2. **API** (``nvh serve``, default :8000) — depends on Ollama for engine init.
3. **WebUI** (Next.js, default :3000) — depends on the API for data.

Until 2026-05-21 the order was implicit, scattered across ``install.sh``,
``nvh webui``, and the user's muscle memory. This module centralizes the
contract:

  * Module-level health probes (importable + testable) — ``ollama_healthy``,
    ``api_healthy``, ``webui_port_listening``.
  * Module-level stale-process kill helper — ``kill_stale_api`` (extracted
    from the inline closure that PR #65 added to ``webui()``).
  * Pipeline orchestration — ``service_status_table``, ``start_pipeline``,
    ``restart_pipeline``.
  * ``nvh services`` typer subapp — status / start / restart / stop.

See ``docs/SERVICE_ORDER.md`` for the dependency graph, the env knobs that
tune the gates, and how ``nvh services`` interacts with ``nvh webui``.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from typing import Any

# These constants describe the contract — keep them in lock-step with
# ``docs/SERVICE_ORDER.md`` (the release-hardening test enforces this).
OLLAMA_PORT = 11434
API_PORT_DEFAULT = 8000
WEBUI_PORT_DEFAULT = 3000

OLLAMA_HEALTH_PATH = "/api/tags"
API_HEALTH_PATH = "/v1/health"


def _env_int(name: str, default: int) -> int:
    """Read an int from the environment, falling back gracefully."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def ollama_boot_timeout() -> int:
    """How long to wait for Ollama to bind :11434.

    Matches the ``NVH_OLLAMA_BOOT_TIMEOUT`` knob honoured by
    ``install.sh``'s ``start_ollama_with_health_wait``.
    """
    return _env_int("NVH_OLLAMA_BOOT_TIMEOUT", 15)


def api_boot_timeout() -> int:
    """How long to wait for the API ``/v1/health`` to return engine-ready.

    Cold-import time for FastAPI + the nvHive providers on a fresh cloud
    VM is 10-15s; 20s gives a small safety margin without dragging out
    boot on a healthy rig. Overridable via ``NVH_API_BOOT_TIMEOUT``.
    """
    return _env_int("NVH_API_BOOT_TIMEOUT", 20)


def webui_boot_timeout() -> int:
    """How long to wait for the WebUI port to start listening.

    ``npm run start`` cold-boots Next.js in 5-15s; ``npm run dev`` can
    take 30s+ on a fresh checkout. 30s is the conservative default.
    """
    return _env_int("NVH_WEBUI_BOOT_TIMEOUT", 30)


# ---------------------------------------------------------------------------
# Low-level health probes — these are the *only* signals nvh services trusts.
# Each returns (is_healthy, human_reason) so callers can render status tables.
# ---------------------------------------------------------------------------


def port_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """Return True if something is accepting TCP connections on ``port``.

    Cheap; safe to call repeatedly. Used as the WebUI health probe (a
    Next.js production server's ``/`` is too expensive to GET — it
    streams the homepage) and as a fast pre-check before a real HTTP
    health probe.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ollama_healthy(
    port: int = OLLAMA_PORT, timeout: float = 2.0
) -> tuple[bool, str]:
    """Probe ``http://localhost:<port>/api/tags`` for an Ollama daemon.

    Healthy means: HTTP 200 with a JSON body containing a ``models`` list
    (the field is always present even when empty). Returns ``(False,
    reason)`` so the status table can show *why* a probe failed.
    """
    url = f"http://127.0.0.1:{port}{OLLAMA_HEALTH_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return False, f"HTTP {resp.status}"
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError) as exc:
        return False, f"unreachable ({exc})"
    except ValueError:
        return False, "non-JSON response"
    models = body.get("models")
    if not isinstance(models, list):
        return False, "missing models[]"
    return True, f"{len(models)} models"


def api_healthy(
    port: int = API_PORT_DEFAULT, timeout: float = 2.0
) -> tuple[bool, str]:
    """Probe ``http://localhost:<port>/v1/health`` for a healthy API server.

    Healthy means: HTTP 200, response envelope ``status: success``, AND
    ``data.engine_initialized: true``. This is the same invariant the
    WebUI's ``ApiHealthBanner`` uses (and the same one PR #65 introduced
    in ``nvh webui``'s closure — extracted to module level here so
    ``nvh services`` and the test suite can call it directly).
    """
    url = f"http://127.0.0.1:{port}{API_HEALTH_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return False, f"HTTP {resp.status}"
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError) as exc:
        return False, f"unreachable ({exc})"
    except ValueError:
        return False, "non-JSON response"
    if body.get("status") != "success":
        return False, f"status={body.get('status')!r}"
    data = body.get("data") or {}
    if not data.get("engine_initialized"):
        return False, "engine_not_initialized"
    return True, "engine_initialized"


def webui_port_listening(port: int = WEBUI_PORT_DEFAULT) -> tuple[bool, str]:
    """TCP-only probe for the WebUI port.

    A full HTTP GET on Next.js' ``/`` route would either hit the
    streaming homepage (slow) or 404 on a route that isn't yet built —
    neither is a good "is it up" signal. The port being LISTEN is the
    de-facto contract.
    """
    if port_listening(port):
        return True, "listening"
    return False, "no listener"


def wizard_smoke_test(
    api_port: int = API_PORT_DEFAULT, timeout: float = 45.0
) -> tuple[bool, str]:
    """Final end-to-end sanity check: can the Wizard actually answer?

    The hardening cycle has covered install / boot / probe / recover.
    The last "land on a broken state" failure mode is: Ollama up, API
    up, WebUI up — but no model loaded, OR the Wizard endpoint errors,
    OR no provider configured. The user lands on the WebUI thinking
    everything works, types "hi", gets a confusing error, and we lose
    them.

    This helper closes the loop by actually POSTing to /v1/wizard/chat
    and verifying a non-empty answer comes back. It's permissive on
    purpose:

      - HTTP 200 with non-empty answer  → (True, "ok: <mode>")
      - HTTP 200 with empty answer      → (False, "wizard returned empty answer")
      - Timeout / connection error      → (False, "<reason>")
      - HTTP 4xx/5xx                    → (False, "HTTP <code>")

    Callers can treat "ok: fallback" / "ok: deterministic" as degraded
    (Wizard works but no local LLM) and still proceed. Use ``timeout``
    generously: cold model load on CPU can run 30s+.
    """
    url = f"http://127.0.0.1:{api_port}/v1/wizard/chat"
    payload = json.dumps({"question": "hi", "history": []}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False, f"HTTP {resp.status}"
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"unreachable ({exc})"
    except ValueError:
        return False, "non-JSON response"
    # Response envelope: {status: "success", data: {answer, mode, ...}}
    data = body.get("data") or body  # accept either wrapped or flat
    answer = (data.get("answer") or "").strip()
    mode = data.get("mode") or "unknown"
    if not answer:
        return False, "wizard returned empty answer"
    return True, f"ok: {mode}"


# ---------------------------------------------------------------------------
# Stale-process recovery — extracted from PR #65's ``_kill_stale_api`` closure.
# ---------------------------------------------------------------------------


def kill_stale_api(port: int = API_PORT_DEFAULT) -> None:
    """Best-effort: free ``port`` by killing whatever's listening.

    Linux uses ``fuser -k`` (cleanest). macOS uses ``lsof -t`` + ``kill -TERM``
    (the canonical mac path since fuser doesn't ship there). Both are
    best-effort — if neither tool is available the new spawn just fails
    to bind and the user sees the error in the API log.

    Originally added inline in ``nvh webui`` by PR #65; promoted here so
    ``nvh services restart`` and the test suite can call it.
    """
    for cmd in (
        ["fuser", "-k", f"{port}/tcp"],
        ["pkill", "-f", f"nvh serve.*{port}"],
    ):
        try:
            subprocess.run(cmd, capture_output=True, timeout=5, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["lsof", "-nP", "-iTCP:" + str(port), "-sTCP:LISTEN", "-t"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            for pid in out.stdout.split():
                try:
                    subprocess.run(
                        ["kill", "-TERM", pid], capture_output=True, timeout=2
                    )
                except subprocess.TimeoutExpired:
                    pass
        except FileNotFoundError:
            pass
    time.sleep(0.5)


def kill_stale_port(port: int) -> None:
    """Generic version of ``kill_stale_api`` for any port.

    Used by ``nvh services restart`` to take down Ollama and the WebUI
    along with the API. Mirrors the same Linux/macOS branches.
    """
    for cmd in (["fuser", "-k", f"{port}/tcp"],):
        try:
            subprocess.run(cmd, capture_output=True, timeout=5, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["lsof", "-nP", "-iTCP:" + str(port), "-sTCP:LISTEN", "-t"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            for pid in out.stdout.split():
                try:
                    subprocess.run(
                        ["kill", "-TERM", pid], capture_output=True, timeout=2
                    )
                except subprocess.TimeoutExpired:
                    pass
        except FileNotFoundError:
            pass
    time.sleep(0.5)


# ---------------------------------------------------------------------------
# Aggregate status — what ``nvh services`` (and `nvh services status`) prints.
# ---------------------------------------------------------------------------


@dataclass
class ServiceState:
    """One row in the ``nvh services`` status table."""

    name: str
    port: int
    running: bool
    healthy: bool
    detail: str

    @property
    def status_label(self) -> str:
        if not self.running:
            return "not running"
        if not self.healthy:
            return "unhealthy"
        return "running"

    @property
    def suggested_action(self) -> str:
        if not self.running:
            return "start"
        if not self.healthy:
            return "restart"
        return "leave"


@dataclass
class ServicesSnapshot:
    """All three services at one moment in time."""

    ollama: ServiceState
    api: ServiceState
    webui: ServiceState

    def as_list(self) -> list[ServiceState]:
        return [self.ollama, self.api, self.webui]

    def all_healthy(self) -> bool:
        return all(s.healthy for s in self.as_list())


def snapshot(
    api_port: int = API_PORT_DEFAULT,
    webui_port: int = WEBUI_PORT_DEFAULT,
) -> ServicesSnapshot:
    """Probe all three services concurrently-cheap and return a snapshot."""
    ollama_running = port_listening(OLLAMA_PORT)
    if ollama_running:
        ollama_ok, ollama_reason = ollama_healthy(OLLAMA_PORT)
        ollama_detail = (
            f"{OLLAMA_HEALTH_PATH} 200 ({ollama_reason})"
            if ollama_ok
            else f"{OLLAMA_HEALTH_PATH} {ollama_reason}"
        )
    else:
        ollama_ok = False
        ollama_detail = "no listener on 11434"

    api_running = port_listening(api_port)
    if api_running:
        api_ok, api_reason = api_healthy(api_port)
        api_detail = (
            f"{API_HEALTH_PATH} 200 ({api_reason})"
            if api_ok
            else f"{API_HEALTH_PATH} {api_reason}"
        )
    else:
        api_ok = False
        api_detail = f"no listener on {api_port}"

    webui_running = port_listening(webui_port)
    webui_ok = webui_running
    webui_detail = "listening" if webui_running else f"no listener on {webui_port}"

    return ServicesSnapshot(
        ollama=ServiceState(
            name="Ollama",
            port=OLLAMA_PORT,
            running=ollama_running,
            healthy=ollama_ok,
            detail=ollama_detail,
        ),
        api=ServiceState(
            name="API",
            port=api_port,
            running=api_running,
            healthy=api_ok,
            detail=api_detail,
        ),
        webui=ServiceState(
            name="WebUI",
            port=webui_port,
            running=webui_running,
            healthy=webui_ok,
            detail=webui_detail,
        ),
    )


# ---------------------------------------------------------------------------
# Pipeline boot — start Ollama, then API, then WebUI, with health gates.
# Each step writes to ``nvh services`` log and uses the same env knobs as
# install.sh so the user only has to learn one set of timeouts.
# ---------------------------------------------------------------------------


@dataclass
class StartResult:
    """Outcome of ``start_pipeline`` — used by tests + the CLI command."""

    started: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: str | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.failed is None


def _wait_for(
    probe: Any,
    timeout_s: int,
    poll_every: float = 0.5,
) -> tuple[bool, str]:
    """Generic polling loop: call ``probe()`` until it returns ``(True, _)``."""
    deadline = time.monotonic() + timeout_s
    last_reason = "timed out"
    while time.monotonic() < deadline:
        ok, reason = probe()
        if ok:
            return True, reason
        last_reason = reason
        time.sleep(poll_every)
    return False, last_reason


def _ollama_binary() -> str | None:
    """Locate the ollama binary, preferring the rootless install location."""
    explicit = os.environ.get("NVH_OLLAMA_BIN")
    if explicit and os.access(explicit, os.X_OK):
        return explicit
    return shutil.which("ollama")


def _nvh_binary() -> list[str]:
    """Return the argv prefix that invokes the current nvh install."""
    nvh_exe = shutil.which("nvh") or sys.executable
    if nvh_exe.endswith("python.exe") or nvh_exe.endswith("python"):
        return [nvh_exe, "-m", "nvh.cli.main"]
    return [nvh_exe]


def start_ollama(log_dir: str | None = None) -> tuple[bool, str]:
    """Start the Ollama daemon and wait for it to bind :11434.

    Mirrors ``install.sh``'s ``start_ollama_with_health_wait``: a real
    health gate (poll ``/api/tags``), not a fixed sleep. Returns
    ``(True, reason)`` on success, ``(False, reason)`` on failure.
    """
    ok, reason = ollama_healthy(OLLAMA_PORT, timeout=1.0)
    if ok:
        return True, f"already running ({reason})"

    ollama_bin = _ollama_binary()
    if not ollama_bin:
        return False, "ollama binary not found (run install.sh first)"

    log_path = None
    log_handle = None
    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, "ollama.log")
            log_handle = open(log_path, "a", buffering=1, encoding="utf-8")
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            log_handle.write(f"\n--- nvh services start_ollama at {stamp} ---\n")
        except OSError:
            log_handle = None

    try:
        subprocess.Popen(
            [ollama_bin, "serve"],
            stdout=log_handle or subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log_handle else subprocess.DEVNULL,
            env=os.environ.copy(),
            start_new_session=True,
        )
    except OSError as exc:
        return False, f"failed to spawn ollama: {exc}"

    timeout = ollama_boot_timeout()
    ok, reason = _wait_for(lambda: ollama_healthy(OLLAMA_PORT), timeout)
    if ok:
        return True, f"healthy after wait ({reason})"
    return False, f"did not bind 11434 in {timeout}s ({reason})"


def start_api(
    port: int = API_PORT_DEFAULT, log_dir: str | None = None
) -> tuple[bool, str]:
    """Start the API server (``nvh serve``) and wait for healthy.

    If something stale is already on the port we kill it first (PR #65's
    behavior, just at module level now). Returns when ``/v1/health``
    reports ``engine_initialized: true``.
    """
    running = port_listening(port)
    if running:
        ok, reason = api_healthy(port, timeout=2.0)
        if ok:
            return True, f"already running ({reason})"
        # Stale broken instance — exactly the failure mode PR #65 fixed.
        kill_stale_api(port)
        for _ in range(10):
            if not port_listening(port):
                break
            time.sleep(0.3)

    log_path = None
    log_handle = None
    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, "api-server.log")
            log_handle = open(log_path, "a", buffering=1, encoding="utf-8")
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            log_handle.write(
                f"\n--- nvh services start_api at {stamp} ---\n"
            )
        except OSError:
            log_handle = None

    cmd = [*_nvh_binary(), "serve", "--port", str(port)]
    try:
        subprocess.Popen(
            cmd,
            stdout=log_handle or subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log_handle else subprocess.DEVNULL,
            env=os.environ.copy(),
            start_new_session=True,
        )
    except OSError as exc:
        return False, f"failed to spawn nvh serve: {exc}"

    timeout = api_boot_timeout()
    ok, reason = _wait_for(lambda: api_healthy(port), timeout)
    if ok:
        return True, f"healthy after wait ({reason})"
    return False, f"did not become healthy in {timeout}s ({reason})"


def start_webui(
    port: int = WEBUI_PORT_DEFAULT, log_dir: str | None = None
) -> tuple[bool, str]:
    """Start the WebUI via ``nvh webui --no-api`` and wait for the port.

    ``nvh services start`` already started + health-gated the API in the
    previous step, so we pass ``--no-api`` to keep the WebUI command from
    re-doing that work (and from auto-killing the API we just brought up
    if its health probe races). Returns when the WebUI port is listening.
    """
    if port_listening(port):
        return True, "already listening"

    log_path = None
    log_handle = None
    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, "webui.log")
            log_handle = open(log_path, "a", buffering=1, encoding="utf-8")
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            log_handle.write(
                f"\n--- nvh services start_webui at {stamp} ---\n"
            )
        except OSError:
            log_handle = None

    cmd = [
        *_nvh_binary(),
        "webui",
        "--port",
        str(port),
        "--no-api",
        "--no-open",
    ]
    try:
        subprocess.Popen(
            cmd,
            stdout=log_handle or subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log_handle else subprocess.DEVNULL,
            env=os.environ.copy(),
            start_new_session=True,
        )
    except OSError as exc:
        return False, f"failed to spawn nvh webui: {exc}"

    timeout = webui_boot_timeout()
    ok, reason = _wait_for(
        lambda: webui_port_listening(port), timeout, poll_every=0.5
    )
    if ok:
        return True, f"listening after wait ({reason})"
    return False, f"did not bind {port} in {timeout}s ({reason})"


def start_pipeline(
    api_port: int = API_PORT_DEFAULT,
    webui_port: int = WEBUI_PORT_DEFAULT,
    log_dir: str | None = None,
    open_browser: bool = True,
    on_step: Any = None,
    on_step_begin: Any = None,
) -> StartResult:
    """Boot the full pipeline in order, aborting on the first hard failure.

    Order (also documented in ``docs/SERVICE_ORDER.md``):

        1. Ollama → wait for ``/api/tags``     (NVH_OLLAMA_BOOT_TIMEOUT)
        2. API    → wait for ``/v1/health``    (NVH_API_BOOT_TIMEOUT)
        3. WebUI  → wait for TCP LISTEN        (NVH_WEBUI_BOOT_TIMEOUT)

    If any step fails the rest are skipped and the result names the
    step + reason so ``nvh services start`` can render a clear error.

    ``on_step_begin(label)`` fires when a step starts (so a Rich Live
    table can flip the row to "starting"); ``on_step(label, ok, reason)``
    fires when the step finishes. The two callbacks let a TTY caller
    show a fluid status table instead of bursts of completed lines.

    Owner contract (2026-05-22): the browser only opens when ALL THREE
    services reported healthy. The pre-2026-05-22 behavior was to open
    the browser as part of ``nvh webui`` regardless of API engine state,
    which led to red "API offline" banners on the first page load on
    cold cloud rigs. Now the browser launch is gated on the pipeline
    actually reaching the end with ``failed is None``.
    """
    result = StartResult()
    if on_step is None:
        def on_step(label: str, ok: bool, reason: str) -> None:
            return None
    if on_step_begin is None:
        def on_step_begin(label: str) -> None:
            return None

    # 1. Ollama
    on_step_begin("Ollama")
    ok, reason = start_ollama(log_dir=log_dir)
    on_step("Ollama", ok, reason)
    if not ok:
        result.failed = "Ollama"
        result.reason = reason
        result.skipped = ["API", "WebUI"]
        return result
    result.started.append("Ollama")

    # 2. API
    on_step_begin("API")
    ok, reason = start_api(port=api_port, log_dir=log_dir)
    on_step("API", ok, reason)
    if not ok:
        result.failed = "API"
        result.reason = reason
        result.skipped = ["WebUI"]
        return result
    result.started.append("API")

    # 3. WebUI
    on_step_begin("WebUI")
    ok, reason = start_webui(port=webui_port, log_dir=log_dir)
    on_step("WebUI", ok, reason)
    if not ok:
        result.failed = "WebUI"
        result.reason = reason
        result.skipped = ["Smoke test"]
        return result
    result.started.append("WebUI")

    # 4. End-to-end Wizard smoke test — the load-bearing "everything
    # actually works" check. Three services listening on ports is not
    # the same as "the Wizard can answer the user." This step POSTs a
    # real chat request and verifies a non-empty answer comes back.
    # Permissive: a fallback-mode answer (no local LLM) still counts
    # as ok for the browser-open decision — the user has a working
    # Wizard, just not yet a local one. Only true endpoint failures or
    # timeouts gate the browser.
    on_step_begin("Smoke test")
    ok, reason = wizard_smoke_test(api_port=api_port)
    on_step("Smoke test", ok, reason)
    if not ok:
        result.failed = "Smoke test"
        result.reason = reason
        return result
    result.started.append("Smoke test")

    # Only NOW open the browser. The "good and working state" contract:
    # the user sees the WebUI for the first time when every service is
    # already verified healthy AND the Wizard can actually answer.
    if open_browser:
        url = f"http://localhost:{webui_port}/setup"
        try:
            webbrowser.open(url)
        except Exception:
            pass

    return result


def restart_pipeline(
    api_port: int = API_PORT_DEFAULT,
    webui_port: int = WEBUI_PORT_DEFAULT,
    log_dir: str | None = None,
    open_browser: bool = True,
    on_step: Any = None,
    on_step_begin: Any = None,
) -> StartResult:
    """Graceful kill of all three services, then a fresh ``start_pipeline``.

    Ollama is left alone unless explicitly listening on a stale port —
    we want to preserve the warmed-up model cache when possible.
    """
    # Kill in reverse dependency order so the API isn't briefly orphaned
    # serving requests for a WebUI we're about to take down anyway.
    if port_listening(webui_port):
        kill_stale_port(webui_port)
    if port_listening(api_port):
        kill_stale_api(api_port)
    # 1s settle between kill and start (matches the prompt contract).
    time.sleep(1.0)
    return start_pipeline(
        api_port=api_port,
        webui_port=webui_port,
        log_dir=log_dir,
        open_browser=open_browser,
        on_step=on_step,
        on_step_begin=on_step_begin,
    )


# ---------------------------------------------------------------------------
# Rendering — pretty status output. Used by both ``nvh services`` (default
# subcommand) and as the final beat of ``start`` / ``restart``.
# ---------------------------------------------------------------------------


def render_status_table(snap: ServicesSnapshot) -> str:
    """Return the markdown-style status table the prompt specifies."""
    rows = [
        ("Service", "Port", "Status", "Health", "Action"),
        ("------", "----", "------", "------", "------"),
    ]
    for s in snap.as_list():
        rows.append(
            (
                s.name,
                str(s.port),
                s.status_label,
                s.detail if s.detail else "-",
                s.suggested_action,
            )
        )
    widths = [
        max(len(row[i]) for row in rows) for i in range(len(rows[0]))
    ]
    lines = []
    for row in rows:
        line = "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))
        lines.append(line.rstrip())
    return "\n".join(lines)
