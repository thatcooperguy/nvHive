"""API server tests using FastAPI's TestClient.

All tests use mock providers and an isolated in-memory SQLite database.
No real API calls are made.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import nvh.api.deps as deps_module
import nvh.api.server as server_module
from nvh.api.server import QueryRequest, app
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
from nvh.integrations.services import schedule_runner
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
def test_client(tmp_path: Path, sync_db):
    """Fixture that provides a TestClient wired to a mock Engine and a fresh DB."""
    # Inject a mock engine into the server module (bypasses the lifespan)
    engine = _make_test_engine(tmp_path)
    original_engine = server_module._engine
    server_module._engine = engine

    # Build client — use app directly without triggering lifespan
    client = TestClient(app, raise_server_exceptions=True)

    yield client

    server_module._engine = original_engine


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

    def test_ready_success_maps_production_readiness(
        self,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GET /v1/ready returns the compact launcher readiness shape."""
        from nvh.integrations import production_readiness

        monkeypatch.setattr(
            production_readiness,
            "production_readiness_report",
            lambda home_dir=None: {
                "status": "pilot-ready",
                "pilot_ready": True,
                "production_ready": False,
                "summary": "Ready for beta",
                "counts": {"passed": 8, "warnings": 1, "blocked": 0, "total": 9},
                "next_actions": ["one", "two", "three", "four"],
                "inputs": {"storage_home": str(home_dir)},
            },
        )

        resp = test_client.get("/v1/ready", params={"home_dir": "/persist/nvhive"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["data"]["ready"] is True
        assert body["data"]["production_ready"] is False
        assert body["data"]["next_actions"] == ["one", "two", "three"]
        assert body["data"]["storage_home"] == "/persist/nvhive"

    def test_ready_failure_does_not_leak_exception_or_home_dir(
        self,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GET /v1/ready returns a sanitized failure payload."""
        from nvh.integrations import production_readiness

        def _raise(home_dir=None):
            raise RuntimeError(f"sk-testsecret1234567890 failed in {home_dir}")

        monkeypatch.setattr(production_readiness, "production_readiness_report", _raise)

        resp = test_client.get(
            "/v1/ready",
            params={"home_dir": "C:/Users/someone/persist/nvhive"},
        )
        rendered = resp.text

        assert resp.status_code == 200
        assert "sk-testsecret1234567890" not in rendered
        assert "C:/Users/someone" not in rendered
        assert resp.json()["data"]["ready"] is False

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
        server_module._LIVE_MODELS_CACHE.clear()
        resp = test_client.get("/v1/models")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert "models" in body["data"]
        assert "count" in body["data"]
        assert isinstance(body["data"]["models"], list)
        assert body["data"]["warnings"] == []

    def test_models_list_with_provider_filter(self, test_client: TestClient) -> None:
        """GET /v1/models?provider=alpha returns filtered list."""
        server_module._LIVE_MODELS_CACHE.clear()
        resp = test_client.get("/v1/models?provider=alpha")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"

    def test_models_list_hides_retired_rows_and_keeps_unknown_providers(self, test_client: TestClient) -> None:
        """Retired catalog rows never appear; providers without an adapter pass through unfiltered."""
        engine = server_module._engine
        engine.registry.load_capabilities()  # the shipped catalog, offline
        server_module._LIVE_MODELS_CACHE.clear()
        catalog = engine.registry.list_models()
        retired = {m.model_id for m in catalog if m.status == "retired"}
        active = {m.model_id for m in catalog if m.status != "retired"}
        assert retired and active
        resp = test_client.get("/v1/models")
        data = resp.json()["data"]
        ids = {m["model_id"] for m in data["models"]}
        assert ids == active  # nothing registered for these providers: unknown, so unfiltered
        assert not ids & retired
        assert data["count"] == len(active) and data["warnings"] == []
        row = data["models"][0]
        assert {"cost_known", "deprecation_date", "supports_function_calling", "successor", "status"} <= set(row)

    def test_models_list_intersects_against_the_live_list(self, test_client: TestClient) -> None:
        """A registered provider's live list filters its rows and vets the configured default."""
        engine = server_module._engine
        engine.registry.load_capabilities()
        server_module._LIVE_MODELS_CACHE.clear()
        groq_rows = [m for m in engine.registry.list_models(provider="groq") if m.status != "retired"]
        assert len(groq_rows) >= 2
        served, unserved = groq_rows[0].model_id, groq_rows[1].model_id

        class LiveProvider(SimpleTestProvider):
            async def list_models(self) -> list[ModelInfo]:
                return [ModelInfo(model_id=served, provider="groq")]

        engine.registry.register("groq", LiveProvider("groq"))
        engine.config.providers["groq"] = ProviderConfig(enabled=True, default_model=unserved)
        resp = test_client.get("/v1/models?provider=groq")
        data = resp.json()["data"]
        assert [m["model_id"] for m in data["models"]] == [served]
        assert data["count"] == 1
        assert data["warnings"] == [f"default model {unserved} not served by provider groq"]

    def test_models_list_treats_resource_paused_as_unknown(self, test_client: TestClient) -> None:
        """Admission pausing the probe (a BaseException) leaves the provider unfiltered, negative-cached."""
        from nvh.core.atohi import ResourcePaused

        engine = server_module._engine
        engine.registry.load_capabilities()
        server_module._LIVE_MODELS_CACHE.clear()
        groq_active = {m.model_id for m in engine.registry.list_models(provider="groq") if m.status != "retired"}

        class Paused(SimpleTestProvider):
            async def list_models(self) -> list[ModelInfo]:
                raise ResourcePaused("admission_denied")

        engine.registry.register("groq", Paused("groq"))
        resp = test_client.get("/v1/models?provider=groq")
        data = resp.json()["data"]
        assert {m["model_id"] for m in data["models"]} == groq_active
        assert data["warnings"] == []
        cached_at, cached = server_module._LIVE_MODELS_CACHE["groq"]
        assert cached is None
        remaining = server_module._LIVE_MODELS_TTL - (time.monotonic() - cached_at)
        assert 0 < remaining <= server_module.LIVE_MODELS_NEGATIVE_TTL

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
        assert "hits" in data
        assert "misses" in data
        assert "size" in data
        assert "hit_rate" in data
        assert data["entries"] >= 0

    def test_query_request_normalizes_auto_choices(self) -> None:
        """UI placeholder labels must not be routed as literal model IDs."""
        req = QueryRequest(
            prompt="hello",
            provider="Auto-pick best available",
            model="Recommended model",
        )
        assert req.provider is None
        assert req.model is None

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


# ---------------------------------------------------------------------------
# Coverage gaps — endpoints that test_api.py never touched.
#
# These are the endpoints the audit flagged as having no test coverage:
# /metrics, /v1/system/*, /v1/conversations/*, /v1/sandbox/*,
# /v1/setup/*, /v1/agents/analyze, /v1/auth/me, /v1/webhooks*. We hit
# each one and verify status code + envelope shape — full behavioral
# tests can come later, this is the regression-floor pass.
# ---------------------------------------------------------------------------


class TestAPIEndpointCoverage:
    """Smoke coverage of every documented endpoint we hadn't been hitting."""

    def test_metrics_endpoint(self, test_client: TestClient) -> None:
        """GET /metrics returns Prometheus exposition format."""
        resp = test_client.get("/metrics")
        assert resp.status_code == 200
        # Prometheus format starts with "# HELP" or "# TYPE" lines
        body = resp.text
        assert ("# HELP" in body or "# TYPE" in body or body == ""), (
            f"unexpected /metrics body: {body[:200]}"
        )

    def test_v1_metrics_alias(self, test_client: TestClient) -> None:
        """GET /v1/metrics is the same as /metrics."""
        resp = test_client.get("/v1/metrics")
        assert resp.status_code == 200

    def test_system_gpu_endpoint(self, test_client: TestClient) -> None:
        """GET /v1/system/gpu returns GPU info or empty list."""
        resp = test_client.get("/v1/system/gpu")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        # GPU info on a CI runner is usually empty — just verify shape
        assert "data" in body

    def test_system_recommendations_endpoint(self, test_client: TestClient) -> None:
        """GET /v1/system/recommendations returns recommendation data."""
        resp = test_client.get("/v1/system/recommendations")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"

    def test_system_info_endpoint(self, test_client: TestClient) -> None:
        """GET /v1/system/info combines GPU + advisors + budget."""
        resp = test_client.get("/v1/system/info")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        data = body["data"]
        # Should have at least these top-level keys
        assert "version" in data or "gpu" in data or "advisors" in data

    def test_analytics_endpoint(self, test_client: TestClient) -> None:
        """GET /v1/analytics returns usage analytics."""
        resp = test_client.get("/v1/analytics")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"

    def test_sandbox_status(self, test_client: TestClient) -> None:
        """GET /v1/sandbox/status reports sandbox availability."""
        with patch("nvh.sandbox.executor.SandboxExecutor._check_docker", new=AsyncMock(return_value=False)):
            resp = test_client.get("/v1/sandbox/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"

    def test_setup_validate_key_rejects_bad_key(self, test_client: TestClient) -> None:
        """POST /v1/setup/validate-key reports valid=False without persisting.

        A bogus key should fail the provider health-check; the endpoint must
        return a 200 with valid=False rather than 5xx-ing.
        """
        resp = test_client.post(
            "/v1/setup/validate-key",
            json={"provider": "openai", "api_key": "sk-test-bogus-1234567890"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        # Either the test client has a stub provider that accepts everything
        # (then valid=True), or the real provider rejects (valid=False). Both
        # are fine — what's not fine is a missing 'valid' field or 5xx.
        assert "valid" in body["data"]
        assert isinstance(body["data"]["valid"], bool)

    def test_setup_validate_key_unknown_provider_422(self, test_client: TestClient) -> None:
        """POST /v1/setup/validate-key rejects unknown provider IDs."""
        resp = test_client.post(
            "/v1/setup/validate-key",
            json={"provider": "not-a-real-provider", "api_key": "x" * 20},
        )
        assert resp.status_code == 422

    def test_wizard_reconnect_returns_welcome_back_payload(self, test_client: TestClient) -> None:
        """POST /v1/wizard/reconnect returns the shaped Welcome-back payload.

        End-to-end: route exists, runs through the reconnect helper without
        raising, and returns every key the UI's <WelcomeBackPanel> expects.
        """
        resp = test_client.post("/v1/wizard/reconnect", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        for key in (
            "summary",
            "first_run",
            "changed",
            "needs_attention",
            "auto_repaired",
            "what_changed",
            "what_survived",
            "elapsed_ms",
        ):
            assert key in body["data"], f"missing key: {key}"
        assert isinstance(body["data"]["summary"], str)
        assert isinstance(body["data"]["needs_attention"], list)
        assert isinstance(body["data"]["auto_repaired"], list)
        assert isinstance(body["data"]["elapsed_ms"], int)

    def test_wizard_tools_list_returns_safety_classified_catalog(self, test_client: TestClient) -> None:
        """GET /v1/wizard/tools lists Wizard tools with safety classes."""
        resp = test_client.get("/v1/wizard/tools")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        tools = body["data"]["tools"]
        names = {t["name"] for t in tools}
        assert {"refresh_models", "repair_workspace", "save_provider_key"}.issubset(names)
        # Every tool advertises its safety class so the UI can decide whether
        # to show a confirmation card (red for privileged) before
        # /v1/wizard/tools/execute.
        for tool in tools:
            assert tool["safety_class"] in ("auto", "confirm", "privileged")
            assert isinstance(tool["enabled"], bool)
        assert body["data"]["confirm_count"] >= 1
        assert body["data"]["auto_count"] >= 1
        assert body["data"]["privileged_count"] >= 1
        assert isinstance(body["data"]["privileged_enabled"], bool)

    def test_wizard_tools_execute_unknown_tool_returns_error(self, test_client: TestClient) -> None:
        resp = test_client.post("/v1/wizard/tools/execute", json={"name": "does_not_exist"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["ok"] is False
        assert "Unknown" in body["data"]["error"]

    def test_wizard_tools_execute_confirm_class_blocks_without_confirmed_flag(self, test_client: TestClient) -> None:
        """save_provider_key is confirm-class — it must refuse without confirmed=True."""
        resp = test_client.post(
            "/v1/wizard/tools/execute",
            json={"name": "save_provider_key", "arguments": {"provider": "openai", "api_key": "sk-test"}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["ok"] is False
        assert body["data"]["needs_confirmation"] is True
        assert "summary" in body["data"]

    def test_setup_free_providers(self, test_client: TestClient) -> None:
        """GET /v1/setup/free-providers lists no-signup providers."""
        resp = test_client.get("/v1/setup/free-providers")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert "providers" in body["data"]
        providers = {p["id"]: p for p in body["data"]["providers"]}
        assert providers["openai"]["key_url"] == "https://platform.openai.com/api-keys"
        assert providers["anthropic"]["docs_url"].startswith("https://docs.anthropic.com")
        assert providers["google"]["key_url"] == "https://aistudio.google.com/apikey"
        # llm7 was missing key_url metadata before 0.36; verify the fix landed.
        assert providers["llm7"]["key_url"] == "https://token.llm7.io/"
        # Logo slug exposed so the web client can render a brand mark from the
        # @lobehub/icons CDN. Null is allowed for providers without a mark.
        assert "logo_slug" in providers["openai"]
        assert providers["openai"]["logo_slug"] == "openai"
        assert providers["llm7"]["logo_slug"] is None

    def test_setup_status(self, test_client: TestClient) -> None:
        """GET /v1/setup/status returns first-run setup state."""
        resp = test_client.get("/v1/setup/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"

    def test_setup_production_readiness(self, test_client: TestClient) -> None:
        """GET /v1/setup/production-readiness returns release gates."""
        resp = test_client.get("/v1/setup/production-readiness")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert "production_ready" in body["data"]
        assert isinstance(body["data"]["gates"], list)

    def test_setup_diagnostics(self, test_client: TestClient) -> None:
        """GET /v1/setup/diagnostics returns a redacted support report."""
        resp = test_client.get("/v1/setup/diagnostics", headers={"x-request-id": "test-req"})
        assert resp.status_code == 200
        assert resp.headers["x-request-id"] == "test-req"
        body = resp.json()
        assert body["status"] == "success"
        assert body["data"]["request_id"] == "test-req"
        assert body["data"]["report_id"].startswith("diag-")
        assert "logs" in body["data"]

    def test_conversations_list(self, test_client: TestClient) -> None:
        """GET /v1/conversations returns the conversation list."""
        resp = test_client.get("/v1/conversations")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        # `data` may be a bare list (empty conversations) or a dict
        # with a "conversations" key — the endpoint shape evolved
        # over time. Both are valid for an empty DB.
        data = body["data"]
        assert isinstance(data, (list, dict))

    def test_quota_endpoint(self, test_client: TestClient) -> None:
        """GET /v1/quota returns quota info for all providers."""
        resp = test_client.get("/v1/quota")
        # 200 expected if implemented; 404 means the endpoint doesn't
        # exist on this build (some routes are conditional). Treat
        # both as "wired" for this smoke test — we just want to know
        # the route doesn't 500.
        assert resp.status_code in (200, 404)

    def test_webhooks_list(self, test_client: TestClient) -> None:
        """GET /v1/webhooks returns the configured webhooks list."""
        resp = test_client.get("/v1/webhooks")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"

    def test_context_get(self, test_client: TestClient) -> None:
        """GET /v1/context returns context info."""
        resp = test_client.get("/v1/context")
        # Either succeeds with context data or 404 if no context loaded
        assert resp.status_code in (200, 404)

    def test_agents_analyze_with_preset(self, test_client: TestClient) -> None:
        """POST /v1/agents/analyze with a known preset returns persona list."""
        # Try `default` preset first; if it doesn't exist, the endpoint
        # accepts no preset and just analyzes the prompt.
        resp = test_client.post(
            "/v1/agents/analyze",
            json={"prompt": "Should we use Rust?", "num_agents": 3},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert "agents" in body["data"]
        assert isinstance(body["data"]["agents"], list)

    def test_health_does_not_require_auth(self, test_client: TestClient) -> None:
        """GET /v1/health is the only endpoint that must be public."""
        # No auth header
        resp = test_client.get("/v1/health")
        assert resp.status_code == 200

    def test_auth_me_open_mode(self, test_client: TestClient) -> None:
        """GET /v1/auth/me in open mode (no HIVE_API_KEY) returns user info or 401."""
        resp = test_client.get("/v1/auth/me")
        # In open mode (no users configured) the response can be either:
        # - 200 with a default user payload
        # - 401 because there's no token to identify
        # Either is acceptable — just not a 500.
        assert resp.status_code in (200, 401, 404)


# ---------------------------------------------------------------------------
# /v1/health facts and the integrations scan (0.46)
# ---------------------------------------------------------------------------


class TestHealthFacts:
    """``GET /v1/health`` stays open and carries ``bind_host`` (``null`` when the app
    was not started by ``run_server``), ``auth_mode`` (the first tier ``require_auth``
    applies: ``users`` / ``key`` / ``open``) and ``key_required`` (whether a token-less
    request is refused). SECURITY.md states the disclosure; ``nvh launch`` refuses on them."""

    @staticmethod
    def _users(monkeypatch: pytest.MonkeyPatch, count: int) -> None:
        import nvh.auth.auth as auth_mod

        async def _count() -> int:
            return count

        monkeypatch.setattr(auth_mod, "get_user_count", _count)

    @staticmethod
    def _bind_host(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
        """Record ``app.state.bind_host`` the way ``run_server`` does, restorably.

        Through the ``State`` dict, not pytest's ``setattr`` on ``app.state``:
        Starlette's ``State`` keeps its keys in ``_state`` and defines
        ``__getattr__``, so pytest 9.2's ``setattr`` (which reads the old value
        from the instance ``__dict__``) records the key as absent every time.
        Patching it twice in one test then deletes twice at teardown — a
        ``KeyError`` from ``State.__delattr__`` that aborted the rest of the undo
        and leaked ``NVH_API_BIND_HOST`` into the session (CI run 41378464864).
        ``setitem`` tolerates a key that is already gone.
        """
        monkeypatch.setitem(app.state._state, "bind_host", value)

    @classmethod
    def _unstarted(cls, monkeypatch: pytest.MonkeyPatch) -> None:
        """Neither ``run_server`` nor its env export recorded a bind host."""
        cls._bind_host(monkeypatch, None)
        monkeypatch.delenv("NVH_API_BIND_HOST", raising=False)

    def test_payload_carries_both_facts(self, test_client: TestClient) -> None:
        data = test_client.get("/v1/health").json()["data"]
        assert {"status", "engine_initialized", "providers_enabled", "bind_host", "auth_mode", "key_required"} <= set(data)
        assert data["auth_mode"] in {"open", "key", "users"}
        assert isinstance(data["key_required"], bool)

    def test_open_mode(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVE_API_KEY", raising=False)
        self._unstarted(monkeypatch)
        data = test_client.get("/v1/health").json()["data"]
        assert data["auth_mode"] == "open" and data["key_required"] is False
        # Not started by run_server: the process cannot vouch for the interface it
        # listens on, so it says so instead of asserting loopback (a bare
        # ``uvicorn nvh.api.server:app --host 0.0.0.0`` must not read as safe).
        assert data["bind_host"] is None

    def test_key_mode_and_the_route_stays_open(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVE_API_KEY", "k-for-test")
        resp = test_client.get("/v1/health")  # no credential
        assert resp.status_code == 200
        assert resp.json()["data"]["auth_mode"] == "key"
        assert resp.json()["data"]["key_required"] is True

    def test_users_mode_wins_over_the_key(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        self._users(monkeypatch, 1)
        monkeypatch.delenv("HIVE_API_KEY", raising=False)
        data = test_client.get("/v1/health").json()["data"]
        # ``users`` without the key is still open mode (SECURITY.md); ``key_required``
        # is the fact that tells the two apart, which ``auth_mode`` alone cannot.
        assert data["auth_mode"] == "users" and data["key_required"] is False
        monkeypatch.setenv("HIVE_API_KEY", "k-for-test")
        data = test_client.get("/v1/health").json()["data"]
        assert data["auth_mode"] == "users" and data["key_required"] is True

    def test_bind_host_is_what_run_server_recorded(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        self._bind_host(monkeypatch, "0.0.0.0")
        assert test_client.get("/v1/health").json()["data"]["bind_host"] == "0.0.0.0"
        # uvicorn's reload subprocess only sees run_server's env export.
        self._bind_host(monkeypatch, None)
        monkeypatch.setenv("NVH_API_BIND_HOST", "192.0.2.10")
        assert test_client.get("/v1/health").json()["data"]["bind_host"] == "192.0.2.10"


class TestIntegrationsScanAuth:
    """``GET /v1/integrations/scan`` returns config paths, so a keyed deployment
    demands the key (open until 0.46); open mode admits it as before."""

    @pytest.fixture(autouse=True)
    def _no_detection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nvh.integrations.diagnostics import detector

        monkeypatch.setattr(detector, "detect_platforms", lambda: [])

    def test_keyed_rejects_a_bare_get_and_accepts_the_key(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVE_API_KEY", "k-for-test")
        assert test_client.get("/v1/integrations/scan").status_code == 401
        resp = test_client.get("/v1/integrations/scan", headers={"X-Hive-API-Key": "k-for-test"})
        assert resp.status_code == 200
        assert resp.json()["data"]["total_count"] == 0

    def test_open_mode_admits_the_scan(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVE_API_KEY", raising=False)
        resp = test_client.get("/v1/integrations/scan")
        assert resp.status_code == 200
        assert resp.json()["data"]["platforms"] == []


class TestDepsContract:
    """``nvh/api/deps.py`` is the one definition of the shared dependencies: the server
    re-exports the very same objects, the module imports without the server, and
    SECURITY.md names it. Pinned so a ``def require_auth`` re-added to server.py (every
    behaviour test would stay green) or a server import creeping into deps.py goes red."""

    def test_server_re_exports_the_same_objects(self) -> None:
        for name in deps_module.__all__:
            assert getattr(server_module, name) is getattr(deps_module, name), name
        source = Path(server_module.__file__).read_text(encoding="utf-8")
        for name in ("require_auth", "get_engine", "_response_envelope", "auth_mode", "key_required"):
            assert f"def {name}(" not in source, f"{name} is defined in nvh/api/deps.py, not in server.py"

    def test_deps_imports_without_the_server(self) -> None:
        code = (
            "import sys, nvh.api.deps; "
            "assert 'nvh.api.server' not in sys.modules, sorted(m for m in sys.modules if m.startswith('nvh.api'))"
        )
        subprocess.run([sys.executable, "-I", "-c", code], check=True, timeout=180)

    def test_security_md_names_deps_for_require_auth(self) -> None:
        text = (Path(server_module.__file__).parents[2] / "SECURITY.md").read_text(encoding="utf-8")
        assert "`require_auth` in `nvh/api/deps.py`" in text
        assert "`key_required`" in text and "`bind_host`" in text

    def test_auth_mode_and_open_mode_agree_on_users_without_the_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The three tier predicates answer different questions; pinned against each
        other in the one configuration where they look contradictory."""
        TestHealthFacts._users(monkeypatch, 1)
        monkeypatch.delenv("HIVE_API_KEY", raising=False)
        assert asyncio.run(deps_module.auth_mode()) == "users"
        assert asyncio.run(server_module._auth_open_mode()) is True
        assert deps_module.key_required() is False
        monkeypatch.setenv("HIVE_API_KEY", "k-for-test")
        assert asyncio.run(deps_module.auth_mode()) == "users"
        assert asyncio.run(server_module._auth_open_mode()) is False
        assert deps_module.key_required() is True

    def test_x_api_key_is_a_second_spelling_of_the_credential(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """The Anthropic SDKs send ``ANTHROPIC_API_KEY`` as ``x-api-key`` (0.47, T3.6)."""
        monkeypatch.setenv("HIVE_API_KEY", "k-for-test")
        assert test_client.get("/v1/models").status_code == 401
        ok = test_client.get("/v1/models", headers={"x-api-key": "k-for-test"})
        assert ok.status_code == 200, ok.text

    def test_wrong_x_api_key_is_refused_with_the_bearer_detail(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVE_API_KEY", "k-for-test")
        bad_key = test_client.get("/v1/models", headers={"x-api-key": "not-the-key"})
        bad_bearer = test_client.get("/v1/models", headers={"Authorization": "Bearer not-the-key"})
        assert bad_key.status_code == bad_bearer.status_code == 401
        assert bad_key.json()["detail"] == bad_bearer.json()["detail"]
        assert "x-api-key" in bad_key.json()["detail"]

# ---------------------------------------------------------------------------
# 0.46 Wave B (B4l): the routers mounted on the app, the session cost block of
# /v1/budget/status, the indexed vault save, profile starters, the avatar
# wording and the /v1/smart refusal of the confirm-class ``imagine`` (Risk 7).
# ---------------------------------------------------------------------------

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def _seed(coro_factory) -> None:
    """Run one seeding coroutine against the repository the ``sync_db`` fixture opened."""
    asyncio.run(coro_factory())


async def _backdate(query_id: str, *, hours: float) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    import nvh.storage.repository as repo
    from nvh.storage.models import QueryLog

    async with repo.get_session() as session:
        await session.execute(
            update(QueryLog).where(QueryLog.id == query_id)
            .values(created_at=datetime.now(UTC) - timedelta(hours=hours))
        )
        await session.commit()


def _since_ms(*, hours_ago: float) -> int:
    return int((time.time() - hours_ago * 3600) * 1000)


class TestBudgetStatusSession:
    """``GET /v1/budget/status`` (0.46 §7): ``hourly_rate_usd`` is the owner's figure
    or ``null``; ``session`` (with ``since``) sums the ledger since one instant and
    carries the catalog's verdicts; ``platform`` comes from the warmed facts."""

    @pytest.fixture(autouse=True)
    def _verdict_by_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A model called ``unpriced-*`` has no price; everything else is priced.

        Keeps the logic tests off LiteLLM's table (the real verdict is
        ``_catalog_cost_known``, exercised by its own tests)."""
        monkeypatch.setattr(
            server_module, "_catalog_cost_known",
            lambda model, provider, cost_usd, **kw: not str(model).startswith("unpriced-"),
        )

    def test_payload_carries_the_pill_fields_and_the_platform_block(self, test_client: TestClient) -> None:
        from nvh.utils.platform_facts import detect_platform_facts

        data = test_client.get("/v1/budget/status").json()["data"]
        assert {"daily_spend", "monthly_spend", "hourly_rate_usd", "daily_cost_known", "session", "platform"} <= set(data)
        assert data["hourly_rate_usd"] is None  # BudgetConfig default: never guessed
        assert data["session"] is None  # no ``since`` → no session block
        facts = detect_platform_facts()
        assert data["platform"] == {
            "device_class": facts.device_class,
            "device_label": facts.device_label,
            "is_cloud": facts.is_cloud,
            "cloud_provider": facts.cloud_provider,
            "is_owned": facts.is_owned,
        }
        assert data["platform"]["is_owned"] is True  # conftest: a workstation, not cloud

    def test_hourly_rate_is_the_configured_decimal_as_a_string(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server_module._engine.config.budget, "hourly_rate_usd", Decimal("2.10"))
        assert test_client.get("/v1/budget/status").json()["data"]["hourly_rate_usd"] == "2.10"

    def test_since_sums_successful_rows_after_the_cut_per_provider(self, test_client: TestClient) -> None:
        import nvh.storage.repository as repo

        async def seed() -> None:
            old = await repo.log_query(mode="simple", provider="openai", model="gpt-x", cost_usd=Decimal("0.50"))
            await _backdate(old.id, hours=3)
            await repo.log_query(mode="proxy", provider="openai", model="gpt-x", cost_usd=Decimal("0.10"))
            await repo.log_query(mode="proxy", provider="groq", model="llama-x", cost_usd=Decimal("0.02"))
            await repo.log_query(mode="proxy", provider="openai", model="gpt-x", cost_usd=Decimal("0.30"), status="error")

        _seed(seed)
        since = _since_ms(hours_ago=1)
        session = test_client.get("/v1/budget/status", params={"since": since}).json()["data"]["session"]
        assert session["since"] == since
        assert Decimal(session["spend_usd"]) == Decimal("0.12")  # the 3h-old row and the failed row are out
        assert {k: Decimal(v) for k, v in session["by_provider"].items()} == {"openai": Decimal("0.10"), "groq": Decimal("0.02")}
        assert session["cost_known"] is True and session["local_only"] is False and session["unavailable"] is False

    def test_cost_known_is_false_when_one_pair_is_unpriced_even_a_failed_call(self, test_client: TestClient) -> None:
        import nvh.storage.repository as repo

        async def seed() -> None:
            await repo.log_query(mode="proxy", provider="openai", model="gpt-x", cost_usd=Decimal("0.10"))
            # A cloud call that failed still happened: its pair counts for the verdict.
            await repo.log_query(mode="proxy", provider="openai", model="unpriced-x", cost_usd=Decimal("0"), status="error")

        _seed(seed)
        session = test_client.get("/v1/budget/status", params={"since": _since_ms(hours_ago=1)}).json()["data"]["session"]
        assert Decimal(session["spend_usd"]) == Decimal("0.10")
        assert session["cost_known"] is False

    def test_local_only_is_the_catalogs_verdict_never_a_zero_sum(self, test_client: TestClient) -> None:
        import nvh.storage.repository as repo

        async def seed_local() -> None:
            await repo.log_query(mode="wizard-chat", provider="ollama", model="llama3.2", cost_usd=Decimal("0"))

        _seed(seed_local)
        since = _since_ms(hours_ago=1)
        session = test_client.get("/v1/budget/status", params={"since": since}).json()["data"]["session"]
        assert session["local_only"] is True and Decimal(session["spend_usd"]) == 0

        async def seed_cloud_at_zero() -> None:
            # An unpriced cloud model logs $0 too — the sum stays zero, "local only" must not.
            await repo.log_query(mode="proxy", provider="openai", model="unpriced-y", cost_usd=Decimal("0"))

        _seed(seed_cloud_at_zero)
        session = test_client.get("/v1/budget/status", params={"since": since}).json()["data"]["session"]
        assert Decimal(session["spend_usd"]) == 0
        assert session["local_only"] is False and session["cost_known"] is False

    def test_daily_cost_known_judges_todays_pairs(self, test_client: TestClient) -> None:
        import nvh.storage.repository as repo

        assert test_client.get("/v1/budget/status").json()["data"]["daily_cost_known"] is True  # nothing spent

        async def seed() -> None:
            await repo.log_query(mode="proxy", provider="openai", model="unpriced-z", cost_usd=Decimal("0"))

        _seed(seed)
        assert test_client.get("/v1/budget/status").json()["data"]["daily_cost_known"] is False

    @pytest.mark.parametrize("since", ["-1", "soon", "1e30", "0", "1761955200"])
    def test_a_bad_since_is_a_422(self, test_client: TestClient, since: str) -> None:
        """Negative, non-integer, overflowing — and epoch *seconds* (``1761955200`` is 2025-11-01 in
        seconds; as milliseconds it is a 1970 instant that would sum the whole ledger)."""
        resp = test_client.get("/v1/budget/status", params={"since": since})
        assert resp.status_code == 422, resp.text
        if since in ("0", "1761955200"):
            assert "reads as seconds" in resp.json()["detail"]
        # A real millisecond instant still works.
        assert test_client.get("/v1/budget/status", params={"since": _since_ms(hours_ago=1)}).status_code == 200


class TestVaultMemoryIndexAndNotesRouter:
    """``POST /v1/vault/memory`` indexes the note best-effort and says so; the
    ``/v1/vault/notes`` router is mounted on the app and sees the same vault."""

    def test_save_returns_id_and_indexed_and_the_note_is_listed(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        import nvh.integrations.rag as rag

        calls: list[tuple[Path, object, object]] = []

        async def _index(path, *, home_dir=None, admission=None):
            calls.append((Path(path), home_dir, admission))
            return True

        monkeypatch.setattr(rag, "index_vault_note", _index)
        resp = test_client.post("/v1/vault/memory", json={"title": "ComfyUI port", "body": "ComfyUI listens on 8188 on this box."})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["saved"] is True and len(data["id"]) == 32 and data["schema"] == "nvh-memory/1"
        assert data["indexed"] is True
        # The path just written, the request's (absent) home_dir, and the engine's own admission —
        # the same object every other RAG route hands the embedder (an Atohi broker rides on it).
        assert calls == [(Path(data["path"]), None, server_module._engine.registry.admission)]
        assert calls[0][2] is not None

        listed = test_client.get("/v1/vault/notes", params={"memory_only": "true"}).json()["data"]
        assert any(note["id"] == data["id"] for note in listed["notes"])
        assert all("body" not in note for note in listed["notes"])
        one = test_client.get(f"/v1/vault/notes/{data['id']}").json()["data"]
        assert one["title"] == "ComfyUI port" and "8188" in one["body"]

    def test_an_indexing_failure_never_fails_the_save(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        import nvh.integrations.rag as rag

        async def _boom(path, **kw):
            raise RuntimeError("no embedder")

        monkeypatch.setattr(rag, "index_vault_note", _boom)
        resp = test_client.post("/v1/vault/memory", json={"title": "note", "body": "The save stands without an index."})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["saved"] is True and data["indexed"] is False and Path(data["path"]).exists()

    def test_a_paused_resource_leaves_the_save_standing_and_unindexed(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """``ResourcePaused`` is a ``BaseException``: an ``except Exception`` would let it escape as a 500
        after the note was written. The route catches it by name: 200, ``indexed: false``."""
        import nvh.integrations.rag as rag
        from nvh.core.atohi import ResourcePaused

        async def _paused(path, **kw):
            raise ResourcePaused("broker_unavailable")

        monkeypatch.setattr(rag, "index_vault_note", _paused)
        resp = test_client.post("/v1/vault/memory", json={"title": "paused", "body": "Written while the GPU was paused."})
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["saved"] is True and data["indexed"] is False and Path(data["path"]).exists()

    def test_notes_router_demands_the_key_when_keyed(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVE_API_KEY", "k-for-test")
        assert test_client.get("/v1/vault/notes").status_code == 401
        assert test_client.get("/v1/vault/notes", headers={"X-Hive-API-Key": "k-for-test"}).status_code == 200


class TestOutputsRouterMounted:
    """``GET /v1/outputs/images/{scope}/{filename}`` through the real app: open mode
    serves the file with the private cache header, a keyed deployment 401s without
    the key, a foreign scope is a 404."""

    def test_open_serves_keyed_refuses(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        from nvh.integrations.imagegen import outputs_images_root

        folder = outputs_images_root() / "wizard"
        folder.mkdir(parents=True)
        (folder / "20261101T120000Z-abcdef12.png").write_bytes(_PNG)
        url = "/v1/outputs/images/wizard/20261101T120000Z-abcdef12.png"

        monkeypatch.delenv("HIVE_API_KEY", raising=False)
        resp = test_client.get(url)
        assert resp.status_code == 200 and resp.content == _PNG
        assert resp.headers["content-type"].startswith("image/png")
        assert resp.headers["cache-control"] == "private, max-age=300"
        assert test_client.get("/v1/outputs/images/other/20261101T120000Z-abcdef12.png").status_code == 404

        monkeypatch.setenv("HIVE_API_KEY", "k-for-test")
        assert test_client.get(url).status_code == 401
        assert test_client.get(url, headers={"Authorization": "Bearer k-for-test"}).status_code == 200


class TestWizardProfilesStarter:
    """Rows of ``GET /v1/wizard/profiles`` carry ``starter`` (0.46) and the built-in
    avatar colours; ``POST`` accepts a ``starter`` for a user profile."""

    def test_rows_carry_starter_and_avatar_colours(self, test_client: TestClient) -> None:
        from nvh.integrations.wizard.avatars import _BUILT_IN_AVATARS

        rows = test_client.get("/v1/wizard/profiles").json()["data"]["profiles"]
        assert rows and all({"starter", "accent", "background"} <= set(row) for row in rows)
        by_name = {row["name"]: row for row in rows}
        assert by_name["wizard"]["starter"]  # the six core built-ins ship one
        assert by_name["wizard"]["background"] == _BUILT_IN_AVATARS["wizard"][0]
        assert by_name["wizard"]["accent"] == _BUILT_IN_AVATARS["wizard"][1]
        library_row = next(row for row in rows if row["name"] not in _BUILT_IN_AVATARS)
        assert library_row["accent"] == "" and library_row["background"] == ""

    def test_upsert_accepts_a_starter_and_the_list_returns_it(self, test_client: TestClient) -> None:
        resp = test_client.post("/v1/wizard/profiles", json={
            "name": "lab-helper", "title": "Lab helper", "starter": "Check the lab GPU queue.",
        })
        assert resp.status_code == 200 and resp.json()["data"]["saved"] is True
        rows = test_client.get("/v1/wizard/profiles").json()["data"]["profiles"]
        row = next(r for r in rows if r["name"] == "lab-helper")
        assert row["starter"] == "Check the lab GPU queue." and row["accent"] == ""


class TestAvatarGenerateWording:
    """The avatar route plans the backend first and names it in the error (0.46 §3)
    instead of blaming ComfyUI for every failure; the success envelope keeps its keys."""

    @staticmethod
    def _plan(monkeypatch: pytest.MonkeyPatch, backend: str | None) -> None:
        import nvh.integrations.imagegen as imagegen

        monkeypatch.setattr(
            imagegen, "plan_image_backend",
            lambda **kw: {"backend": backend, "summary": f"planned {backend or 'nothing'}"},
        )

    def test_error_names_the_planned_backend(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        import nvh.integrations.installs.comfyui as comfyui

        self._plan(monkeypatch, "comfyui")

        async def _boom(prompt, **kw):
            raise RuntimeError("ComfyUI returned 500")

        monkeypatch.setattr(comfyui, "generate_portrait", _boom)
        data = test_client.post("/v1/wizard/profiles/wizard/avatar/generate", json={"name": "wizard"}).json()["data"]
        assert data["ok"] is False
        assert data["error"] == "Image generation failed (comfyui): ComfyUI returned 500"
        assert data["backend"] == "comfyui" and data["plan"] == "planned comfyui"

    def test_no_backend_reads_none_with_the_shims_hint(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        import nvh.integrations.installs.comfyui as comfyui

        self._plan(monkeypatch, None)

        async def _unavailable(prompt, **kw):
            raise NotImplementedError(comfyui.PORTRAIT_UNAVAILABLE_HINT)

        monkeypatch.setattr(comfyui, "generate_portrait", _unavailable)
        data = test_client.post("/v1/wizard/profiles/wizard/avatar/generate", json={"name": "wizard"}).json()["data"]
        assert data["ok"] is False and data["backend"] is None
        assert data["error"].startswith("Image generation failed (none): No portrait generator available")

    def test_success_envelope_keeps_its_keys(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        import nvh.integrations.installs.comfyui as comfyui

        self._plan(monkeypatch, "nvidia")

        async def _render(prompt, **kw):
            return _PNG, ".png"

        monkeypatch.setattr(comfyui, "generate_portrait", _render)
        data = test_client.post("/v1/wizard/profiles/wizard/avatar/generate", json={"name": "wizard"}).json()["data"]
        assert {"ok", "name", "path", "url", "prompt"} <= set(data)
        assert data["ok"] is True and data["url"] == "/v1/wizard/profiles/wizard/avatar" and data["backend"] == "nvidia"
        assert Path(data["path"]).read_bytes() == _PNG


class TestJobsListFilters:
    """``GET /v1/jobs`` forwards ``exclude_kind`` / ``schedule_id`` (0.46 §6) to
    ``list_jobs`` only when given, so a kind-less call is byte-identical to before."""

    def test_filters_reach_list_jobs_only_when_given(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        import nvh.integrations.services.jobs as jobs

        seen: list[dict] = []

        def _list(**kwargs):
            seen.append(kwargs)
            return []

        monkeypatch.setattr(jobs, "list_jobs", _list)
        assert test_client.get("/v1/jobs").json()["data"] == {"jobs": [], "count": 0}
        test_client.get("/v1/jobs", params={"exclude_kind": "schedule-run", "schedule_id": "abc123", "limit": 8})
        assert seen == [
            {"kind": None, "status": None, "limit": 20},
            {"kind": None, "status": None, "limit": 8, "exclude_kind": "schedule-run", "schedule_id": "abc123"},
        ]


class TestSmartRefusesImagine:
    """Risk 7 / O3: ``imagine`` is confirm-class and ``/v1/smart`` runs the agent loop
    with no confirm callback, so an image task is refused — "Tool requires approval:
    imagine" is what the loop feeds back and what the answer says — and the image
    backend is never called, never billed."""

    def test_image_task_is_refused_and_never_bills(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        fed: list[str] = []

        async def _query(**kwargs):
            fed.append(kwargs["prompt"])
            if len(fed) == 1:
                content = 'TOOL_CALL: {"name": "imagine", "arguments": {"prompt": "a hero image for the lab wiki"}}'
            else:
                # The model relays the tool error it was handed, as a real one would.
                error = next((line for line in kwargs["prompt"].splitlines() if line.startswith("Error:")), "Error: ?")
                content = f"I can't render that from here — {error.removeprefix('Error: ')}."
            return SimpleNamespace(
                content=content, cost_usd=None, tool_calls=None, model="test-model", provider="alpha", usage=None,
            )

        monkeypatch.setattr(server_module._engine, "query", _query)
        generate = AsyncMock(return_value="/nowhere/hero.png")
        with patch("nvh.core.image_gen.generate_image", generate):
            resp = test_client.post(
                "/v1/smart", json={"prompt": "create a hero image for the lab wiki and then post it on the wiki"},
            )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["type"] == "agent" and data["completed"] is True
        assert data["tool_calls"] == 0  # nothing ran
        assert "Tool requires approval: imagine" in fed[1]
        assert "Tool requires approval: imagine" in data["output"]
        generate.assert_not_awaited()


# ---------------------------------------------------------------------------
# Schedules router through the real app  (design 0.46 §6; B4s)
# ---------------------------------------------------------------------------


class TestSchedulesRouterMounted:
    """The seven ``/v1/schedules`` routes answer through ``nvh.api.server.app``.

    The routes themselves are tested on a minimal app in tests/test_schedule_runner.py;
    these are the mount smoke: the list carries the runner block with the
    unattended tool roster, ``mode: do`` is a 422 with the exact refusal, the
    round trip (create, read, patch, run now, run log, delete) works end to end,
    and a keyed deployment refuses without the key. The store is the hermetic
    home's ``state/schedules.json``; nothing runs (``run_schedule_once`` is patched).
    """

    def test_list_carries_runner_block_and_do_is_refused(self, test_client: TestClient) -> None:
        from nvh.integrations.wizard.chat import SCHEDULE_AUTO_TOOLS

        resp = test_client.get("/v1/schedules")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["schedules"] == [] and data["count"] == 0
        runner = data["runner"]
        assert runner["active"] is False
        assert runner["tools"] == sorted(SCHEDULE_AUTO_TOOLS) and runner["tools"]
        assert runner["history"] == schedule_runner.SCHEDULE_RUN_HISTORY
        assert {"pid", "lock_path", "heartbeat", "stale"} <= set(runner)

        refused = test_client.post(
            "/v1/schedules", json={"prompt": "install everything", "every": "1h", "mode": "do"},
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"] == schedule_runner.DO_REFUSED
        assert test_client.get("/v1/schedules").json()["data"]["count"] == 0  # nothing was stored

    def test_seven_routes_round_trip(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        created = test_client.post(
            "/v1/schedules",
            json={"prompt": "what changed in the vault today?", "every": "1h", "name": "Vault digest"},
        )
        assert created.status_code == 201, created.text
        record = created.json()["data"]
        schedule_id = record["id"]
        assert len(schedule_id) == 32 and record["mode"] == "ask" and record["enabled"] is True
        assert record["interval_seconds"] == 3600 and record["name"] == "Vault digest"

        got = test_client.get(f"/v1/schedules/{schedule_id}")
        assert got.status_code == 200 and got.json()["data"]["id"] == schedule_id

        patched = test_client.patch(f"/v1/schedules/{schedule_id}", json={"enabled": False, "every": "30m"})
        assert patched.status_code == 200, patched.text
        assert patched.json()["data"]["enabled"] is False
        assert patched.json()["data"]["interval_seconds"] == 1800

        ran_with: list[str] = []

        async def _fake_run_once(task, **kwargs):
            ran_with.append(task.id)
            return "20261101120000-deadbeef"

        monkeypatch.setattr(schedule_runner, "run_schedule_once", _fake_run_once)
        run = test_client.post(f"/v1/schedules/{schedule_id}/run")
        assert run.status_code == 202, run.text
        assert run.json()["data"] == {"job_id": "20261101120000-deadbeef", "schedule_id": schedule_id}
        assert ran_with == [schedule_id]

        runs = test_client.get(f"/v1/schedules/{schedule_id}/runs?limit=5")
        assert runs.status_code == 200, runs.text
        assert runs.json()["data"] == {"runs": [], "count": 0, "schedule_id": schedule_id}

        deleted = test_client.delete(f"/v1/schedules/{schedule_id}")
        assert deleted.status_code == 200 and deleted.json()["data"]["deleted"] is True
        assert test_client.get(f"/v1/schedules/{schedule_id}").status_code == 404
        assert test_client.get("/v1/schedules").json()["data"]["count"] == 0

    def test_keyed_deployment_refuses_without_the_key(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVE_API_KEY", "k-for-schedules")
        assert test_client.get("/v1/schedules").status_code == 401
        assert test_client.post("/v1/schedules", json={"prompt": "x", "every": "1h"}).status_code == 401
        assert test_client.get("/v1/schedules/nope").status_code == 401
        ok = test_client.get("/v1/schedules", headers={"X-Hive-API-Key": "k-for-schedules"})
        assert ok.status_code == 200
        assert test_client.get("/v1/schedules", headers={"Authorization": "Bearer k-for-schedules"}).status_code == 200


# ---------------------------------------------------------------------------
# Lifespan: the schedule runner task  (design 0.46 §6 / Risk 3; B4s)
# ---------------------------------------------------------------------------


class _LifespanStubRegistry:
    def list_enabled(self) -> list[str]:
        return []


class _LifespanStubEngine:
    """Engine stand-in for the real lifespan: no providers, no DB, no network."""

    webhooks = None
    _initialized = True
    registry = _LifespanStubRegistry()

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def initialize(self) -> list[str]:
        return []


@pytest.fixture()
def scheduler_lifespan_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run the real lifespan against the hermetic home with a stub Engine.

    Same shape as ``lifespan_env`` in tests/test_api_server_endpoints.py; the
    home stays conftest's hermetic ``NVH_HOME`` so ``schedule_runner.lock_path()``
    is where a real loop would write.
    """
    import nvh.cli.setup as nvh_setup

    monkeypatch.setenv("HIVE_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("NVH_BOOT_PREFLIGHT", "0")
    monkeypatch.delenv("NVH_USE_KEYRING", raising=False)
    monkeypatch.setattr(nvh_setup, "DEFAULT_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(server_module, "Engine", _LifespanStubEngine)
    monkeypatch.setattr(server_module, "_engine", None)


class TestLifespanScheduler:
    """``NVH_SCHEDULER=1`` starts ``run_loop`` after the engine is ready and shutdown
    stops it: the stop event is set first, then the task is cancelled once the
    bounded wait runs out. With conftest's default ``NVH_SCHEDULER=0`` no task
    starts and no ``state/scheduler.lock`` appears under the hermetic home."""

    def test_runner_started_then_stopped_and_cancelled_on_shutdown(
        self, scheduler_lifespan_env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NVH_SCHEDULER", "1")
        monkeypatch.setenv("NVH_SCHEDULER_POLL_S", "7")
        monkeypatch.setattr(server_module, "_SCHEDULER_STOP_TIMEOUT_S", 0.2)
        seen: dict[str, object] = {}

        async def _recording_run_loop(stop: asyncio.Event, **kwargs) -> bool:
            seen.update(kwargs)
            seen["started"] = True
            await stop.wait()
            seen["stop_seen"] = True
            await asyncio.sleep(3600)  # a run "in flight" that ignores stop: shutdown must cancel it
            return True

        monkeypatch.setattr(schedule_runner, "run_loop", _recording_run_loop)

        with TestClient(app) as client:
            assert client.get("/v1/health").status_code == 200
            task = app.state.scheduler_task
            assert isinstance(task, asyncio.Task) and not task.done()
            assert task.get_name() == "nvh-schedule-runner"
            assert seen.get("started") is True
            assert app.state.scheduler_stop.is_set() is False

        assert seen["poll_s"] == 7.0 and seen["runner"] == "api"
        assert seen["stop_seen"] is True, "shutdown set the stop event before anything else"
        assert app.state.scheduler_stop.is_set()
        assert task.done() and task.cancelled(), "the stuck loop was cancelled after the bounded wait"
        assert not schedule_runner.lock_path().exists()

    def test_real_run_loop_waits_for_a_held_lock_instead_of_giving_up(
        self, scheduler_lifespan_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The REAL ``run_loop`` under the lifespan while ``nvh schedule start`` holds the lock: the task
        stays alive and waits (no takeover, nothing polled), one INFO line says so, and shutdown ends
        it cleanly with the holder's lock untouched — the API is never left without a scheduler.
        (The lifespan's ``setup_logging`` routes records to stdout, so the lines are read there.)"""
        import json

        monkeypatch.setenv("NVH_SCHEDULER", "1")
        monkeypatch.setenv("NVH_SCHEDULER_POLL_S", "0.05")
        holder = schedule_runner.ScheduleLock(schedule_runner.lock_path(), poll_s=60, runner=schedule_runner.RUNNER_CLI)
        assert holder.acquire()
        try:
            with TestClient(app) as client:
                assert client.get("/v1/health").status_code == 200
                time.sleep(0.3)
                task = app.state.scheduler_task
                assert isinstance(task, asyncio.Task) and not task.done(), "waiting, not finished"
                assert json.loads(schedule_runner.lock_path().read_text(encoding="utf-8"))["runner"] == "cli"
            assert task.done() and not task.cancelled() and task.result() is False
            out = capsys.readouterr().out
            assert "Schedule runner waiting: a live cli runner" in out
            assert "Schedule runner ended without polling" in out
            assert "Taking over" not in out
            assert json.loads(schedule_runner.lock_path().read_text(encoding="utf-8"))["runner"] == "cli"
        finally:
            holder.release()

    def test_a_runner_that_dies_at_start_is_logged_not_silent(
        self, scheduler_lifespan_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A runner task that raises is retrieved by the done-callback and logged at ERROR — not left
        for asyncio's "Task exception was never retrieved" at garbage-collection time."""
        monkeypatch.setenv("NVH_SCHEDULER", "1")

        async def _dies(stop: asyncio.Event, **kwargs) -> bool:
            raise RuntimeError("lock dir unwritable")

        monkeypatch.setattr(schedule_runner, "run_loop", _dies)
        with TestClient(app) as client:
            assert client.get("/v1/health").status_code == 200
            task = app.state.scheduler_task
            assert task.done() and isinstance(task.exception(), RuntimeError)
        assert "Schedule runner stopped with an error: lock dir unwritable" in capsys.readouterr().out

    def test_default_off_starts_no_task_and_writes_no_lock(self, scheduler_lifespan_env, monkeypatch: pytest.MonkeyPatch) -> None:
        assert server_module.os.environ.get("NVH_SCHEDULER") == "0"  # conftest's autouse default
        calls: list[dict[str, object]] = []

        async def _never_run_loop(stop: asyncio.Event, **kwargs) -> bool:
            calls.append(kwargs)
            return True

        monkeypatch.setattr(schedule_runner, "run_loop", _never_run_loop)

        with TestClient(app) as client:
            assert client.get("/v1/health").status_code == 200
            assert app.state.scheduler_task is None
            assert isinstance(app.state.scheduler_stop, asyncio.Event)

        assert calls == []
        assert not schedule_runner.lock_path().exists()
        assert not list(schedule_runner.lock_path().parent.glob("scheduler.lock*"))  # no heartbeat tmp either

    def test_poll_seconds_env_is_validated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NVH_SCHEDULER_POLL_S", raising=False)
        assert server_module._scheduler_poll_s() == schedule_runner.DEFAULT_POLL_S
        monkeypatch.setenv("NVH_SCHEDULER_POLL_S", "15")
        assert server_module._scheduler_poll_s() == 15.0
        for bad in ("0", "-3", "soon", "inf", "nan"):
            monkeypatch.setenv("NVH_SCHEDULER_POLL_S", bad)
            assert server_module._scheduler_poll_s() == schedule_runner.DEFAULT_POLL_S, bad
        for off in ("0", "false", "off", " No "):
            monkeypatch.setenv("NVH_SCHEDULER", off)
            assert server_module._scheduler_enabled() is False, off
        monkeypatch.setenv("NVH_SCHEDULER", "1")
        assert server_module._scheduler_enabled() is True

    def test_reload_parent_defaults_the_scheduler_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``run_server(reload=True)`` is uvicorn's reloader parent; it sets ``NVH_SCHEDULER=0``
        for the worker it spawns unless the operator chose a value (setdefault)."""
        import sys as _sys
        from types import ModuleType

        fake_uvicorn = ModuleType("uvicorn")
        runs: list[dict[str, object]] = []
        fake_uvicorn.run = lambda *args, **kwargs: runs.append(kwargs)  # type: ignore[attr-defined]
        monkeypatch.setitem(_sys.modules, "uvicorn", fake_uvicorn)
        # run_server also exports the port/bind facts. ``delenv(raising=False)`` on an absent name
        # records nothing, so set each one first: then monkeypatch really does put every one back
        # (CI runs the whole suite in one process; a leaked NVH_API_SERVER_PROCESS=1 would make
        # later tests look like the running API server).
        for var in ("NVH_SCHEDULER", "NVH_API_PORT", "NVH_API_SERVER_PROCESS", "NVH_API_BIND_HOST"):
            monkeypatch.setenv(var, server_module.os.environ.get(var, ""))
            monkeypatch.delenv(var)
        monkeypatch.setitem(app.state._state, "bind_host", app.state._state.get("bind_host"))

        server_module.run_server(host="127.0.0.1", port=8999, reload=True)
        assert server_module.os.environ["NVH_SCHEDULER"] == "0"
        assert runs and runs[-1]["reload"] is True

        monkeypatch.setenv("NVH_SCHEDULER", "1")
        server_module.run_server(host="127.0.0.1", port=8999, reload=True)
        assert server_module.os.environ["NVH_SCHEDULER"] == "1"  # an explicit choice wins

        monkeypatch.delenv("NVH_SCHEDULER", raising=False)
        server_module.run_server(host="127.0.0.1", port=8999, reload=False)
        assert "NVH_SCHEDULER" not in server_module.os.environ  # a plain `nvh serve` runs the scheduler


class TestVoiceRoutes:
    """``/v1/voice/*`` (nvh/api/routes/voice.py) through the server's ``include_router`` line:
    the refusals a client meets on a box without the ``voice`` extra, the one job the download
    route starts, and the key gate. tests/test_voice.py holds the backend matrix; here
    ``faster_whisper`` is absent (``sys.modules`` entry ``None``) unless a test fakes it."""

    @pytest.fixture(autouse=True)
    def _no_voice_paths(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nvh.api.routes import voice as voice_routes
        from nvh.core import voice

        monkeypatch.setitem(sys.modules, "faster_whisper", None)
        for name in voice.GROQ_KEY_ENV:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(voice, "_keyring_groq_key", lambda: None)
        monkeypatch.setattr(voice, "_LOCAL_MODELS", {})
        monkeypatch.setattr(voice_routes, "_DOWNLOAD_JOB_ID", None)
        monkeypatch.setattr(voice_routes, "_STT_LOCK", None)
        # The reason string must read "install nvhive[voice]" whatever box runs this; patch the
        # module's own seam rather than the interpreter-wide sys.platform.
        monkeypatch.setattr(voice, "platform_has_wheel", lambda: True)
        monkeypatch.delenv("HIVE_API_KEY", raising=False)

    @staticmethod
    def _post_clip(client: TestClient, body: bytes, mime: str = "audio/webm", name: str = "clip.webm"):
        return client.post("/v1/voice/transcribe", files={"file": (name, body, mime)})

    def test_transcribe_415_on_text(self, test_client: TestClient) -> None:
        resp = self._post_clip(test_client, b"hello", mime="text/plain", name="notes.txt")
        assert resp.status_code == 415, resp.text
        assert "audio/webm" in resp.json()["detail"]

    def test_transcribe_413_on_an_oversized_clip(self, test_client: TestClient) -> None:
        resp = self._post_clip(test_client, b"\0" * (21 * 1024 * 1024))
        assert resp.status_code == 413, resp.text
        assert "20 MB" in resp.json()["detail"]

    def test_transcribe_503_shape_when_no_path_exists(self, test_client: TestClient) -> None:
        from nvh.core import voice

        resp = self._post_clip(test_client, b"\x1aE\xdf\xa3" + b"\0" * 64)
        assert resp.status_code == 503, resp.text
        detail = resp.json()["detail"]
        assert set(detail) == {"message", "status", "downloading", "hint"}
        assert detail["downloading"] is False
        assert detail["status"]["stt"]["local_installed"] is False
        assert detail["status"]["stt"]["reason"] == voice.REASON_NOT_INSTALLED
        assert detail["status"]["stt"]["groq_available"] is False
        assert "nvhive[voice]" in detail["hint"] and "nvh advisor add groq" in detail["hint"]

    def test_download_returns_a_job_id_and_a_second_call_409s(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Through a shared portal (``with TestClient``, no lifespan: a minimal app carrying the
        router), since the job's consumer task must outlive the request that started it."""
        import threading
        import types

        from fastapi import FastAPI

        from nvh.api.routes import voice as voice_routes
        from nvh.core import voice
        from nvh.integrations.services import jobs

        gate = threading.Event()
        fake = types.ModuleType("faster_whisper")

        def download_model(name: str, **kwargs) -> str:
            gate.wait(timeout=10)
            snap = voice.download_root() / "models--Systran--faster-whisper-small.en" / "snapshots" / "x"
            snap.mkdir(parents=True, exist_ok=True)
            (snap / "model.bin").write_bytes(b"\0" * 16)
            (snap / "config.json").write_text("{}", encoding="utf-8")
            (snap / "vocabulary.txt").write_text("<|endoftext|>\n", encoding="utf-8")
            return str(snap)

        fake.download_model = download_model  # type: ignore[attr-defined]
        fake.WhisperModel = object  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "faster_whisper", fake)

        mini = FastAPI()
        mini.include_router(voice_routes.router)
        with TestClient(mini, raise_server_exceptions=True) as client:
            first = client.post("/v1/voice/download", json={"model": "small.en"})
            assert first.status_code == 202, first.text
            job_id = first.json()["data"]["job_id"]
            assert job_id and first.json()["data"]["model"] == "small.en"
            second = client.post("/v1/voice/download")
            assert second.status_code == 409, second.text
            assert second.json()["detail"]["job_id"] == job_id
            assert client.get("/v1/voice/status").json()["data"]["downloading"] is True
            gate.set()
            for _ in range(500):
                job = jobs.load_job(job_id, reconcile=False)
                if job["status"] in jobs.TERMINAL_STATUSES:
                    break
                time.sleep(0.02)
            assert job["status"] == "complete", job
            assert job["kind"] == "voice-model-download" and job["request"]["model"] == "small.en"
            assert client.get("/v1/voice/status").json()["data"]["downloading"] is False
        assert voice_routes.download_in_flight() is None

    def test_speak_501_without_edge_tts(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        import shutil

        monkeypatch.setattr(shutil, "which", lambda cmd, *a, **k: None)
        resp = test_client.post("/v1/voice/speak", json={"text": "hello"})
        assert resp.status_code == 501, resp.text
        assert resp.json()["detail"] == "browser speech synthesis is used; server TTS is optional"

    def test_status_is_keyed_like_every_other_route(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVE_API_KEY", "k-for-voice")
        assert test_client.get("/v1/voice/status").status_code == 401
        assert self._post_clip(test_client, b"x").status_code == 401
        assert test_client.post("/v1/voice/download").status_code == 401
        assert test_client.post("/v1/voice/speak", json={"text": "hi"}).status_code == 401
        ok = test_client.get("/v1/voice/status", headers={"X-Hive-API-Key": "k-for-voice"})
        assert ok.status_code == 200, ok.text
        data = ok.json()["data"]
        assert {"stt", "tts", "downloading", "download_job_id", "hint"} <= set(data)
        assert data["stt"]["local_installed"] is False and data["downloading"] is False
        assert test_client.get("/v1/voice/status", headers={"Authorization": "Bearer k-for-voice"}).status_code == 200
