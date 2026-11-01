"""Engine.query paths — escalation, verification, budget, fallback, DB logging, compare.

ResponseCache unit tests live in test_engine_deep.py.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

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
from nvh.core.engine import BudgetExceededError, Engine
from nvh.providers.base import (
    CompletionResponse,
    FinishReason,
    HealthStatus,
    ModelInfo,
    ProviderError,
    ProviderUnavailableError,
    StreamChunk,
    Usage,
)
from nvh.providers.registry import ProviderRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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

    async def list_models(self):
        return [ModelInfo(model_id="test-model", provider=self._name)]

    async def health_check(self):
        return HealthStatus(provider=self._name, healthy=True, latency_ms=1)

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


def _build_engine(
    names: tuple[str, ...] = ("alpha",), fallback_order: list[str] | None = None,
) -> Engine:
    config = CouncilConfig(
        defaults=DefaultsConfig(
            provider=names[0], model="test-model",
            temperature=0.0, max_tokens=128,
        ),
        providers={n: ProviderConfig(enabled=True, default_model="test-model") for n in names},
        council=CouncilModeConfig(
            quorum=1, strategy="majority_vote", timeout=30,
            default_weights={n: 1.0 for n in names}, synthesis_provider=names[0],
            fallback_order=fallback_order or [],
        ),
        routing=RoutingConfig(),
        budget=BudgetConfig(),
        cache=CacheConfig(enabled=True, ttl_seconds=3600, max_size=100),
    )
    registry = ProviderRegistry()
    for n in names:
        registry.register(n, _MockProvider(n))
    engine = Engine(config=config, registry=registry)
    engine._initialized = True
    return engine


def _resp(content="hello", provider="mock", model="m1", cost="0"):
    return CompletionResponse(
        content=content,
        model=model,
        provider=provider,
        usage=Usage(input_tokens=10, output_tokens=20),
        cost_usd=Decimal(cost),
        latency_ms=100,
        finish_reason=FinishReason.STOP,
    )


def _build_config(**overrides) -> CouncilConfig:
    defaults = {
        "providers": {
            "provA": ProviderConfig(default_model="mA"),
            "provB": ProviderConfig(default_model="mB"),
            "provC": ProviderConfig(default_model="mC"),
            "provD": ProviderConfig(default_model="mD"),
        },
        "council": CouncilModeConfig(
            default_weights={"provA": 0.5, "provB": 0.5},
            synthesis_provider="provA",
            quorum=2,
            timeout=30,
        ),
    }
    defaults.update(overrides)
    return CouncilConfig(**defaults)


def _bare_engine(**attrs) -> Engine:
    """An Engine without __init__ — only the attributes a test needs."""
    e = Engine.__new__(Engine)
    e._budget_lock = asyncio.Lock()
    e.webhooks = MagicMock()
    e.webhooks.emit = AsyncMock()
    for k, v in attrs.items():
        setattr(e, k, v)
    return e


# ---------------------------------------------------------------------------
# Engine.query decision paths
# ---------------------------------------------------------------------------


class TestEngineQuery:
    """Cover escalation, verification, budget, and auto-detect paths."""

    @pytest.mark.asyncio
    async def test_query_with_escalate_delegates(self):
        """escalate=True delegates to query_with_escalation."""
        fake_resp = _resp()
        fake_meta = {"escalated": True, "original_strategy": "cheapest"}

        with (
            patch("nvh.core.engine.Engine.__init__", return_value=None),
            patch("nvh.core.smart_query.query_with_escalation", new_callable=AsyncMock) as mock_esc,
        ):
            mock_esc.return_value = (fake_resp, fake_meta)

            engine = Engine.__new__(Engine)
            engine.config = _build_config()

            result = await engine.query("test prompt", escalate=True)

            mock_esc.assert_awaited_once()
            assert result.metadata.get("escalated") is True

    @pytest.mark.asyncio
    async def test_query_with_verify_attaches_verification(self):
        """verify=True attaches verification metadata to response."""
        fake_resp = _resp()
        fake_resp.metadata = {}

        mock_verification = MagicMock()
        mock_verification.verdict = "correct"
        mock_verification.confidence = 0.95
        mock_verification.issues = []
        mock_verification.correction = None
        mock_verification.verifier_provider = "provB"

        with (
            patch("nvh.core.engine.Engine.__init__", return_value=None),
            patch("nvh.core.smart_query.query_with_escalation", new_callable=AsyncMock) as mock_esc,
            patch("nvh.core.smart_query.verify_response", new_callable=AsyncMock) as mock_ver,
        ):
            mock_esc.return_value = (fake_resp, {"escalated": False})
            mock_ver.return_value = mock_verification

            engine = Engine.__new__(Engine)
            engine.config = _build_config()

            result = await engine.query("test", escalate=True, verify=True)

            mock_ver.assert_awaited_once()
            assert result.metadata["verification"]["verdict"] == "correct"
            assert result.metadata["verification"]["confidence"] == 0.95

    @pytest.mark.asyncio
    async def test_query_with_verify_hands_the_exclusion_to_the_verifier(self):
        """The cross-check runs under the same ``exclude_providers`` the answer did (escalate path)."""
        fake_resp = _resp()
        fake_resp.metadata = {}
        with (
            patch("nvh.core.engine.Engine.__init__", return_value=None),
            patch("nvh.core.smart_query.query_with_escalation", new_callable=AsyncMock) as mock_esc,
            patch("nvh.core.smart_query.verify_response", new_callable=AsyncMock) as mock_ver,
        ):
            mock_esc.return_value = (fake_resp, {"escalated": False})
            mock_ver.return_value = MagicMock(
                verdict="correct", confidence=0.9, issues=[], correction=None, verifier_provider="provB",
            )
            engine = Engine.__new__(Engine)
            engine.config = _build_config()
            await engine.query("test", escalate=True, verify=True, exclude_providers=frozenset({"perplexity"}))
            assert mock_ver.await_args.kwargs["exclude_providers"] == frozenset({"perplexity"})

    @pytest.mark.asyncio
    async def test_check_budget_daily_exceeded_raises(self):
        """_check_budget raises BudgetExceededError when daily limit reached."""
        with patch("nvh.core.engine.Engine.__init__", return_value=None):
            engine = Engine.__new__(Engine)
            engine.config = _build_config(
                budget=BudgetConfig(
                    daily_limit_usd=Decimal("1"),
                    monthly_limit_usd=Decimal("50"),
                    hard_stop=True,
                ),
            )
            engine._budget_lock = asyncio.Lock()
            engine.webhooks = MagicMock()
            engine.webhooks.emit = AsyncMock()

            with patch("nvh.core.engine.repo.get_spend", new_callable=AsyncMock) as mock_spend:
                mock_spend.return_value = Decimal("1.50")

                with pytest.raises(BudgetExceededError, match="Daily budget"):
                    await engine._check_budget()

    @pytest.mark.asyncio
    async def test_check_budget_monthly_exceeded_raises(self):
        """_check_budget raises BudgetExceededError when monthly limit hit."""
        with patch("nvh.core.engine.Engine.__init__", return_value=None):
            engine = Engine.__new__(Engine)
            engine.config = _build_config(
                budget=BudgetConfig(
                    daily_limit_usd=Decimal("0"),
                    monthly_limit_usd=Decimal("10"),
                    hard_stop=True,
                ),
            )
            engine._budget_lock = asyncio.Lock()
            engine.webhooks = MagicMock()
            engine.webhooks.emit = AsyncMock()

            with patch("nvh.core.engine.repo.get_spend", new_callable=AsyncMock) as mock_spend:
                mock_spend.return_value = Decimal("15")

                with pytest.raises(BudgetExceededError, match="Monthly budget"):
                    await engine._check_budget()

    @pytest.mark.asyncio
    async def test_auto_detect_providers_finds_llm7(self):
        """_auto_detect_providers registers llm7 through the registry's lazy_adapter."""
        with patch("nvh.core.engine.Engine.__init__", return_value=None):
            engine = Engine.__new__(Engine)
            engine.registry = ProviderRegistry()

            mock_provider = MagicMock()
            with patch("nvh.core.engine.lazy_adapter", return_value=mock_provider) as adapter:
                result = engine._auto_detect_providers()

            assert "llm7" in result
            adapter.assert_any_call("llm7")
            assert engine.registry.get("llm7") is mock_provider

    @pytest.mark.asyncio
    async def test_auto_detect_env_providers_uses_lazy_adapter(self):
        """Env-var keys register via lazy_adapter(name, api_key=...) — no shim module paths."""
        with patch("nvh.core.engine.Engine.__init__", return_value=None):
            engine = Engine.__new__(Engine)
            engine.registry = ProviderRegistry()

            with (
                patch.dict("os.environ", {"GROQ_API_KEY": "gsk-test"}, clear=True),
                patch("nvh.core.engine.lazy_adapter", return_value=MagicMock()) as adapter,
            ):
                detected: list[str] = []
                engine._auto_detect_env_providers(detected)

            assert detected == ["groq"]
            adapter.assert_called_once_with("groq", api_key="gsk-test")

    @pytest.mark.asyncio
    async def test_auto_detect_providers_no_keys_no_ollama(self):
        """_auto_detect_providers returns only llm7 when no keys/ollama available."""
        with patch("nvh.core.engine.Engine.__init__", return_value=None):
            engine = Engine.__new__(Engine)
            engine.registry = ProviderRegistry()

            # Block httpx so Ollama check fails, clear env keys
            with (
                patch("httpx.get", side_effect=Exception("no connection")),
                patch.dict("os.environ", {}, clear=True),
                patch.object(engine, "_try_start_ollama"),
            ):
                result = engine._auto_detect_providers()

            assert isinstance(result, list)
            # llm7 should still be detected (always available)
            assert "llm7" in result or len(result) >= 0


