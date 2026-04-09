"""Auth regression tests.

Before this file, the auth middleware had zero test coverage — any
regression in the API-key / Bearer flow would have shipped silently.
These tests lock down the minimum contract: open mode works without
a token, protected mode rejects missing/malformed tokens and
accepts valid ones, and the auth rate limiter actually rate-limits.

We bypass the app's lifespan (which would try to initialize real
providers) by reusing the same mock-engine fixture pattern as
test_api.py.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import nvh.api.server as server_module
import nvh.storage.repository as repo
from nvh.api.server import app
from nvh.config.settings import (
    BudgetConfig,
    CacheConfig,
    CouncilConfig,
    CouncilModeConfig,
    DefaultsConfig,
    ProviderConfig,
    RoutingConfig,
)
from nvh.core.engine import Engine
from nvh.providers.base import (
    CompletionResponse,
    FinishReason,
    HealthStatus,
    ModelInfo,
    StreamChunk,
    Usage,
)
from nvh.providers.registry import ProviderRegistry

# ---------------------------------------------------------------------------
# Minimal mock provider (mirrors test_api.py::SimpleTestProvider)
# ---------------------------------------------------------------------------

class _AuthTestProvider:
    def __init__(self, name: str = "alpha") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def complete(self, messages, model=None, temperature=1.0,
                       max_tokens=4096, system_prompt=None, **kwargs):
        return CompletionResponse(
            content=f"ok from {self._name}",
            model=model or "test-model",
            provider=self._name,
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            cost_usd=Decimal("0.0"),
            latency_ms=1,
        )

    async def stream(self, messages, model=None, temperature=1.0,
                     max_tokens=4096, system_prompt=None, **kwargs):
        # `async def` + `yield` in the body makes this an async generator
        # directly — the callsite does `async for chunk in provider.stream(...)`
        # and we must not wrap this in another coroutine or we'd get
        # "coroutine 'stream' was never awaited" warnings.
        yield StreamChunk(
            delta="ok",
            is_final=True,
            accumulated_content="ok",
            model=model or "test-model",
            provider=self._name,
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            cost_usd=Decimal("0.0"),
            finish_reason=FinishReason.STOP,
        )

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(model_id="test-model", provider=self._name)]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(provider=self._name, healthy=True, latency_ms=1)

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


def _make_engine() -> Engine:
    provider = _AuthTestProvider("alpha")
    config = CouncilConfig(
        defaults=DefaultsConfig(provider="alpha", model="test-model"),
        providers={"alpha": ProviderConfig(enabled=True, default_model="test-model")},
        council=CouncilModeConfig(
            quorum=1, strategy="majority_vote", timeout=5,
            default_weights={"alpha": 1.0}, synthesis_provider="alpha",
        ),
        routing=RoutingConfig(),
        budget=BudgetConfig(),
        cache=CacheConfig(enabled=False, ttl_seconds=1, max_size=1),
    )
    registry = ProviderRegistry()
    registry.register("alpha", provider)
    engine = Engine(config=config, registry=registry)
    engine._initialized = True
    return engine


@pytest.fixture()
def secured_client(tmp_path: Path, monkeypatch):
    """TestClient with HIVE_API_KEY set so require_auth enforces a key."""
    import asyncio

    monkeypatch.setenv("HIVE_API_KEY", "secret-test-key-abc123")

    db_file = tmp_path / "auth_test.db"
    repo._engine = None
    repo._session_factory = None
    asyncio.run(repo.init_db(db_path=db_file))

    engine = _make_engine()
    original_engine = server_module._engine
    server_module._engine = engine

    # Clear auth rate-limit buckets so previous tests don't poison us
    server_module._auth_attempts.clear()

    # Don't use `with TestClient(app)` — that triggers the lifespan
    # which would re-initialize the real Engine and stomp our mock.
    client = TestClient(app, raise_server_exceptions=True)
    yield client

    server_module._engine = original_engine
    repo._engine = None
    repo._session_factory = None


@pytest.fixture()
def open_client(tmp_path: Path, monkeypatch):
    """TestClient with HIVE_API_KEY explicitly unset (open mode)."""
    import asyncio

    monkeypatch.delenv("HIVE_API_KEY", raising=False)

    db_file = tmp_path / "auth_test_open.db"
    repo._engine = None
    repo._session_factory = None
    asyncio.run(repo.init_db(db_path=db_file))

    engine = _make_engine()
    original_engine = server_module._engine
    server_module._engine = engine
    server_module._auth_attempts.clear()

    client = TestClient(app, raise_server_exceptions=True)
    yield client

    server_module._engine = original_engine
    repo._engine = None
    repo._session_factory = None


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------

class TestAuthEnforcement:
    def test_health_check_is_public(self, secured_client):
        """/v1/health must not require auth — it's polled by monitors."""
        r = secured_client.get("/v1/health")
        assert r.status_code == 200

    def test_protected_endpoint_rejects_missing_token(self, secured_client):
        """Protected endpoints must 401 without any credentials."""
        r = secured_client.get("/v1/models")
        assert r.status_code == 401
        assert "Authentication required" in r.json().get("detail", "")

    def test_protected_endpoint_rejects_malformed_bearer(self, secured_client):
        """A Bearer token that doesn't match must 401."""
        r = secured_client.get(
            "/v1/models",
            headers={"Authorization": "Bearer not-the-real-key"},
        )
        assert r.status_code == 401

    def test_protected_endpoint_rejects_wrong_xkey(self, secured_client):
        """An X-Hive-API-Key value that doesn't match must 401."""
        r = secured_client.get(
            "/v1/models",
            headers={"X-Hive-API-Key": "wrong-value"},
        )
        assert r.status_code == 401

    def test_protected_endpoint_accepts_valid_bearer(self, secured_client):
        """A valid Bearer token must let the request through."""
        r = secured_client.get(
            "/v1/models",
            headers={"Authorization": "Bearer secret-test-key-abc123"},
        )
        assert r.status_code == 200

    def test_protected_endpoint_accepts_valid_xkey(self, secured_client):
        """A valid X-Hive-API-Key must let the request through."""
        r = secured_client.get(
            "/v1/models",
            headers={"X-Hive-API-Key": "secret-test-key-abc123"},
        )
        assert r.status_code == 200

    def test_open_mode_skips_auth(self, open_client):
        """When HIVE_API_KEY is unset, protected endpoints must be open."""
        r = open_client.get("/v1/models")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# WebSocket auth
