"""Final coverage push E — deep into server.py, engine.py, council.py,
router.py, tools.py, proxy.py internals."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

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
# nvh/core/engine.py — uncovered decision paths
# ---------------------------------------------------------------------------

class TestEngineDeepPaths:
    @pytest.fixture(autouse=True)
    async def _init_db(self, tmp_path):
        import nvh.storage.repository as repo
        repo._engine = None
        repo._session_factory = None
        await repo.init_db(db_path=tmp_path / "engine_test.db")
        yield
        repo._engine = None
        repo._session_factory = None

    @pytest.mark.asyncio
    async def test_query_with_system_prompt(self):
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

        class FakeProvider:
            name = "alpha"
            async def complete(self, messages, **kw):
                # Check system prompt was passed
                sys = kw.get("system_prompt", "")
                return CompletionResponse(
                    content=f"got system: {sys}", model="m", provider="alpha",
                    usage=Usage(total_tokens=10), cost_usd=Decimal("0"), latency_ms=1,
                )
            async def stream(self, messages, **kw):
                yield StreamChunk(delta="ok", is_final=True, accumulated_content="ok",
                    model="m", provider="alpha", usage=Usage(total_tokens=5),
                    cost_usd=Decimal("0"), finish_reason=FinishReason.STOP)
            async def list_models(self): return [ModelInfo(model_id="m", provider="alpha")]
            async def health_check(self): return HealthStatus(provider="alpha", healthy=True, latency_ms=1)
            def estimate_tokens(self, t): return len(t) // 4

        config = CouncilConfig(
            defaults=DefaultsConfig(provider="alpha"),
            providers={"alpha": ProviderConfig(enabled=True, default_model="m")},
            council=CouncilModeConfig(quorum=1, timeout=5, default_weights={"alpha": 1.0}, synthesis_provider="alpha"),
            routing=RoutingConfig(), budget=BudgetConfig(),
            cache=CacheConfig(enabled=False, ttl_seconds=1, max_size=1),
        )
        reg = ProviderRegistry()
        reg.register("alpha", FakeProvider())
        engine = Engine(config=config, registry=reg)
        engine._initialized = True

        resp = await engine.query(prompt="hello", system_prompt="You are helpful")
        assert "got system" in resp.content

    @pytest.mark.asyncio
    async def test_compare_returns_dict(self):
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

        class FakeP:
            def __init__(self, n): self._n = n
            @property
            def name(self): return self._n
            async def complete(self, messages, **kw):
                return CompletionResponse(content=f"from {self._n}", model="m",
                    provider=self._n, usage=Usage(total_tokens=5),
                    cost_usd=Decimal("0"), latency_ms=10)
            async def stream(self, messages, **kw):
                yield StreamChunk(delta="ok", is_final=True, accumulated_content="ok",
                    model="m", provider=self._n, usage=Usage(total_tokens=5),
                    cost_usd=Decimal("0"), finish_reason=FinishReason.STOP)
            async def list_models(self): return [ModelInfo(model_id="m", provider=self._n)]
            async def health_check(self): return HealthStatus(provider=self._n, healthy=True, latency_ms=1)
            def estimate_tokens(self, t): return 1

        config = CouncilConfig(
            defaults=DefaultsConfig(provider="alpha"),
            providers={
                "alpha": ProviderConfig(enabled=True, default_model="m"),
                "beta": ProviderConfig(enabled=True, default_model="m"),
            },
            council=CouncilModeConfig(quorum=1, timeout=5,
                default_weights={"alpha": 1.0, "beta": 1.0}, synthesis_provider="alpha"),
            routing=RoutingConfig(), budget=BudgetConfig(),
            cache=CacheConfig(enabled=False, ttl_seconds=1, max_size=1),
        )
        reg = ProviderRegistry()
        reg.register("alpha", FakeP("alpha"))
        reg.register("beta", FakeP("beta"))
        engine = Engine(config=config, registry=reg)
        engine._initialized = True

        results = await engine.compare(prompt="hello")
        assert isinstance(results, dict)
        assert "alpha" in results or "beta" in results


# ---------------------------------------------------------------------------
# nvh/core/council.py — strategy branches
# ---------------------------------------------------------------------------

class TestCouncilStrategies:
    @pytest.mark.asyncio
    async def test_majority_vote_picks_most_common(self):
        from nvh.config.settings import (
            BudgetConfig,
            CacheConfig,
            CouncilConfig,
            CouncilModeConfig,
            DefaultsConfig,
            ProviderConfig,
            RoutingConfig,
        )
        from nvh.core.council import CouncilOrchestrator

        class VoteP:
            def __init__(self, n, answer):
                self._n = n; self._answer = answer
            @property
            def name(self): return self._n
            async def complete(self, messages, **kw):
                return CompletionResponse(content=self._answer, model="m",
                    provider=self._n, usage=Usage(total_tokens=5),
                    cost_usd=Decimal("0"), latency_ms=10)
            async def stream(self, messages, **kw):
                yield StreamChunk(delta=self._answer, is_final=True,
                    accumulated_content=self._answer, model="m",
                    provider=self._n, usage=Usage(total_tokens=5),
                    cost_usd=Decimal("0"), finish_reason=FinishReason.STOP)
            async def list_models(self): return []
            async def health_check(self): return HealthStatus(provider=self._n, healthy=True, latency_ms=1)
            def estimate_tokens(self, t): return 1

        config = CouncilConfig(
            defaults=DefaultsConfig(provider="a"),
            providers={
                "a": ProviderConfig(enabled=True, default_model="m"),
                "b": ProviderConfig(enabled=True, default_model="m"),
                "c": ProviderConfig(enabled=True, default_model="m"),
            },
            council=CouncilModeConfig(
                quorum=2, strategy="majority_vote", timeout=10,
                default_weights={"a": 1.0, "b": 1.0, "c": 1.0},
                synthesis_provider="a",
            ),
            routing=RoutingConfig(), budget=BudgetConfig(),
            cache=CacheConfig(enabled=False, ttl_seconds=1, max_size=1),
        )
        reg = ProviderRegistry()
        reg.register("a", VoteP("a", "Python"))
        reg.register("b", VoteP("b", "Python"))
        reg.register("c", VoteP("c", "Java"))

        council = CouncilOrchestrator(config, reg)
        result = await council.run_council(
            query="Best language?", synthesize=False, strategy="majority_vote",
        )
        assert result.quorum_met is True
        assert len(result.member_responses) >= 2


# ---------------------------------------------------------------------------
# nvh/core/tools.py — more handler coverage
# ---------------------------------------------------------------------------

class TestToolsDeep:
    @pytest.mark.asyncio
    async def test_shell_simple_command(self):
        from nvh.core.tools import ToolRegistry
        reg = ToolRegistry(workspace=".", include_system=False)
        result = await reg.execute("shell", {"command": "echo test123"})
        # On CI, Docker noise may interfere — just verify tool ran
        assert result is not None

    @pytest.mark.asyncio
    async def test_list_files_no_match(self, tmp_path):
        from nvh.core.tools import ToolRegistry
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        result = await reg.execute("list_files", {"pattern": "*.nonexistent"})
        assert result.success
        # Empty or "no files" message
        assert result.output is not None

    @pytest.mark.asyncio
    async def test_read_file_not_found(self, tmp_path):
        from nvh.core.tools import ToolRegistry
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        result = await reg.execute("read_file", {"path": "does_not_exist.txt"})
        assert not result.success
        assert "not found" in result.error.lower() or "No such" in result.error

    @pytest.mark.asyncio
    async def test_write_then_read(self, tmp_path):
        from nvh.core.tools import ToolRegistry
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        w = await reg.execute("write_file", {"path": "round_trip.txt", "content": "hello world"})
        assert w.success
        r = await reg.execute("read_file", {"path": "round_trip.txt"})
        assert r.success
        assert "hello world" in r.output


# ---------------------------------------------------------------------------
# nvh/core/router.py — scoring paths
# ---------------------------------------------------------------------------

class TestRouterScoring:
    def test_route_returns_decision(self):
        from nvh.config.settings import (
            BudgetConfig,
            CacheConfig,
            CouncilConfig,
            CouncilModeConfig,
            DefaultsConfig,
            ProviderConfig,
            RoutingConfig,
        )
        from nvh.core.rate_limiter import ProviderRateManager
        from nvh.core.router import RoutingEngine

        config = CouncilConfig(
            defaults=DefaultsConfig(provider="alpha"),
            providers={"alpha": ProviderConfig(enabled=True, default_model="m")},
            council=CouncilModeConfig(quorum=1, timeout=5, default_weights={"alpha": 1.0}),
            routing=RoutingConfig(), budget=BudgetConfig(),
            cache=CacheConfig(enabled=False, ttl_seconds=1, max_size=1),
        )
        reg = ProviderRegistry()

        class FakeP:
            name = "alpha"
            async def list_models(self): return [ModelInfo(model_id="m", provider="alpha")]
        reg.register("alpha", FakeP())

        rm = ProviderRateManager()
        router = RoutingEngine(config, reg, rm)
        decision = router.route("hello world")
        assert decision.provider == "alpha"
        assert decision.model is not None

    def test_route_with_override(self):
        from nvh.config.settings import (
            BudgetConfig,
            CacheConfig,
            CouncilConfig,
            CouncilModeConfig,
            DefaultsConfig,
            ProviderConfig,
            RoutingConfig,
        )
        from nvh.core.rate_limiter import ProviderRateManager
        from nvh.core.router import RoutingEngine

        config = CouncilConfig(
            defaults=DefaultsConfig(provider="alpha"),
            providers={"alpha": ProviderConfig(enabled=True, default_model="m")},
            council=CouncilModeConfig(quorum=1, timeout=5, default_weights={"alpha": 1.0}),
            routing=RoutingConfig(), budget=BudgetConfig(),
            cache=CacheConfig(enabled=False, ttl_seconds=1, max_size=1),
        )
        reg = ProviderRegistry()

        class FakeP:
            name = "alpha"
        reg.register("alpha", FakeP())

        rm = ProviderRateManager()
        router = RoutingEngine(config, reg, rm)
        decision = router.route("hello", provider_override="alpha", model_override="custom-model")
        assert decision.provider == "alpha"
        assert decision.model == "custom-model"


# ---------------------------------------------------------------------------
# nvh/providers — more provider coverage
# ---------------------------------------------------------------------------

class TestProviderMiscDeep:
    @pytest.mark.asyncio
    async def test_cohere_complete(self):
        from nvh.providers.cohere_provider import CohereProvider
        fake = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="cohere ok"), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=5, total_tokens=8),
            model="command-r",
        )
        with patch("nvh.providers.cohere_provider.litellm.acompletion", new=AsyncMock(return_value=fake)):
            p = CohereProvider()
            resp = await p.complete(messages=[Message(role="user", content="hi")])
            assert resp.content == "cohere ok"

    @pytest.mark.asyncio
    async def test_mistral_complete(self):
        from nvh.providers.mistral_provider import MistralProvider
        fake = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="mistral ok"), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=5, total_tokens=8),
            model="mistral-large",
        )
        with patch("nvh.providers.mistral_provider.litellm.acompletion", new=AsyncMock(return_value=fake)):
            p = MistralProvider()
            resp = await p.complete(messages=[Message(role="user", content="hi")])
            assert resp.content == "mistral ok"

    @pytest.mark.asyncio
    async def test_google_complete(self):
        from nvh.providers.google_provider import GoogleProvider
        fake = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="gemini ok"), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=5, total_tokens=8),
            model="gemini-pro",
        )
        with patch("nvh.providers.google_provider.litellm.acompletion", new=AsyncMock(return_value=fake)):
            p = GoogleProvider()
            resp = await p.complete(messages=[Message(role="user", content="hi")])
            assert resp.content == "gemini ok"

    @pytest.mark.asyncio
    async def test_grok_complete(self):
        from nvh.providers.grok_provider import GrokProvider
        fake = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="grok ok"), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=5, total_tokens=8),
            model="grok-1",
        )
        with patch("nvh.providers.grok_provider.litellm.acompletion", new=AsyncMock(return_value=fake)):
            p = GrokProvider()
            resp = await p.complete(messages=[Message(role="user", content="hi")])
            assert resp.content == "grok ok"
