"""Functional tests for ``nvh.cli.services`` — the module-level health
probes and pipeline orchestrator that back ``nvh services``.

These exist because PR #65's helpers used to live as closures inside
``webui()`` (no way to unit-test them). Now that they're module-level
we cover:

  * api_healthy — all the failure modes the prior TCP-only check missed.
  * ollama_healthy — happy path + the "200 but no models[]" coincidence.
  * webui_port_listening — TCP probe wrapper.
  * kill_stale_api — best-effort behavior on Linux + macOS (mocked).
  * start_pipeline / restart_pipeline — order + abort-on-failure.
"""

from __future__ import annotations

import json
import socket
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from nvh.cli import services

# ---------------------------------------------------------------------------
# Helpers: spin up a tiny stub HTTP server so the health probes hit a real
# socket (urlopen has enough edge cases that mocking it lies to us about
# the production behavior).
# ---------------------------------------------------------------------------


class _Stub(BaseHTTPRequestHandler):
    """HTTPServer handler driven by a class-level ``ROUTES`` dict."""

    ROUTES: dict[str, tuple[int, dict[str, Any] | str | None]] = {}

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return  # silence the per-request stderr line

    def _respond(self) -> None:
        route = self.ROUTES.get(self.path)
        if route is None:
            self.send_response(404)
            self.end_headers()
            return
        status, body = route
        self.send_response(status)
        if isinstance(body, dict):
            payload = json.dumps(body).encode("utf-8")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif isinstance(body, str):
            payload = body.encode("utf-8")
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self._respond()

    def do_POST(self) -> None:  # noqa: N802
        # Drain the request body so the client's send completes cleanly,
        # then route by path exactly like GET. Used by wizard_smoke_test,
        # which POSTs to /v1/wizard/chat.
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)
        self._respond()


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _StubServer:
    """Context manager: start a stub HTTP server with the given routes."""

    def __init__(self, routes: dict[str, tuple[int, dict[str, Any] | str | None]]):
        self.port = _free_port()

        handler_cls = type(
            "StubHandler",
            (_Stub,),
            {"ROUTES": routes},
        )
        self.server = HTTPServer(("127.0.0.1", self.port), handler_cls)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )

    def __enter__(self) -> _StubServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()


# ---------------------------------------------------------------------------
# api_healthy
# ---------------------------------------------------------------------------


def test_api_healthy_returns_true_when_engine_initialized() -> None:
    with _StubServer({
        "/v1/health": (
            200,
            {"status": "success", "data": {"engine_initialized": True}},
        )
    }) as server:
        ok, reason = services.api_healthy(server.port, timeout=1.0)
    assert ok is True
    assert "engine_initialized" in reason


def test_api_healthy_rejects_engine_not_initialized() -> None:
    """The exact failure mode PR #65 fixed — HTTP 200 but engine_initialized:false."""
    with _StubServer({
        "/v1/health": (
            200,
            {"status": "success", "data": {"engine_initialized": False}},
        )
    }) as server:
        ok, reason = services.api_healthy(server.port, timeout=1.0)
    assert ok is False
    assert reason == "engine_not_initialized"


def test_api_healthy_rejects_non_success_envelope() -> None:
    with _StubServer({
        "/v1/health": (200, {"status": "error", "data": {}})
    }) as server:
        ok, reason = services.api_healthy(server.port, timeout=1.0)
    assert ok is False
    assert "status=" in reason


def test_api_healthy_rejects_non_200() -> None:
    with _StubServer({"/v1/health": (500, "boom")}) as server:
        ok, reason = services.api_healthy(server.port, timeout=1.0)
    assert ok is False
    # urlopen raises HTTPError on 5xx — handled via the urllib.error.URLError
    # branch.
    assert reason  # some non-empty reason


def test_api_healthy_handles_unreachable_port() -> None:
    # A free port that nothing's listening on. Connection refused.
    port = _free_port()
    ok, reason = services.api_healthy(port, timeout=0.5)
    assert ok is False
    assert "unreachable" in reason