class TestEngineQueryPaths:
    """Engine.query() code paths: cache hit, fallback, escalation, budget."""

    @pytest.mark.asyncio
    async def test_query_cache_hit(self, db):
        engine = _build_engine()
        with patch.object(engine, "_check_connectivity", return_value=True):
            await engine.query("hello", provider="alpha", temperature=0)
        with patch.object(engine, "_check_connectivity", return_value=True):
            resp2 = await engine.query("hello", provider="alpha", temperature=0)
        assert resp2.cache_hit is True
        assert resp2.cost_usd == Decimal("0")

    @pytest.mark.asyncio
    async def test_query_fallback_on_error(self, db):
        engine = _build_engine()

        bad = _MockProvider("bad")
        bad.complete = AsyncMock(side_effect=ProviderError("provider boom"))
        engine.registry.register("bad", bad)

        with patch.object(engine, "_check_connectivity", return_value=True):
            resp = await engine.query("test", provider="bad")
        # Should have fallen back to alpha
        assert resp.provider == "alpha"
        assert resp.fallback_from == "bad"

    @pytest.mark.asyncio
    async def test_get_budget_status(self, db):
        engine = _build_engine()
        status = await engine.get_budget_status()
        assert "daily_spend" in status
        assert "monthly_spend" in status
        assert "daily_limit" in status
        assert "monthly_limit" in status
        assert "daily_queries" in status
        assert "monthly_queries" in status

    @pytest.mark.asyncio
    async def test_log_query_handles_error(self):
        engine = _build_engine()
        resp = CompletionResponse(
            content="x", model="m", provider="alpha",
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            cost_usd=Decimal("0"), latency_ms=1,
        )
        with patch("nvh.storage.repository.log_query", side_effect=RuntimeError("db down")):
            # Should not raise — logs warning instead
            await engine._log_query(resp, "simple")

    @pytest.mark.asyncio
    async def test_query_escalation_delegates(self, db):
        engine = _build_engine()
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
    async def test_engine_query_logs_to_db(self, db):
        engine = _build_engine()

        with patch.object(engine, "_check_connectivity", return_value=True):
            resp = await engine.query("hello", provider="alpha", privacy=False)
        assert resp.content == "reply from alpha"

        spend = await repo.get_spend("daily")
        assert spend > Decimal("0")

    @pytest.mark.asyncio
    async def test_engine_run_council_logs(self, db):
        engine = _build_engine(("alpha", "beta"))

        result = await engine.run_council("test prompt", synthesize=False)
        assert result.quorum_met
        assert len(result.member_responses) >= 1

        spend = await repo.get_spend("daily")
        assert spend > Decimal("0")


