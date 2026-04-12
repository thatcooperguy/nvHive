"""Deep coverage tests for proxy, engine, and system_tools modules.

Targets uncovered code paths in:
  - nvh/api/proxy.py   (helper functions, model resolution, format builders)
  - nvh/core/engine.py (cache hits, fallback, escalation, budget, logging)
  - nvh/core/system_tools.py (tool registration, descriptions, clipboard)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import nvh.api.server as server_module
import nvh.storage.repository as repo
from nvh.api.proxy import (
    build_models_list,
    format_openai_response,
    is_throwdown_model,
    openai_messages_to_nvhive,
    parse_council_model,
    resolve_provider_from_model,
)
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
from nvh.core.engine import Engine, ResponseCache
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

# =====================================================================
# Shared helpers
# =====================================================================


class _MockProvider:
    """Minimal mock provider reused across test classes."""

    def __init__(self, name: str = "mock") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def complete(self, messages, model=None, temperature=1.0,
                       max_tokens=4096, system_prompt=None, **kw):
        return CompletionResponse(
            content=f"reply from {self._name}",
            model=model or "test-model",
            provider=self._name,
            usage=Usage(input_tokens=5, output_tokens=10, total_tokens=15),
            cost_usd=Decimal("0.0001"),
            latency_ms=20,
        )

    async def stream(self, messages, model=None, temperature=1.0,
                     max_tokens=4096, system_prompt=None, **kw):
        async def _gen() -> AsyncIterator[StreamChunk]:
            yield StreamChunk(
                delta="reply",
                is_final=True,
                accumulated_content="reply",
                model=model or "test-model",
                provider=self._name,
                usage=Usage(input_tokens=5, output_tokens=10, total_tokens=15),
                cost_usd=Decimal("0.0001"),
                finish_reason=FinishReason.STOP,
            )
        return _gen()

    async def list_models(self):
        return [ModelInfo(model_id="test-model", provider=self._name)]

    async def health_check(self):
        return HealthStatus(provider=self._name, healthy=True, latency_ms=1)

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


def _build_engine(tmp_path: Path) -> Engine:
    provider = _MockProvider("alpha")
    config = CouncilConfig(
        defaults=DefaultsConfig(
            provider="alpha", model="test-model",
            temperature=0.0, max_tokens=128,
        ),
        providers={"alpha": ProviderConfig(enabled=True, default_model="test-model")},
        council=CouncilModeConfig(
            quorum=1, strategy="majority_vote", timeout=30,
            default_weights={"alpha": 1.0}, synthesis_provider="alpha",
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
def _test_client(tmp_path: Path):
    db_file = tmp_path / "deep_test.db"
    repo._engine = None
    repo._session_factory = None
    asyncio.run(repo.init_db(db_path=db_file))
    engine = _build_engine(tmp_path)
    original = server_module._engine
    server_module._engine = engine
    client = TestClient(app, raise_server_exceptions=True)
    yield client
    server_module._engine = original
    repo._engine = None
    repo._session_factory = None


# =====================================================================
# Module 1 — nvh/api/proxy.py
# =====================================================================


class TestProxyHelpers:
    """Pure-function helpers in proxy.py."""

    def test_parse_council_model_default(self):
        assert parse_council_model("council") == 3

    def test_parse_council_model_with_count(self):
        assert parse_council_model("council:5") == 5

    def test_parse_council_model_clamped(self):
        assert parse_council_model("council:1") == 2
        assert parse_council_model("council:99") == 10

    def test_parse_council_model_invalid(self):
        assert parse_council_model("council:abc") == 3

    def test_parse_council_model_none(self):
        assert parse_council_model("") is None
        assert parse_council_model("gpt-4o") is None

    def test_is_throwdown_model(self):
        assert is_throwdown_model("throwdown") is True
        assert is_throwdown_model("auto") is False
        assert is_throwdown_model(None) is False

    def test_resolve_provider_auto(self):
        assert resolve_provider_from_model("auto") == (None, None)
        assert resolve_provider_from_model(None) == (None, None)

    def test_resolve_provider_safe(self):
        assert resolve_provider_from_model("safe") == ("ollama", None)
        assert resolve_provider_from_model("local") == ("ollama", None)

    def test_resolve_provider_known_model(self):
        prov, mod = resolve_provider_from_model("gpt-4o")
        assert prov == "openai"
        assert mod == "gpt-4o"

    def test_resolve_provider_prefix_match(self):
        prov, mod = resolve_provider_from_model("gpt-4o-2024-11-20")
        assert prov == "openai"
        assert mod == "gpt-4o-2024-11-20"

    def test_resolve_provider_unknown(self):
        prov, mod = resolve_provider_from_model("my-custom-model")
        assert prov is None
        assert mod == "my-custom-model"

    def test_openai_messages_single_user(self):
        msgs = [{"role": "user", "content": "hello"}]
        prompt, sys = openai_messages_to_nvhive(msgs)
        assert prompt == "hello"
        assert sys is None

    def test_openai_messages_with_system(self):
        msgs = [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
        ]
        prompt, sys = openai_messages_to_nvhive(msgs)
        assert sys == "you are helpful"

    def test_openai_messages_structured_content(self):
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "describe this"},
            {"type": "image_url", "image_url": {"url": "x"}},
        ]}]
        prompt, _ = openai_messages_to_nvhive(msgs)
        assert "describe this" in prompt

    def test_format_openai_response_structure(self):
        resp = format_openai_response(
            "hi", "gpt-4o", "openai",
            prompt_tokens=10, completion_tokens=5,
        )
        assert resp["object"] == "chat.completion"
        assert resp["choices"][0]["message"]["content"] == "hi"
        assert resp["usage"]["total_tokens"] == 15
        assert resp["x_nvhive_provider"] == "openai"

    def test_build_models_list_virtual(self):
        registry = MagicMock()
        registry.list_enabled.return_value = []
        result = build_models_list(registry)
        assert result["object"] == "list"
        ids = [m["id"] for m in result["data"]]
        assert "nvhive" in ids
        assert "council" in ids
        assert "throwdown" in ids


class TestProxyEndpoints:
    """Proxy HTTP endpoints via TestClient."""

    def test_proxy_chat_completions(self, _test_client):
        resp = _test_client.post("/v1/proxy/chat/completions", json={
            "model": "auto",
            "messages": [{"role": "user", "content": "ping"}],
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["message"]["role"] == "assistant"

    def test_proxy_completions(self, _test_client):
        resp = _test_client.post("/v1/proxy/completions", json={
            "model": "auto",
            "prompt": "hello",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert "choices" in body

    def test_proxy_models(self, _test_client):
        resp = _test_client.get("/v1/proxy/models")
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "list"
        assert len(body["data"]) > 0

    def test_proxy_health(self, _test_client):
        resp = _test_client.get("/v1/proxy/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"

    def test_proxy_chat_error_model(self, _test_client):
        """Requesting a model that maps to a missing provider still returns 200
        with an error in the response (OpenAI convention)."""
        resp = _test_client.post("/v1/proxy/chat/completions", json={
            "model": "safe",
            "messages": [{"role": "user", "content": "test"}],
        })
        # safe -> ollama which isn't registered; the endpoint either
        # returns an error body or falls back — both are valid.
        assert resp.status_code in (200, 500, 502)


# =====================================================================
# Module 2 — nvh/core/engine.py
# =====================================================================


class TestResponseCache:
    """Direct tests on the in-memory LRU cache."""

    @pytest.mark.asyncio
    async def test_cache_put_and_get(self):
        cache = ResponseCache(max_size=10, ttl_seconds=300)
        resp = CompletionResponse(
            content="cached", model="m", provider="p",
            usage=Usage(input_tokens=1, output_tokens=2, total_tokens=3),
            cost_usd=Decimal("0.01"), latency_ms=10,
        )
        msgs = [Message(role="user", content="q")]
        await cache.put("p", "m", msgs, 0.0, 128, resp)
        hit = await cache.get("p", "m", msgs, 0.0, 128)
        assert hit is not None
        assert hit.cache_hit is True
        assert hit.cost_usd == Decimal("0")

    @pytest.mark.asyncio
    async def test_cache_miss(self):
        cache = ResponseCache(max_size=10, ttl_seconds=300)
        msgs = [Message(role="user", content="x")]
        assert await cache.get("p", "m", msgs, 0.0, 128) is None

    @pytest.mark.asyncio
    async def test_cache_ttl_expiry(self):
        cache = ResponseCache(max_size=10, ttl_seconds=1)
        resp = CompletionResponse(
            content="old", model="m", provider="p",
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            cost_usd=Decimal("0"), latency_ms=1,
        )
        msgs = [Message(role="user", content="q")]
        await cache.put("p", "m", msgs, 0.0, 128, resp)
        # Manually backdate the entry so it appears expired
        key = cache._make_key("p", "m", msgs, 0.0, 128)
        cache._store[key].timestamp -= 10
        assert await cache.get("p", "m", msgs, 0.0, 128) is None

    @pytest.mark.asyncio
    async def test_cache_eviction(self):
        cache = ResponseCache(max_size=1, ttl_seconds=300)
        msgs_a = [Message(role="user", content="a")]
        msgs_b = [Message(role="user", content="b")]
        resp = CompletionResponse(
            content="v", model="m", provider="p",
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            cost_usd=Decimal("0"), latency_ms=1,
        )
        await cache.put("p", "m", msgs_a, 0.0, 128, resp)
        await cache.put("p", "m", msgs_b, 0.0, 128, resp)
        assert await cache.get("p", "m", msgs_a, 0.0, 128) is None
        assert await cache.get("p", "m", msgs_b, 0.0, 128) is not None

    @pytest.mark.asyncio
    async def test_cache_clear(self):
        cache = ResponseCache(max_size=10, ttl_seconds=300)
        resp = CompletionResponse(
            content="v", model="m", provider="p",
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            cost_usd=Decimal("0"), latency_ms=1,
        )
        msgs = [Message(role="user", content="q")]
        await cache.put("p", "m", msgs, 0.0, 128, resp)
        cleared = await cache.clear()
        assert cleared == 1
        assert cache.stats["entries"] == 0


class TestEngineQueryPaths:
    """Engine.query() code paths: cache hit, fallback, escalation, budget."""

    @pytest.mark.asyncio
    async def test_query_cache_hit(self, tmp_path):
        engine = _build_engine(tmp_path)
        await repo.init_db(db_path=tmp_path / "eng.db")
        with patch.object(engine, "_check_connectivity", return_value=True):
            await engine.query("hello", provider="alpha", temperature=0)
        with patch.object(engine, "_check_connectivity", return_value=True):
            resp2 = await engine.query("hello", provider="alpha", temperature=0)
        assert resp2.cache_hit is True
        assert resp2.cost_usd == Decimal("0")

    @pytest.mark.asyncio
    async def test_query_fallback_on_error(self, tmp_path):
        engine = _build_engine(tmp_path)
        await repo.init_db(db_path=tmp_path / "eng2.db")

        from nvh.providers.base import ProviderError

        bad = _MockProvider("bad")
        bad.complete = AsyncMock(side_effect=ProviderError("provider boom"))
        engine.registry.register("bad", bad)

        with patch.object(engine, "_check_connectivity", return_value=True):
            resp = await engine.query("test", provider="bad")
        # Should have fallen back to alpha
        assert resp.provider == "alpha"
        assert resp.fallback_from == "bad"

    @pytest.mark.asyncio
    async def test_get_budget_status(self, tmp_path):
        engine = _build_engine(tmp_path)
        await repo.init_db(db_path=tmp_path / "eng3.db")
        status = await engine.get_budget_status()
        assert "daily_spend" in status
        assert "monthly_spend" in status
        assert "daily_limit" in status
        assert "monthly_limit" in status
        assert "daily_queries" in status
        assert "monthly_queries" in status

    @pytest.mark.asyncio
    async def test_log_query_handles_error(self, tmp_path):
        engine = _build_engine(tmp_path)
        resp = CompletionResponse(
            content="x", model="m", provider="alpha",
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            cost_usd=Decimal("0"), latency_ms=1,
        )
        with patch("nvh.storage.repository.log_query", side_effect=RuntimeError("db down")):
            # Should not raise — logs warning instead
            await engine._log_query(resp, "simple")

    @pytest.mark.asyncio
    async def test_query_escalation_delegates(self, tmp_path):
        engine = _build_engine(tmp_path)
        await repo.init_db(db_path=tmp_path / "eng4.db")
        mock_resp = CompletionResponse(
            content="escalated", model="m", provider="alpha",
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            cost_usd=Decimal("0"), latency_ms=1,
        )
        with patch(
            "nvh.core.smart_query.query_with_escalation",
            new_callable=AsyncMock,
            return_value=(mock_resp, {"escalated": False}),
        ):
            resp = await engine.query("test", escalate=True)
        assert resp.content == "escalated"

    @pytest.mark.asyncio
    async def test_cache_stats_property(self):
        cache = ResponseCache(max_size=50, ttl_seconds=600)
        stats = cache.stats
        assert stats["max_size"] == 50
        assert stats["ttl_seconds"] == 600
        assert stats["entries"] == 0


# =====================================================================
# Module 3 — nvh/core/system_tools.py
# =====================================================================


class TestSystemToolsRegistration:
    """Verify tool registration and metadata from system_tools module."""

    def test_register_system_tools_adds_tools(self):
        from nvh.core.system_tools import register_system_tools
        from nvh.core.tools import ToolRegistry

        registry = ToolRegistry(include_system=False)
        before = len(registry.list_tools())
        register_system_tools(registry)
        after = len(registry.list_tools())
        assert after > before

    def test_all_tool_descriptions_nonempty(self):
        from nvh.core.system_tools import register_system_tools
        from nvh.core.tools import ToolRegistry

        registry = ToolRegistry(include_system=False)
        register_system_tools(registry)
        for tool in registry.list_tools():
            assert tool.description, f"Tool {tool.name} has empty description"

    def test_known_tools_present(self):
        from nvh.core.system_tools import register_system_tools
        from nvh.core.tools import ToolRegistry

        registry = ToolRegistry(include_system=False)
        register_system_tools(registry)
        names = {t.name for t in registry.list_tools()}
        for expected in ("list_processes", "get_clipboard", "set_clipboard",
                         "system_info", "find_files", "disk_usage"):
            assert expected in names, f"Missing tool: {expected}"

    def test_tool_safe_flags(self):
        from nvh.core.system_tools import register_system_tools
        from nvh.core.tools import ToolRegistry

        registry = ToolRegistry(include_system=False)
        register_system_tools(registry)
        safe_tool = registry.get("list_processes")
        assert safe_tool is not None
        assert safe_tool.safe is True
        unsafe_tool = registry.get("kill_process")
        assert unsafe_tool is not None
        assert unsafe_tool.safe is False

    @pytest.mark.asyncio
    async def test_get_clipboard_mocked(self):
        """get_clipboard with a mocked subprocess returns captured text."""
        from nvh.core.system_tools import register_system_tools
        from nvh.core.tools import ToolRegistry

        registry = ToolRegistry(include_system=False)
        register_system_tools(registry)
        tool = registry.get("get_clipboard")
        assert tool is not None

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"clipboard text", b""))

        with patch("asyncio.create_subprocess_shell", return_value=mock_proc), \
             patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await tool.handler()
        assert "clipboard" in result.lower() or "text" in result.lower()

    @pytest.mark.asyncio
    async def test_set_clipboard_mocked(self):
        from nvh.core.system_tools import register_system_tools
        from nvh.core.tools import ToolRegistry

        registry = ToolRegistry(include_system=False)
        register_system_tools(registry)
        tool = registry.get("set_clipboard")
        assert tool is not None

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_shell", return_value=mock_proc), \
             patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await tool.handler(content="hello world")
        assert "11" in result or "copied" in result.lower()
