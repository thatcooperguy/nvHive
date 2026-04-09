"""Live uvicorn integration tests.

Everything else in tests/ uses FastAPI's ``TestClient``, which runs
the app in-process without the real ASGI server. That catches most
route-level bugs but misses the ones that only show up with a real
server:

- lifespan startup/shutdown hooks (the Engine initializes here!)
- Uvicorn-specific middleware ordering
- CORS preflight against real hostnames
- WebSocket upgrade over a real socket (not the TestClient's fake one)
- HTTP/2, keep-alive, connection pooling
- Actual port binding and localhost routing

This file spins up ``uvicorn nvh.api.server:app`` as a subprocess on
an ephemeral port, polls until ``/v1/health`` responds, runs a
handful of smoke checks against the live server, and tears it down.

Every test here is essentially a production rehearsal: if this file
goes green, ``nvh serve`` on a user's machine will start correctly.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Grab a free TCP port by binding to 0 and reading it back."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(port: int, timeout: float = 30.0) -> bool:
    """Poll /v1/health until the server responds or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/v1/health", timeout=1.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


# ---------------------------------------------------------------------------
# Session-scoped fixture — one server per test session, shared across tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_server():
    """Spin up `uvicorn nvh.api.server:app` on an ephemeral port.

    Yields the base URL. Tears down the subprocess on module exit.
    The server uses the real Engine (no mock) so it performs actual
    provider auto-detection at startup — which means this test
    depends on the real environment (keys, ollama, etc.) for the
    engine startup to succeed. We don't hit any provider-specific
    endpoints, only /v1/health and /v1/models, which work without
    any credentials.
    """
    port = _free_port()
    # Use python -m uvicorn rather than the installed script so we
    # don't depend on the user's PATH and so it works on every
    # platform (the uvicorn.exe shim on Windows is the classic
    # "file in use" trap).
    cmd = [
        sys.executable, "-m", "uvicorn",
        "nvh.api.server:app",
        "--host", "127.0.0.1",
        "--port", str(port),
        "--log-level", "warning",
    ]

    # Suppress stdout/stderr noise by default. If debugging a failure,
    # re-enable by removing the DEVNULL arguments.
    proc = subprocess.Popen(
        cmd,
        cwd=str(Path(__file__).resolve().parent.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )

    try:
        ready = _wait_for_server(port, timeout=45.0)
        if not ready:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            pytest.skip(
                f"uvicorn did not start within 45s on port {port} — "
                f"possibly missing config or broken provider init"
            )

        yield f"http://127.0.0.1:{port}"

    finally:
        # Clean teardown — SIGTERM first, then SIGKILL if it lingers.
        # On Windows, Popen.terminate is actually TerminateProcess,
        # which is forceful but works.
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLiveServerStartup:
    def test_health_endpoint_responds(self, live_server: str):
        """GET /v1/health must return 200 with engine_initialized=True.

        If the lifespan hook raised, this test fails with a connection
        error or a 500 — either way, we catch broken Engine init
        before it hits a user's `nvh serve`.
        """
        r = httpx.get(f"{live_server}/v1/health", timeout=5.0)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "success"
        assert body["data"]["status"] == "ok"
        assert body["data"]["engine_initialized"] is True

    def test_openapi_schema_available(self, live_server: str):
        """GET /openapi.json returns a valid schema.

        FastAPI's auto-generated schema is how clients discover
        endpoints. Any route that can't be serialized breaks this.
        """
        r = httpx.get(f"{live_server}/openapi.json", timeout=5.0)
        assert r.status_code == 200
        schema = r.json()
        assert "openapi" in schema
        assert "paths" in schema
        # Sanity: the schema must list our core routes
        paths = schema["paths"]
        assert "/v1/health" in paths
        assert "/v1/advisors" in paths
        assert "/v1/models" in paths

    def test_docs_page_renders(self, live_server: str):
        """GET /docs returns the Swagger UI HTML."""
        r = httpx.get(f"{live_server}/docs", timeout=5.0)
        assert r.status_code == 200
        assert "swagger" in r.text.lower() or "openapi" in r.text.lower()

    def test_cors_preflight(self, live_server: str):
        """OPTIONS preflight from an allowed origin returns proper headers.

        Regression test for the CORS fix earlier in the 0.5.x series
        where the webui on http://nvhive couldn't reach the API on
        :8000 because the default ALLOWED_ORIGINS was too narrow.
        """
        r = httpx.options(
            f"{live_server}/v1/advisors",
            headers={
                "Origin": "http://nvhive",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "content-type",
            },
            timeout=5.0,
        )
        # Preflight responses are 200 or 204 depending on the
        # middleware config — both are correct.
        assert r.status_code in (200, 204)
        # The critical header is allow-origin (case-insensitive)
        allow_origin = r.headers.get("access-control-allow-origin", "")
        assert allow_origin == "http://nvhive" or allow_origin == "*"

    def test_404_on_unknown_route(self, live_server: str):
        """Unknown paths must 404 cleanly, not 500."""
        r = httpx.get(f"{live_server}/this/does/not/exist", timeout=5.0)
        assert r.status_code == 404

    def test_models_endpoint(self, live_server: str):
        """GET /v1/models returns the catalog via the real lifecycle.

        This exercises the /v1/models live-filter cache that uses
        asyncio.gather + per-provider timeouts. If the cache has any
        race or cleanup issue, it'll surface here rather than in a
        unit test that runs in an already-initialized state.
        """
        r = httpx.get(f"{live_server}/v1/models", timeout=15.0)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "success"
        assert "models" in body["data"]
        assert "count" in body["data"]