def test_api_healthy_handles_non_json_body() -> None:
    with _StubServer({"/v1/health": (200, "not-json")}) as server:
        ok, reason = services.api_healthy(server.port, timeout=1.0)
    assert ok is False
    assert reason == "non-JSON response"


# ---------------------------------------------------------------------------
# ollama_healthy
# ---------------------------------------------------------------------------


def test_ollama_healthy_returns_true_with_models_list() -> None:
    with _StubServer({
        "/api/tags": (
            200,
            {"models": [{"name": "nemotron"}, {"name": "llama3.2-vision"}]},
        )
    }) as server:
        ok, reason = services.ollama_healthy(server.port, timeout=1.0)
    assert ok is True
    assert "2 models" in reason


def test_ollama_healthy_accepts_empty_models_list() -> None:
    """A fresh Ollama with no models pulled is still healthy."""
    with _StubServer({"/api/tags": (200, {"models": []})}) as server:
        ok, reason = services.ollama_healthy(server.port, timeout=1.0)
    assert ok is True
    assert "0 models" in reason


def test_ollama_healthy_rejects_missing_models_field() -> None:
    """Catches the 'something else bound :11434 and returned 200' case."""
    with _StubServer({"/api/tags": (200, {"version": "0.0.0"})}) as server:
        ok, reason = services.ollama_healthy(server.port, timeout=1.0)
    assert ok is False
    assert reason == "missing models[]"


# ---------------------------------------------------------------------------
# webui_port_listening / port_listening
# ---------------------------------------------------------------------------


def test_webui_port_listening_detects_open_socket() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        ok, reason = services.webui_port_listening(port)
        assert ok is True
        assert reason == "listening"
    finally:
        sock.close()


def test_webui_port_listening_handles_closed_port() -> None:
    port = _free_port()
    ok, reason = services.webui_port_listening(port)
    assert ok is False
    assert "no listener" in reason


# ---------------------------------------------------------------------------
# kill_stale_api — best-effort, just verify it doesn't raise and that it
# shells out to the right tools per platform.
# ---------------------------------------------------------------------------