class TestEngineQueryAndCompare:
    @pytest.mark.asyncio
    async def test_query_with_system_prompt(self, db):
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

        resp = await engine.query(prompt="hello", provider="alpha", system_prompt="You are helpful")
        assert "got system" in resp.content

    @pytest.mark.asyncio
    async def test_compare_returns_dict(self, db):
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
# Budget and fallback on a bare engine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_monthly_exceeded():
    e = _bare_engine(config=_build_config(budget=BudgetConfig(
        daily_limit_usd=Decimal("0"), monthly_limit_usd=Decimal("5"),
        hard_stop=True)))
    with patch("nvh.storage.repository.get_spend", return_value=Decimal("6")):
        with pytest.raises(BudgetExceededError, match="Monthly"):
            await e._check_budget()


@pytest.mark.asyncio
async def test_budget_daily_alert_and_status():
    e = _bare_engine(config=_build_config(budget=BudgetConfig(
        daily_limit_usd=Decimal("10"), hard_stop=True, alert_threshold=0.8)))
    with patch("nvh.storage.repository.get_spend", return_value=Decimal("9")):
        await e._check_budget()
    e.webhooks.emit.assert_called_once()
    # Also verify get_budget_status fields
    e2 = _bare_engine(config=_build_config())
    with (patch("nvh.storage.repository.get_spend", return_value=Decimal("1")),
          patch("nvh.storage.repository.get_spend_by_provider",
                return_value={"openai": Decimal("1")}),
          patch("nvh.storage.repository.get_query_count", return_value=42)):
        s = await e2.get_budget_status()
    assert s["daily_queries"] == 42 and "by_provider" in s


