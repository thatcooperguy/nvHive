"""API server tests using FastAPI's TestClient.

All tests use mock providers and an isolated in-memory SQLite database.
No real API calls are made.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
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
    Message,
    ModelInfo,
    StreamChunk,
    Usage,
)
from nvh.providers.registry import ProviderRegistry

# ---------------------------------------------------------------------------
# Mock provider (same shape as the one in test_integration.py)
# ---------------------------------------------------------------------------


class SimpleTestProvider:
    """Minimal mock provider — no real API calls."""

    def __init__(self, name: str = "test_provider") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 1.0,
        max_tokens: int = 4096,
        system_prompt: str | None = None,
        **kwargs,
    ) -> CompletionResponse:
        return CompletionResponse(
            content=f"Mock response from {self._name}",
            model=model or "test-model",
            provider=self._name,
            usage=Usage(input_tokens=10, output_tokens=20, total_tokens=30),
            cost_usd=Decimal("0.001"),
            latency_ms=50,
        )

    async def stream(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 1.0,
        max_tokens: int = 4096,
        system_prompt: str | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        async def _gen() -> AsyncIterator[StreamChunk]:
            yield StreamChunk(
                delta=f"Mock response from {self._name}",
                is_final=True,
                accumulated_content=f"Mock response from {self._name}",
                model=model or "test-model",
                provider=self._name,
                usage=Usage(input_tokens=10, output_tokens=20, total_tokens=30),
                cost_usd=Decimal("0.001"),
                finish_reason=FinishReason.STOP,
            )

        return _gen()

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(model_id="test-model", provider=self._name)]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(provider=self._name, healthy=True, latency_ms=5)

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_test_engine(tmp_path: Path) -> Engine:
    """Build a fully-configured Engine backed by mock providers."""
    provider = SimpleTestProvider("alpha")

    config = CouncilConfig(
        defaults=DefaultsConfig(
            provider="alpha",
            model="test-model",
            temperature=1.0,
            max_tokens=256,
        ),
        providers={"alpha": ProviderConfig(enabled=True, default_model="test-model")},
        council=CouncilModeConfig(
            quorum=1,
            strategy="majority_vote",
            timeout=30,
            default_weights={"alpha": 1.0},
            synthesis_provider="alpha",
        ),
        routing=RoutingConfig(),
        budget=BudgetConfig(),
        cache=CacheConfig(enabled=True, ttl_seconds=3600, max_size=100),
    )

    registry = ProviderRegistry()
    registry.register("alpha", provider)

    engine = Engine(config=config, registry=registry)
    engine._initialized = True
    return engine


@pytest.fixture()
def test_client(tmp_path: Path):
    """Fixture that provides a TestClient wired to a mock Engine and a fresh DB."""
    # 1. Point repository at a fresh SQLite file for this test.
    # We use asyncio.run() so the DB init and the resulting aiosqlite worker
    # thread share a single completed loop — pytest-asyncio then uses its own
    # fresh loop for the async tests.
    import asyncio

    db_file = tmp_path / "api_test.db"
    repo._engine = None
    repo._session_factory = None
    asyncio.run(repo.init_db(db_path=db_file))

    # 2. Inject a mock engine into the server module (bypasses the lifespan)
    engine = _make_test_engine(tmp_path)
    original_engine = server_module._engine
    server_module._engine = engine

    # 3. Build client — use app directly without triggering lifespan
    client = TestClient(app, raise_server_exceptions=True)

    yield client

    # Restore state
    server_module._engine = original_engine
    repo._engine = None
    repo._session_factory = None


# ---------------------------------------------------------------------------
# TestAPIEndpoints
# ---------------------------------------------------------------------------


class TestAPIEndpoints:
    """Tests for the Council REST API endpoints."""

    def test_health_check(self, test_client: TestClient) -> None:
        """GET /v1/health returns 200 with status ok."""
        resp = test_client.get("/v1/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["data"]["status"] == "ok"
        assert body["data"]["engine_initialized"] is True

    def test_providers_list(self, test_client: TestClient) -> None:
        """GET /v1/advisors returns provider list."""
        resp = test_client.get("/v1/advisors")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        providers = body["data"]["providers"]
        assert isinstance(providers, list)
        names = [p["name"] for p in providers]
        assert "alpha" in names

    def test_models_list(self, test_client: TestClient) -> None:
        """GET /v1/models returns model catalog (may be empty for mock registry)."""
        resp = test_client.get("/v1/models")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert "models" in body["data"]
        assert "count" in body["data"]
        assert isinstance(body["data"]["models"], list)

    def test_models_list_with_provider_filter(self, test_client: TestClient) -> None:
        """GET /v1/models?provider=alpha returns filtered list."""
        resp = test_client.get("/v1/models?provider=alpha")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"

    def test_agent_presets(self, test_client: TestClient) -> None:
        """GET /v1/agents/presets returns preset names and role lists."""
        resp = test_client.get("/v1/agents/presets")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        presets = body["data"]["presets"]
        assert isinstance(presets, dict)
        # Known presets should be present
        for preset_name in ("executive", "engineering", "code_review"):
            assert preset_name in presets
            assert isinstance(presets[preset_name], list)
            assert len(presets[preset_name]) > 0

    def test_cache_stats(self, test_client: TestClient) -> None:
        """GET /v1/cache/stats returns cache info."""
        resp = test_client.get("/v1/cache/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        data = body["data"]
        assert "entries" in data
        assert "max_size" in data
        assert "ttl_seconds" in data
        assert data["entries"] >= 0

    def test_budget_status(self, test_client: TestClient) -> None:
        """GET /v1/budget/status returns budget info."""
        resp = test_client.get("/v1/budget/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        data = body["data"]
        assert "daily_spend" in data
        assert "monthly_spend" in data
        assert "daily_limit" in data
        assert "monthly_limit" in data
        assert "daily_queries" in data
        assert "monthly_queries" in data

    def test_agents_analyze(self, test_client: TestClient) -> None:
        """POST /v1/agents/analyze returns generated agent personas."""
        resp = test_client.post(
            "/v1/agents/analyze",
            json={"prompt": "Design a distributed database system", "num_agents": 2},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        agents = body["data"]["agents"]
        assert isinstance(agents, list)
        assert len(agents) >= 1
        for agent in agents:
            assert "role" in agent
            assert "expertise" in agent
            assert "system_prompt" in agent

    def test_agents_analyze_with_preset(self, test_client: TestClient) -> None:
        """POST /v1/agents/analyze with preset returns preset personas."""
        resp = test_client.post(
            "/v1/agents/analyze",
            json={"prompt": "Improve our system security", "preset": "security_review"},
        )
        assert resp.status_code == 200
        body = resp.json()
        agents = body["data"]["agents"]
        assert len(agents) > 0

    def test_agents_analyze_invalid_preset(self, test_client: TestClient) -> None:
        """POST /v1/agents/analyze with unknown preset returns 422."""
        resp = test_client.post(
            "/v1/agents/analyze",
            json={"prompt": "Test", "preset": "nonexistent_preset_xyz"},
        )
        assert resp.status_code == 422

    def test_cache_clear(self, test_client: TestClient) -> None:
        """DELETE /v1/cache clears all cache entries."""
        resp = test_client.delete("/v1/cache")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert "cleared" in body["data"]

    def test_cache_clear_by_provider(self, test_client: TestClient) -> None:
        """DELETE /v1/cache?provider=alpha clears provider-specific entries."""
        resp = test_client.delete("/v1/cache?provider=alpha")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["provider"] == "alpha"

    def test_provider_health_specific(self, test_client: TestClient) -> None:
        """GET /v1/advisors/alpha/health returns health status for that provider."""
        resp = test_client.get("/v1/advisors/alpha/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["data"]["healthy"] is True

    def test_provider_health_not_found(self, test_client: TestClient) -> None:
        """GET /v1/advisors/unknown/health returns 404."""
        resp = test_client.get("/v1/advisors/unknown_xyz/health")
        assert resp.status_code == 404

    def test_query_endpoint(self, test_client: TestClient) -> None:
        """POST /v1/query returns a completion response."""
        resp = test_client.post(
            "/v1/query",
            json={"prompt": "Hello from test", "provider": "alpha"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        data = body["data"]
        assert "content" in data
        assert "provider" in data
        assert data["provider"] == "alpha"
        assert "Mock response" in data["content"]

    def test_compare_endpoint(self, test_client: TestClient) -> None:
        """POST /v1/compare returns responses keyed by provider name."""
        resp = test_client.post(
            "/v1/compare",
            json={"prompt": "Compare me", "providers": ["alpha"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        data = body["data"]
        assert "alpha" in data

    def test_council_endpoint(self, test_client: TestClient) -> None:
        """POST /v1/council with a single-member council returns a result."""
        resp = test_client.post(
            "/v1/council",
            json={
                "prompt": "Council test",
                "members": ["alpha"],
                "synthesize": False,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        data = body["data"]
        assert "member_responses" in data
        assert "quorum_met" in data