def test_kill_stale_api_invokes_fuser_and_pkill(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> Any:
        calls.append(list(cmd))

        class _Result:
            stdout = ""
            returncode = 0

        return _Result()

    monkeypatch.setattr(services.subprocess, "run", fake_run)
    monkeypatch.setattr(services.time, "sleep", lambda _: None)
    # Force the non-darwin branch so the test is platform-stable.
    monkeypatch.setattr(services.sys, "platform", "linux")

    services.kill_stale_api(8000)

    cmds = [c[0] for c in calls]
    assert "fuser" in cmds
    assert "pkill" in cmds
    # fuser is invoked with the port spec we expect.
    fuser_call = next(c for c in calls if c[0] == "fuser")
    assert fuser_call == ["fuser", "-k", "8000/tcp"]


def test_kill_stale_api_on_darwin_falls_back_to_lsof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> Any:
        calls.append(list(cmd))

        class _Result:
            # When lsof asks for listeners, return one pid so the kill path
            # also gets exercised.
            stdout = "12345\n" if cmd[0] == "lsof" else ""
            returncode = 0

        return _Result()

    monkeypatch.setattr(services.subprocess, "run", fake_run)
    monkeypatch.setattr(services.time, "sleep", lambda _: None)
    monkeypatch.setattr(services.sys, "platform", "darwin")

    services.kill_stale_api(8000)

    cmds = [c[0] for c in calls]
    assert "lsof" in cmds, f"expected lsof on darwin, got {cmds}"
    assert "kill" in cmds, f"expected kill -TERM on darwin, got {cmds}"
    kill_call = next(c for c in calls if c[0] == "kill")
    assert kill_call == ["kill", "-TERM", "12345"]


def test_kill_stale_api_swallows_missing_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If fuser / pkill / lsof aren't installed, kill_stale_api must NOT raise."""
    def raise_fnf(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("tool missing")

    monkeypatch.setattr(services.subprocess, "run", raise_fnf)
    monkeypatch.setattr(services.time, "sleep", lambda _: None)
    monkeypatch.setattr(services.sys, "platform", "darwin")

    # Must complete cleanly — best-effort is the whole point.
    services.kill_stale_api(8000)


# ---------------------------------------------------------------------------
# snapshot — aggregate status table
# ---------------------------------------------------------------------------


def test_snapshot_reports_all_three_services(monkeypatch: pytest.MonkeyPatch) -> None:
    """When nothing is running, snapshot reports all three as not-running."""

    def always_closed(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
        return False

    monkeypatch.setattr(services, "port_listening", always_closed)

    snap = services.snapshot(api_port=8000, webui_port=3000)

    assert snap.ollama.name == "Ollama"
    assert snap.ollama.running is False
    assert snap.ollama.suggested_action == "start"

    assert snap.api.port == 8000
    assert snap.api.running is False

    assert snap.webui.port == 3000
    assert snap.webui.running is False

    table = services.render_status_table(snap)
    assert "Ollama" in table
    assert "11434" in table
    assert "API" in table
    assert "WebUI" in table


def test_snapshot_distinguishes_running_from_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TCP listener that fails the health probe must surface as 'unhealthy'."""

    def always_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
        return True

    monkeypatch.setattr(services, "port_listening", always_listening)
    monkeypatch.setattr(
        services, "ollama_healthy",
        lambda port=services.OLLAMA_PORT, timeout=2.0: (True, "1 models"),
    )
    monkeypatch.setattr(
        services, "api_healthy",
        lambda port=8000, timeout=2.0: (False, "engine_not_initialized"),
    )

    snap = services.snapshot(api_port=8000, webui_port=3000)
    assert snap.ollama.healthy is True
    assert snap.api.running is True
    assert snap.api.healthy is False
    assert snap.api.suggested_action == "restart"
    # WebUI's "listening" is enough — no health probe beyond TCP.
    assert snap.webui.healthy is True
    assert snap.webui.suggested_action == "leave"


# ---------------------------------------------------------------------------
# start_pipeline / restart_pipeline — order + abort-on-failure
# ---------------------------------------------------------------------------


def test_start_pipeline_aborts_when_ollama_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        services, "start_ollama", lambda log_dir=None: (False, "binary missing")
    )
    # If we ever fall through, these would record steps — they must not.
    called: list[str] = []

    def _api(*args: Any, **kwargs: Any) -> tuple[bool, str]:
        called.append("api")
        return True, "ok"

    def _webui(*args: Any, **kwargs: Any) -> tuple[bool, str]:
        called.append("webui")
        return True, "ok"

    monkeypatch.setattr(services, "start_api", _api)
    monkeypatch.setattr(services, "start_webui", _webui)

    result = services.start_pipeline(open_browser=False)

    assert result.ok is False
    assert result.failed == "Ollama"
    assert result.reason == "binary missing"
    # Skip list now includes Smoke test (added 2026-05-22 as 4th
    # pipeline step). An earlier-step failure marks ALL later steps,
    # including the smoke test, so the Live table shows them as
    # skipped (not stuck on "waiting").
    assert result.skipped == ["API", "WebUI", "Smoke test"]
    assert called == [], "later steps must be skipped when Ollama fails"


def test_start_pipeline_aborts_when_api_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        services, "start_ollama", lambda log_dir=None: (True, "healthy")
    )
    monkeypatch.setattr(
        services, "start_api",
        lambda port=8000, log_dir=None: (False, "engine_not_initialized"),
    )
    webui_called = {"hit": False}

    def _webui(*args: Any, **kwargs: Any) -> tuple[bool, str]:
        webui_called["hit"] = True
        return True, "ok"

    monkeypatch.setattr(services, "start_webui", _webui)

    result = services.start_pipeline(open_browser=False)

    assert result.failed == "API"
    assert "engine_not_initialized" in result.reason
    # WebUI AND the smoke-test step (added 2026-05-22) both get skipped
    # when an earlier step fails.
    # Skip list now includes Smoke test (added 2026-05-22 as 4th
    # pipeline step) when API fails.
    assert result.skipped == ["WebUI", "Smoke test"]
    assert webui_called["hit"] is False


def test_start_pipeline_records_started_services_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        services, "start_ollama", lambda log_dir=None: (True, "ok-ollama")
    )
    monkeypatch.setattr(
        services, "start_api",
        lambda port=8000, log_dir=None: (True, "ok-api"),
    )
    monkeypatch.setattr(
        services, "start_webui",
        lambda port=3000, log_dir=None: (True, "ok-webui"),
    )
    # Smoke test (added 2026-05-22 as the 4th pipeline step) — stub
    # to return "ok" since we're not running a real API server in this
    # unit test. The end-to-end shape is verified by the real-rig
    # retest sequence + the release-hardening contract tests.
    monkeypatch.setattr(
        services, "wizard_smoke_test",
        lambda api_port=8000, timeout=45.0: (True, "ok: fake"),
    )

    # Don't actually open a browser during the test.
    monkeypatch.setattr(services.webbrowser, "open", lambda url: True)

    steps: list[tuple[str, bool]] = []

    def _on_step(label: str, ok: bool, reason: str) -> None:
        steps.append((label, ok))

    result = services.start_pipeline(open_browser=True, on_step=_on_step)
    assert result.ok is True
    assert result.started == ["Ollama", "API", "WebUI", "Smoke test"]
    assert [s[0] for s in steps] == ["Ollama", "API", "WebUI", "Smoke test"]
    assert all(ok for _, ok in steps)


def test_restart_pipeline_kills_then_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[int] = []

    monkeypatch.setattr(
        services, "port_listening",
        lambda port, host="127.0.0.1", timeout=0.5: True,
    )
    monkeypatch.setattr(
        services, "kill_stale_port", lambda port: killed.append(port)
    )
    monkeypatch.setattr(
        services, "kill_stale_api", lambda port=8000: killed.append(port)
    )
    monkeypatch.setattr(services.time, "sleep", lambda _: None)

    called_start: list[str] = []

    def _fake_start(**kwargs: Any) -> services.StartResult:
        called_start.append("start")
        result = services.StartResult()
        result.started = ["Ollama", "API", "WebUI"]
        return result

    monkeypatch.setattr(services, "start_pipeline", _fake_start)

    result = services.restart_pipeline(open_browser=False)

    # Both ports were targeted for kill (WebUI first, then API — reverse-
    # dependency order so the API isn't briefly orphaned).
    assert 3000 in killed
    assert 8000 in killed
    # And start_pipeline was invoked exactly once after the kill phase.
    assert called_start == ["start"]
    assert result.ok is True


# ---------------------------------------------------------------------------
# CLI integration — typer wiring exists and is reachable.
# ---------------------------------------------------------------------------


def test_services_app_registered_on_cli() -> None:
    """The `services` subapp must be registered on the main typer app."""
    from nvh.cli import main as cli_main

    # Inspect the typer app's registered groups. CliRunner-free check
    # because we don't want to spin up FastAPI / Engine just to verify
    # the subapp exists.
    registered = [g.typer_instance for g in cli_main.app.registered_groups]
    assert cli_main.services_app in registered

    # And the three subcommands are registered on the services subapp.
    command_names = {
        c.name for c in cli_main.services_app.registered_commands
    }
    assert {"status", "start", "restart"}.issubset(command_names)


# ---------------------------------------------------------------------------
# wizard_smoke_test — behavioral coverage (2026-06-10 audit: the #84-#88
# safety nets had only source-grep contract tests; these exercise the real
# HTTP parsing against a stub server, the same pattern as api_healthy above).
# ---------------------------------------------------------------------------


def test_wizard_smoke_test_ok_when_mode_is_llm() -> None:
    with _StubServer({
        "/v1/wizard/chat": (200, {"status": "success", "data": {"answer": "hi there", "mode": "llm"}}),
    }) as server:
        ok, reason = services.wizard_smoke_test(api_port=server.port, timeout=5)
    assert ok is True
    assert reason == "ok: llm"


def test_wizard_smoke_test_degraded_when_mode_is_deterministic() -> None:
    # The load-bearing fix: a deterministic-fallback answer is NON-empty
    # but must NOT be reported as healthy. ok=True (browser still opens)
    # but the reason is "degraded:" so the Live table shows yellow.
    with _StubServer({
        "/v1/wizard/chat": (200, {
            "status": "success",
            "data": {"answer": "Open the Setup page.", "mode": "deterministic",
                     "fallback_reason": "engine not initialized"},
        }),
    }) as server:
        ok, reason = services.wizard_smoke_test(api_port=server.port, timeout=5)
    assert ok is True
    assert reason.startswith("degraded: deterministic (no LLM)")
    assert "engine not initialized" in reason


def test_wizard_smoke_test_caps_long_fallback_reason() -> None:
    long_reason = "x" * 300
    with _StubServer({
        "/v1/wizard/chat": (200, {
            "status": "success",
            "data": {"answer": "canned", "mode": "deterministic", "fallback_reason": long_reason},
        }),
    }) as server:
        ok, reason = services.wizard_smoke_test(api_port=server.port, timeout=5)
    assert ok is True
    # The 300-char reason is capped at 80 chars in the status row.
    assert ("x" * 80) in reason
    assert ("x" * 81) not in reason


def test_wizard_smoke_test_rejects_empty_answer() -> None:
    with _StubServer({
        "/v1/wizard/chat": (200, {"status": "success", "data": {"answer": "   ", "mode": "llm"}}),
    }) as server:
        ok, reason = services.wizard_smoke_test(api_port=server.port, timeout=5)
    assert ok is False
    assert reason == "wizard returned empty answer"


def test_wizard_smoke_test_accepts_flat_unwrapped_body() -> None:
    # Some response shapes are flat ({answer, mode} at top level, no data:).
    with _StubServer({
        "/v1/wizard/chat": (200, {"answer": "flat hello", "mode": "llm"}),
    }) as server:
        ok, reason = services.wizard_smoke_test(api_port=server.port, timeout=5)
    assert ok is True
    assert reason == "ok: llm"


def test_wizard_smoke_test_reports_http_500() -> None:
    with _StubServer({"/v1/wizard/chat": (500, "boom")}) as server:
        ok, reason = services.wizard_smoke_test(api_port=server.port, timeout=5)
    assert ok is False
    assert reason == "HTTP 500"


def test_wizard_smoke_test_401_spells_out_the_fix() -> None:
    with _StubServer({"/v1/wizard/chat": (401, "unauthorized")}) as server:
        ok, reason = services.wizard_smoke_test(api_port=server.port, timeout=5)
    assert ok is False
    assert "HTTP 401" in reason
    assert "HIVE_API_KEY" in reason


def test_wizard_smoke_test_attaches_api_key_header(monkeypatch: pytest.MonkeyPatch) -> None:
    # When HIVE_API_KEY is set, the probe must send X-Hive-API-Key so an
    # auth-gated API doesn't 401 its own smoke test.
    seen_headers: dict[str, str] = {}

    class _AuthStub(_Stub):
        ROUTES = {"/v1/wizard/chat": (200, {"status": "success", "data": {"answer": "ok", "mode": "llm"}})}

        def do_POST(self) -> None:  # noqa: N802
            for k, v in self.headers.items():
                seen_headers[k.lower()] = v
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length:
                self.rfile.read(length)
            self._respond()

    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _AuthStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("HIVE_API_KEY", "secret-token-123")
        ok, _ = services.wizard_smoke_test(api_port=port, timeout=5)
    finally:
        server.shutdown()
        server.server_close()
    assert ok is True
    assert seen_headers.get("x-hive-api-key") == "secret-token-123"


# ---------------------------------------------------------------------------
# read_log_tail — failure-context helper attached to StartResult.
# ---------------------------------------------------------------------------


def test_read_log_tail_missing_file_returns_empty() -> None:
    assert services.read_log_tail("/no/such/path/nope.log") == []
    assert services.read_log_tail(None) == []


def test_read_log_tail_returns_last_n_lines(tmp_path) -> None:
    log = tmp_path / "api-server.log"
    log.write_text("\n".join(f"line {i}" for i in range(100)), encoding="utf-8")
    tail = services.read_log_tail(str(log), lines=10)
    assert tail == [f"line {i}" for i in range(90, 100)]


def test_read_log_tail_short_file_returns_all() -> None:
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
        fh.write("only one line")
        name = fh.name
    assert services.read_log_tail(name, lines=25) == ["only one line"]


# ---------------------------------------------------------------------------
# _preload_default_model — opt-out short-circuit.
# ---------------------------------------------------------------------------


def test_preload_default_model_optout_makes_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # NVH_OLLAMA_PRELOAD=0 must return before kicking any warmup work —
    # no /api/generate POST attempted. Patch urlopen to fail loud if the
    # opt-out is ever bypassed (it runs in a daemon thread, so give it a
    # beat to fire before asserting).
    import time as _time

    monkeypatch.setenv("NVH_OLLAMA_PRELOAD", "0")
    calls: list[Any] = []

    def _boom(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        raise AssertionError("preload made an HTTP request despite opt-out")

    monkeypatch.setattr(services.urllib.request, "urlopen", _boom)
    services._preload_default_model()
    _time.sleep(0.2)  # a real warmup thread would have fired by now
    assert calls == [], "preload must not POST /api/generate when opted out"


# ---------------------------------------------------------------------------
# `nvh services stop` — reverse-order kill + Ollama preservation (new command
# in this cycle, previously zero behavioral coverage).
# ---------------------------------------------------------------------------


def test_services_stop_kills_webui_before_api_and_spares_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    from nvh.cli import main as cli_main

    order: list[int] = []
    # Ports start "listening"; a kill marks them released so the verify
    # re-probe passes.
    released: set[int] = set()

    def fake_port_listening(port: int, *_a: Any, **_k: Any) -> bool:
        return port not in released

    def fake_kill_port(port: int) -> None:
        order.append(port)
        released.add(port)

    def fake_kill_api(port: int = 8000) -> None:
        order.append(port)
        released.add(port)

    monkeypatch.setattr(services, "port_listening", fake_port_listening)
    monkeypatch.setattr(services, "kill_stale_port", fake_kill_port)
    monkeypatch.setattr(services, "kill_stale_api", fake_kill_api)

    result = CliRunner().invoke(cli_main.services_app, ["stop"])
    assert result.exit_code == 0, result.output
    # WebUI (3000) killed before API (8000); Ollama (11434) never touched.
    assert order == [3000, 8000]
    assert services.OLLAMA_PORT not in order


def test_services_stop_with_ollama_flag_also_stops_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    from nvh.cli import main as cli_main

    order: list[int] = []
    released: set[int] = set()

    monkeypatch.setattr(
        services, "port_listening",
        lambda port, *a, **k: port not in released,
    )
    monkeypatch.setattr(
        services, "kill_stale_port",
        lambda port: (order.append(port), released.add(port)),
    )
    monkeypatch.setattr(
        services, "kill_stale_api",
        lambda port=8000: (order.append(port), released.add(port)),
    )

    result = CliRunner().invoke(cli_main.services_app, ["stop", "--ollama"])
    assert result.exit_code == 0, result.output
    assert services.OLLAMA_PORT in order
    # Ollama is stopped last (after WebUI + API).
    assert order[-1] == services.OLLAMA_PORT