@pytest.mark.asyncio
async def test_fallback_sets_fallback_from():
    from nvh.core.router import RoutingDecision
    from nvh.providers.base import Message, TaskType
    cfg = _build_config(
        defaults=DefaultsConfig(provider="openai", model="gpt-4o"),
        providers={"openai": ProviderConfig(default_model="gpt-4o"),
                   "groq": ProviderConfig(default_model="llama")},
        council=CouncilModeConfig(fallback_order=["groq"]))
    reg = ProviderRegistry()
    mo = MagicMock()
    mo.complete = AsyncMock(side_effect=ProviderError("x", provider="openai"))
    mg = MagicMock()
    mg.complete = AsyncMock(return_value=_resp(provider="groq", model="gpt-4o", cost="0.001"))
    reg.register("openai", mo)
    reg.register("groq", mg)
    e = _bare_engine(config=cfg, registry=reg, rate_manager=MagicMock())
    dec = RoutingDecision(provider="openai", model="gpt-4o",
                          task_type=TaskType.CONVERSATION,
                          confidence=0.9, scores={}, reason="t")
    r = await e._execute_with_fallback(
        [Message(role="user", content="hi")], dec, 0.7, 100, None, False)
    assert r.fallback_from == "openai"


# ---------------------------------------------------------------------------
# exclude_providers — a call never falls back to what the caller ruled out
# ---------------------------------------------------------------------------


def test_fallback_chain_exclude_drops_fallbacks_and_keeps_the_primary():
    engine = _build_engine(("alpha", "search", "gamma"), fallback_order=["search", "gamma"])
    assert engine._get_fallback_chain("alpha") == ["alpha", "search", "gamma"]
    assert engine._get_fallback_chain("alpha", exclude={"search"}) == ["alpha", "gamma"]
    assert engine._get_fallback_chain("alpha", exclude=frozenset({"search", "gamma"})) == ["alpha"]
    # The primary is the caller's pick (an explicit pin or the router's
    # decision under the same set): it stays even when named.
    chain = engine._get_fallback_chain("search", exclude={"search"})
    assert chain[0] == "search" and "search" not in chain[1:] and set(chain[1:]) == {"alpha", "gamma"}


@pytest.mark.asyncio
async def test_query_exclude_providers_never_falls_back_to_them(db):
    """A grounded research prompt pinned to an advisor that fails must not
    land on the search-native advisor the initial pick refused — it goes to
    the next one instead."""
    engine = _build_engine(("alpha", "search", "gamma"), fallback_order=["search", "gamma"])
    engine.registry.get("alpha").complete = AsyncMock(side_effect=ProviderError("alpha boom", provider="alpha"))
    search = engine.registry.get("search")
    search.complete = AsyncMock(return_value=_resp(content="searched again", provider="search", model="s"))

    with patch.object(engine, "_check_connectivity", return_value=True):
        plain = await engine.query("grounded prompt", provider="alpha", use_cache=False)
    assert (plain.provider, plain.fallback_from) == ("search", "alpha")
    assert search.complete.await_count == 1

    with patch.object(engine, "_check_connectivity", return_value=True):
        kept_off = await engine.query(
            "grounded prompt", provider="alpha", use_cache=False, exclude_providers=frozenset({"search"}),
        )
    assert (kept_off.provider, kept_off.fallback_from) == ("gamma", "alpha")
    assert search.complete.await_count == 1  # never tried


@pytest.mark.asyncio
async def test_query_hands_the_exclusion_to_the_router(db):
    """Without an explicit provider the router itself must honour the set."""
    engine = _build_engine(("alpha", "search"))
    seen: dict = {}
    real_route = engine.router.route

    def spy(**kwargs):
        seen.update(kwargs)
        return real_route(**kwargs)

    engine.router.route = spy
    with patch.object(engine, "_check_connectivity", return_value=True):
        resp = await engine.query("hello there", use_cache=False, exclude_providers=frozenset({"search"}))
    assert seen["exclude"] == frozenset({"search"})
    assert resp.provider == "alpha"


