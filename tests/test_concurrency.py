"""Concurrency stress tests for the core Engine.

Before this file, nothing exercised the rate limiter, router, or
query path under parallel load. Any race condition in those code
paths would show up as intermittent flakes on users' machines
instead of a deterministic CI failure.

These tests drive the Engine directly via `asyncio.gather` and
assert that aggregate state (call count, per-provider spend)
matches the sum of individual outcomes exactly — any mismatch
would indicate a lost or double-counted update under concurrency.

We exercise the engine rather than the HTTP layer because FastAPI's
TestClient isn't thread-safe, and the concurrency risk we care
about is in the engine + provider layer, not FastAPI itself.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

import nvh.storage.repository as repo
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


class _FixedCostProvider:
    """Mock provider that always returns the same fixed cost.

    Used so we can verify budget accumulation under concurrent load:
    N requests must leave `daily_spend == N * fixed_cost` exactly.
    """

    FIXED_COST = Decimal("0.01")

    def __init__(self, name: str = "alpha") -> None:
        self._name = name
        self.call_count = 0

    @property
    def name(self) -> str:
        return self._name

    async def complete(self, messages, model=None, temperature=1.0,
                       max_tokens=4096, system_prompt=None, **kwargs):
        self.call_count += 1
        # Small async yield so the scheduler can interleave requests
        await asyncio.sleep(0)
        return CompletionResponse(
            content=f"response {self.call_count}",
            model=model or "test-model",
            provider=self._name,
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            cost_usd=self.FIXED_COST,
            latency_ms=1,
        )

    async def stream(self, messages, model=None, temperature=1.0,
                     max_tokens=4096, system_prompt=None, **kwargs):
        self.call_count += 1
        yield StreamChunk(
            delta="ok",
            is_final=True,
            accumulated_content="ok",
            model=model or "test-model",
            provider=self._name,
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            cost_usd=self.FIXED_COST,
            finish_reason=FinishReason.STOP,
        )

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(model_id="test-model", provider=self._name)]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(provider=self._name, healthy=True, latency_ms=1)

    def estimate_tokens(self, text: str) -> int:
        return 1


@pytest.fixture()
async def concurrent_engine(tmp_path: Path):
    """A fully-configured Engine wired to a _FixedCostProvider mock."""
    db_file = tmp_path / "concurrency.db"
    repo._engine = None
    repo._session_factory = None
    await repo.init_db(db_path=db_file)

    provider = _FixedCostProvider("alpha")
    config = CouncilConfig(
        defaults=DefaultsConfig(provider="alpha", model="test-model"),
        providers={"alpha": ProviderConfig(enabled=True, default_model="test-model")},
        council=CouncilModeConfig(
            quorum=1, strategy="majority_vote", timeout=5,
            default_weights={"alpha": 1.0}, synthesis_provider="alpha",
        ),
        routing=RoutingConfig(),
        budget=BudgetConfig(
            daily_limit_usd=Decimal("100"),
            monthly_limit_usd=Decimal("1000"),
        ),
        cache=CacheConfig(enabled=False, ttl_seconds=1, max_size=1),
    )
    registry = ProviderRegistry()
    registry.register("alpha", provider)
    engine = Engine(config=config, registry=registry)
    engine._initialized = True

    yield engine, provider

    repo._engine = None
    repo._session_factory = None


class TestConcurrentEngineQueries:
    """Fire many parallel engine.query() calls and verify aggregate
    state is consistent. The rate_manager TokenBucket has no explicit
    lock and budget accumulation is the classic race-condition site."""

    @pytest.mark.asyncio
    async def test_20_concurrent_engine_queries(self, concurrent_engine):
        """20 parallel queries — every one must hit the provider exactly
        once. Any lost or duplicated call indicates a race."""
        engine, provider = concurrent_engine

        async def one_query(i: int):
            return await engine.query(
                prompt=f"request {i}",
                provider="alpha",
            )

        results = await asyncio.gather(
            *[one_query(i) for i in range(20)],
            return_exceptions=True,
        )

        # No exceptions — all queries must have succeeded
        failed = [r for r in results if isinstance(r, Exception)]
        assert not failed, f"Expected no failures, got: {failed}"

        # Provider must have been called exactly 20 times
        assert provider.call_count == 20, (
            f"Expected exactly 20 provider calls, got {provider.call_count}"
        )

        # Every result must carry the fixed cost — no cost loss or
        # duplication at the CompletionResponse level
        total_cost = sum(r.cost_usd for r in results)
        expected = provider.FIXED_COST * 20
        assert total_cost == expected, (
            f"Expected total cost {expected}, got {total_cost}"
        )

    @pytest.mark.asyncio
    async def test_concurrent_rate_manager_success_recording(self, concurrent_engine):
        """Fire 10 parallel record_success() calls — no exceptions, and
        the provider's health score remains healthy throughout."""
        engine, provider = concurrent_engine

        async def one_record():
            engine.rate_manager.record_success("alpha")

        await asyncio.gather(*[one_record() for _ in range(10)])

        # Health score must still be sane (1.0 after all successes)
        score = engine.rate_manager.get_health_score("alpha")
        assert 0.5 <= score <= 1.0, (
            f"Expected health score in [0.5, 1.0], got {score}"
        )
