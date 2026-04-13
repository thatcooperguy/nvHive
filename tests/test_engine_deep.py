"""Deep tests for nvh.core.engine — ResponseCache, fallback chain, budget, offline.

All provider calls are mocked. No real API calls, no real DB access.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nvh.core.engine import BudgetExceededError, ResponseCache
from nvh.providers.base import CompletionResponse, Message, Usage

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_response(content: str = "hello", provider: str = "test", model: str = "m1") -> CompletionResponse:
    return CompletionResponse(
        content=content,
        model=model,
        provider=provider,
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        cost_usd=Decimal("0.001"),
    )


# ---------------------------------------------------------------------------
# ResponseCache
# ---------------------------------------------------------------------------


class TestResponseCache:

    @pytest.mark.asyncio
    async def test_put_and_get(self):
        cache = ResponseCache(max_size=10, ttl_seconds=300)
        msgs = [Message(role="user", content="hi")]
        resp = _make_response()

        await cache.put("prov", "model", msgs, 0.0, 100, resp)
        hit = await cache.get("prov", "model", msgs, 0.0, 100)

        assert hit is not None
        assert hit.cache_hit is True
        assert hit.cost_usd == Decimal("0")
        assert hit.content == "hello"

    @pytest.mark.asyncio
    async def test_miss_on_different_prompt(self):
        cache = ResponseCache(max_size=10, ttl_seconds=300)
        msgs_a = [Message(role="user", content="hi")]
        msgs_b = [Message(role="user", content="bye")]
        await cache.put("p", "m", msgs_a, 0.0, 100, _make_response())

        hit = await cache.get("p", "m", msgs_b, 0.0, 100)
        assert hit is None

    @pytest.mark.asyncio
    async def test_ttl_expiry(self):
        cache = ResponseCache(max_size=10, ttl_seconds=1)
        msgs = [Message(role="user", content="hi")]
        await cache.put("p", "m", msgs, 0.0, 100, _make_response())

        # Manually expire the entry
        for entry in cache._store.values():
            entry.timestamp = time.time() - 10

        hit = await cache.get("p", "m", msgs, 0.0, 100)
        assert hit is None

    @pytest.mark.asyncio
    async def test_lru_eviction(self):
        cache = ResponseCache(max_size=2, ttl_seconds=300)
        for i in range(3):
            msgs = [Message(role="user", content=f"msg{i}")]
            await cache.put("p", "m", msgs, 0.0, 100, _make_response(content=f"r{i}"))

        # First entry should be evicted
        assert len(cache._store) == 2

    @pytest.mark.asyncio
    async def test_clear_all(self):
        cache = ResponseCache(max_size=10, ttl_seconds=300)
        msgs = [Message(role="user", content="hi")]
        await cache.put("p", "m", msgs, 0.0, 100, _make_response())

        count = await cache.clear()
        assert count == 1
        assert len(cache._store) == 0

    @pytest.mark.asyncio
    async def test_clear_by_provider(self):
        cache = ResponseCache(max_size=10, ttl_seconds=300)
        msgs1 = [Message(role="user", content="q1")]
        msgs2 = [Message(role="user", content="q2")]
        await cache.put("groq", "m", msgs1, 0.0, 100, _make_response(provider="groq"))
        await cache.put("openai", "m", msgs2, 0.0, 100, _make_response(provider="openai"))

        removed = await cache.clear(provider="groq")
        assert removed == 1
        assert len(cache._store) == 1

    @pytest.mark.asyncio
    async def test_stats(self):
        cache = ResponseCache(max_size=50, ttl_seconds=600)
        assert cache.stats["entries"] == 0
        assert cache.stats["max_size"] == 50
        assert cache.stats["ttl_seconds"] == 600

    @pytest.mark.asyncio
    async def test_same_key_overwrites(self):
        cache = ResponseCache(max_size=10, ttl_seconds=300)
        msgs = [Message(role="user", content="hi")]
        await cache.put("p", "m", msgs, 0.0, 100, _make_response(content="first"))
        await cache.put("p", "m", msgs, 0.0, 100, _make_response(content="second"))

        hit = await cache.get("p", "m", msgs, 0.0, 100)
        assert hit.content == "second"
        assert len(cache._store) == 1


# ---------------------------------------------------------------------------
# Engine._get_fallback_chain
# ---------------------------------------------------------------------------


class TestFallbackChain:

    def _make_engine_with_config(self, fallback_order, enabled_providers):
        """Create a minimal Engine mock with config for fallback chain testing."""
        with patch("nvh.core.engine.Engine.__init__", return_value=None):
            from nvh.core.engine import Engine
            engine = Engine.__new__(Engine)

        # Minimal config stubs
        engine.config = MagicMock()
        engine.config.council.fallback_order = fallback_order
        engine.registry = MagicMock()
        engine.registry.list_enabled.return_value = enabled_providers
        return engine

    def test_primary_is_first(self):
        engine = self._make_engine_with_config(["openai", "groq"], ["groq", "openai"])
        chain = engine._get_fallback_chain("anthropic")
        assert chain[0] == "anthropic"

    def test_fallback_order_preserved(self):
        engine = self._make_engine_with_config(["openai", "groq"], ["groq", "openai", "anthropic"])
        chain = engine._get_fallback_chain("anthropic")
        assert chain == ["anthropic", "openai", "groq"]

    def test_remaining_enabled_appended(self):
        engine = self._make_engine_with_config(["openai"], ["groq", "openai", "google"])
        chain = engine._get_fallback_chain("openai")
        # openai first (primary), then fallback_order has openai (skip), then remaining enabled
        assert chain[0] == "openai"
        assert "groq" in chain
        assert "google" in chain

    def test_no_duplicates(self):
        engine = self._make_engine_with_config(["groq", "openai"], ["groq", "openai"])
        chain = engine._get_fallback_chain("groq")
        assert len(chain) == len(set(chain))


# ---------------------------------------------------------------------------
# Engine._check_budget
# ---------------------------------------------------------------------------


class TestBudgetCheck:

    @pytest.mark.asyncio
    async def test_budget_exceeded_raises(self):
        with patch("nvh.core.engine.Engine.__init__", return_value=None):
            from nvh.core.engine import Engine
            engine = Engine.__new__(Engine)

        engine._budget_lock = asyncio.Lock()
        engine.config = MagicMock()
        engine.config.budget.daily_limit_usd = Decimal("1.00")
        engine.config.budget.monthly_limit_usd = Decimal("0")
        engine.config.budget.hard_stop = True
        engine.config.budget.alert_threshold = 0
        engine.webhooks = MagicMock()

        with patch("nvh.storage.repository.get_spend", new_callable=AsyncMock, return_value=Decimal("2.00")):
            with pytest.raises(BudgetExceededError):
                await engine._check_budget()

    @pytest.mark.asyncio
    async def test_budget_ok_does_not_raise(self):
        with patch("nvh.core.engine.Engine.__init__", return_value=None):
            from nvh.core.engine import Engine
            engine = Engine.__new__(Engine)

        engine._budget_lock = asyncio.Lock()
        engine.config = MagicMock()
        engine.config.budget.daily_limit_usd = Decimal("10.00")
        engine.config.budget.monthly_limit_usd = Decimal("0")
        engine.config.budget.hard_stop = True
        engine.config.budget.alert_threshold = 0
        engine.webhooks = MagicMock()

        with patch("nvh.storage.repository.get_spend", new_callable=AsyncMock, return_value=Decimal("0.50")):
            # Should not raise
            await engine._check_budget()