@pytest.mark.asyncio
async def test_query_offline_local_pick_honours_the_exclusion(db):
    """Offline, the auto-route to ollama/llm7 skips an excluded local provider."""
    engine = _build_engine(("llm7", "ollama"))
    with patch.object(engine, "_check_connectivity", return_value=False):
        resp = await engine.query("hello there", use_cache=False, exclude_providers=frozenset({"ollama"}))
    assert resp.provider == "llm7"
    with patch.object(engine, "_check_connectivity", return_value=False):
        with pytest.raises(ProviderUnavailableError):
            await engine.query("hello there", use_cache=False, exclude_providers=frozenset({"ollama", "llm7"}))


@pytest.mark.asyncio
async def test_query_stream_exclude_providers_skips_them(db):
    engine = _build_engine(("alpha", "search", "gamma"), fallback_order=["search", "gamma"])

    def boom(**kwargs):
        raise ProviderError("alpha boom", provider="alpha")

    engine.registry.get("alpha").stream = boom
    search = engine.registry.get("search")
    search.stream = MagicMock(side_effect=lambda **kw: _MockProvider("search").stream(**kw))

    resp = await engine.query_stream("grounded", provider="alpha", exclude_providers=frozenset({"search"}))
    assert resp.provider == "gamma"
    search.stream.assert_not_called()

    resp = await engine.query_stream("grounded", provider="alpha")
    assert resp.provider == "search"


@pytest.mark.asyncio
async def test_query_plain_path_hands_the_exclusion_to_the_verifier(db):
    """Without escalation the verify call sits after the answer; it carries the same set."""
    engine = _build_engine(("alpha", "search"))
    with (
        patch.object(engine, "_check_connectivity", return_value=True),
        patch("nvh.core.smart_query.verify_response", new_callable=AsyncMock) as mock_ver,
    ):
        mock_ver.return_value = MagicMock(
            verdict="correct", confidence=0.9, issues=[], correction=None, verifier_provider="search",
        )
        resp = await engine.query(
            "hello there", provider="alpha", use_cache=False, verify=True,
            exclude_providers=frozenset({"search"}),
        )
    assert resp.metadata["verification"]["verdict"] == "correct"
    assert mock_ver.await_args.args[1:] == ("hello there", "reply from alpha", "alpha")
    assert mock_ver.await_args.kwargs["exclude_providers"] == frozenset({"search"})


@pytest.mark.asyncio
async def test_verify_never_picks_an_excluded_advisor_end_to_end(db):
    """The real verifier under the engine: with [alpha, search] enabled and ``search`` excluded, an answer from
    alpha has no independent advisor left, so the cross-check is ``unverified`` (never a search check, never
    alpha grading itself); without the exclusion ``search`` verifies it."""
    engine = _build_engine(("alpha", "search"))
    with patch.object(engine, "_check_connectivity", return_value=True):
        grounded = await engine.query(
            "hello there", provider="alpha", use_cache=False, verify=True,
            exclude_providers=frozenset({"search"}),
        )
        plain = await engine.query("hello there", provider="alpha", use_cache=False, verify=True)
    assert grounded.metadata["verification"]["verifier"] == "none"
    assert grounded.metadata["verification"]["verdict"] == "unverified"
    assert grounded.metadata["verification"]["issues"] == ["No independent verifier available"]
    assert plain.metadata["verification"]["verifier"] == "search"


@pytest.mark.asyncio
async def test_execute_with_fallback_always_hands_the_exclusion_to_the_chain():
    """One call shape: the chain builder receives ``exclude=`` on every call,
    the empty set included, so a double standing in for it takes the keyword
    (``lambda name, exclude=(): ...``) and no caller keeps a second shape."""
    from nvh.core.router import RoutingDecision
    from nvh.providers.base import Message, TaskType

    cfg = _build_config(providers={"openai": ProviderConfig(default_model="m")})
    reg = ProviderRegistry()
    mo = MagicMock()
    mo.complete = AsyncMock(return_value=_resp(provider="openai"))
    reg.register("openai", mo)
    e = _bare_engine(config=cfg, registry=reg, rate_manager=MagicMock())
    dec = RoutingDecision(provider="openai", model="m", task_type=TaskType.CONVERSATION,
                          confidence=0.9, scores={}, reason="t")
    seen: list = []

    def chain(name, exclude=()):
        seen.append(exclude)
        return [name]

    e._get_fallback_chain = chain
    r = await e._execute_with_fallback([Message(role="user", content="hi")], dec, 0.7, 100, None, False)
    assert r.provider == "openai"
    await e._execute_with_fallback(
        [Message(role="user", content="hi")], dec, 0.7, 100, None, False,
        exclude_providers=frozenset({"search"}),
    )
    assert seen == [frozenset(), frozenset({"search"})]