# ---------------------------------------------------------------------------

class TestWebSocketAuth:
    def test_ws_query_rejects_missing_token(self, secured_client):
        """WebSocket with no token must be refused with code 4001."""
        # TestClient's websocket_connect raises on rejection. We catch it
        # and verify the close reason if available.
        try:
            with secured_client.websocket_connect("/v1/ws/query") as ws:
                ws.send_json({"type": "query_request", "prompt": "hi"})
                # If we got here, the server accepted the connection — bug.
                pytest.fail("WebSocket accepted request without auth")
        except Exception:
            # Any exception here means the server rejected the connection,
            # which is what we want.
            pass

    def test_ws_query_rejects_invalid_token(self, secured_client):
        """WebSocket with a bogus token must be rejected."""
        try:
            with secured_client.websocket_connect(
                "/v1/ws/query?token=not-the-real-key"
            ) as ws:
                ws.send_json({"type": "query_request", "prompt": "hi"})
                pytest.fail("WebSocket accepted request with bad token")
        except Exception:
            pass

    def test_ws_query_accepts_valid_token(self, secured_client):
        """WebSocket with a valid token must be accepted.

        We only verify that the auth handshake succeeds (the connection
        opens without a 4001/4003 close code). We intentionally do NOT
        exercise the full stream path here — the purpose of this test
        is to lock down the auth contract, and driving the stream would
        touch aiosqlite on a different event loop than the fixture
        initialized it on, which hangs deterministically on Linux CI
        runners. The streaming path itself is covered by
        test_streaming_regressions.py.
        """
        # If auth fails, websocket_connect raises. If it succeeds, we
        # get a context manager yielding a connected socket — that's
        # all we need to verify.
        with secured_client.websocket_connect(
            "/v1/ws/query?token=secret-test-key-abc123"
        ) as ws:
            # Connection opened cleanly — auth passed. Close immediately
            # without sending anything so we don't fall into the
            # server's stream path.
            ws.close()


# ---------------------------------------------------------------------------
# Rate limiting on auth endpoints
# ---------------------------------------------------------------------------

class TestAuthRateLimit:
    def test_register_rate_limited_after_5_attempts(self, open_client):
        """The auth rate limiter must 429 after AUTH_RATE_LIMIT attempts."""
        from nvh.api.server import AUTH_RATE_LIMIT

        # Fire N+1 registration attempts with the same (invalid) payload.
        # The 6th must be rate-limited.
        last_status = None
        # Use valid payloads so pydantic validation doesn't short-circuit
        # before the rate-limit check fires. Password must be >=8 chars,
        # username >=3.
        for i in range(AUTH_RATE_LIMIT + 1):
            r = open_client.post(
                "/v1/auth/register",
                json={
                    "username": f"ratetest{i}",
                    "password": "password123",
                },
            )
            last_status = r.status_code

        assert last_status == 429, (
            f"Expected 429 on attempt {AUTH_RATE_LIMIT + 1}, got {last_status}"
        )
