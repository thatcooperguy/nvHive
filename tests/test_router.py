"""Tests for the routing engine and task classifier."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from nvh.config.settings import (
    CouncilConfig,
    CouncilModeConfig,
    DefaultsConfig,
    ProviderConfig,
    RoutingConfig,
    RoutingRule,
)
from nvh.core.rate_limiter import ProviderRateManager
from nvh.core.router import (
    ClassificationResult,
    RoutingEngine,
    classify_task,
)
from nvh.providers.base import (
    CompletionResponse,
    FinishReason,
    ModelInfo,
    TaskType,
    Usage,
)
from nvh.providers.registry import ProviderRegistry


class TestTaskClassifier:
    def test_code_generation(self):
        result = classify_task("Write a Python function to sort a list")
        assert result.task_type == TaskType.CODE_GENERATION
        assert result.confidence > 0

    def test_code_debug(self):
        result = classify_task("I'm getting a TypeError exception, can you fix this bug?")
        assert result.task_type == TaskType.CODE_DEBUG

    def test_math(self):
        result = classify_task("Solve this equation: 2x + 5 = 15")
        assert result.task_type == TaskType.MATH

    def test_creative_writing(self):
        result = classify_task("Write a short story about a dragon")
        assert result.task_type == TaskType.CREATIVE_WRITING

    def test_summarization(self):
        result = classify_task("Summarize this article for me")
        assert result.task_type == TaskType.SUMMARIZATION

    def test_translation(self):
        result = classify_task("Translate this paragraph to Spanish")
        assert result.task_type == TaskType.TRANSLATION

    def test_question(self):
        result = classify_task("What is the capital of France?")
        assert result.task_type == TaskType.QUESTION_ANSWERING

    def test_conversation(self):
        result = classify_task("Hello, how are you?")
        assert result.task_type == TaskType.CONVERSATION

    def test_multimodal(self):
        result = classify_task("Look at this image and tell me what's in the photo")
        assert result.task_type == TaskType.MULTIMODAL

    def test_returns_all_scores(self):
        result = classify_task("Write and debug a Python sort function")
        assert len(result.all_scores) > 0
        assert all(0 <= s <= 1 for s in result.all_scores.values())

    def test_fallback_for_ambiguous(self):
        result = classify_task("hmm")
        assert result.confidence < 1.0

    def test_classify_code_generation(self) -> None:
        result = classify_task("Write a Python function to sort a list")
        assert result.task_type == TaskType.CODE_GENERATION

    def test_classify_math(self) -> None:
        result = classify_task("Calculate the integral of x squared from 0 to 5")
        assert result.task_type == TaskType.MATH

    def test_classify_conversation(self) -> None:
        result = classify_task("Hello how are you doing today")
        assert result.task_type == TaskType.CONVERSATION

    def test_classify_debug(self) -> None:
        result = classify_task("Fix this bug in my code, I'm getting a TypeError")
        assert result.task_type == TaskType.CODE_DEBUG

    def test_classify_summarization(self) -> None:
        result = classify_task("Summarize this article for me in three sentences")
        assert result.task_type == TaskType.SUMMARIZATION

    def test_classify_translation(self) -> None:
        result = classify_task("Translate this paragraph to Spanish")
        assert result.task_type == TaskType.TRANSLATION

    def test_classify_empty_falls_back(self) -> None:
        result = classify_task("")
        assert isinstance(result, ClassificationResult)
        assert result.task_type is not None

    def test_classify_ambiguous_question(self) -> None:
        result = classify_task("what?")
        assert isinstance(result, ClassificationResult)


# ---------------------------------------------------------------------------
# RoutingEngine with a mocked registry
# ---------------------------------------------------------------------------


def _make_engine(
    providers: dict[str, ProviderConfig] | None = None,
    enabled: list[str] | None = None,
    models: list[ModelInfo] | None = None,
    health: float = 1.0,
) -> RoutingEngine:
    """Build a RoutingEngine with mocked registry and rate manager."""
    config = CouncilConfig(
        providers=providers or {},
        defaults=DefaultsConfig(provider="openai", model="gpt-4o"),
    )
    registry = MagicMock(spec=ProviderRegistry)
    registry.list_enabled.return_value = enabled or []
    registry.has.side_effect = lambda n: n in (enabled or [])
    registry.get_models_for_provider.return_value = models or []
    registry.get_model_info.return_value = None

    rate_mgr = MagicMock()
    rate_mgr.get_health_score.return_value = health

    return RoutingEngine(config, registry, rate_mgr)


def _model(
    model_id: str = "test-model",
    provider: str = "openai",
    cap: float = 0.8,
    cost: Decimal = Decimal("1"),
    latency: int = 500,
    context: int = 128000,
) -> ModelInfo:
    return ModelInfo(
        model_id=model_id,
        provider=provider,
        input_cost_per_1m_tokens=cost,
        output_cost_per_1m_tokens=cost,
        typical_latency_ms=latency,
        context_window=context,
        capability_scores={"code_generation": cap, "conversation": cap},
    )


class TestRoutingEngine:
    def test_provider_override(self) -> None:
        engine = _make_engine()
        decision = engine.route("hello", provider_override="anthropic")
        assert decision.provider == "anthropic"
        assert "override" in decision.reason.lower()

    def test_provider_override_with_model(self) -> None:
        engine = _make_engine()
        decision = engine.route(
            "hello",
            provider_override="anthropic",
            model_override="claude-3",
        )
        assert decision.provider == "anthropic"
        assert decision.model == "claude-3"

    def test_no_providers_available(self) -> None:
        engine = _make_engine(enabled=[])
        decision = engine.route("hello")
        assert decision.reason.startswith("No providers available")

    def test_model_override_used(self) -> None:
        m = _model()
        engine = _make_engine(
            enabled=["openai"],
            models=[m],
            providers={"openai": ProviderConfig(default_model="gpt-4o")},
        )
        decision = engine.route("Write code", model_override="gpt-4o-mini")
        assert decision.model == "gpt-4o-mini"

    def test_best_scored_model_is_selected(self) -> None:
        weak = _model(model_id="weak-chat", cap=0.2)
        strong = _model(model_id="strong-code", cap=0.95)
        engine = _make_engine(
            enabled=["openai"],
            models=[weak, strong],
            providers={"openai": ProviderConfig(default_model="weak-chat")},
        )

        decision = engine.route("Write a Python API endpoint")

        assert decision.provider == "openai"
        assert decision.model == "strong-code"

    def test_fallback_when_unhealthy(self) -> None:
        engine = _make_engine(enabled=["openai"], models=[], health=0.0)
        decision = engine.route("hello")
        assert "filtered out" in decision.reason.lower() or "default" in decision.reason.lower()

    def test_cheapest_strategy(self) -> None:
        m = _model(cost=Decimal("0"))
        engine = _make_engine(
            enabled=["openai"],
            models=[m],
            providers={"openai": ProviderConfig(default_model="gpt-4o")},
        )
        decision = engine.route("hello", strategy="cheapest")
        assert decision.provider == "openai"

    def test_fastest_strategy(self) -> None:
        m = _model(latency=100)
        engine = _make_engine(
            enabled=["openai"],
            models=[m],
            providers={"openai": ProviderConfig(default_model="gpt-4o")},
        )
        decision = engine.route("hello", strategy="fastest")
        assert decision.provider == "openai"

    def test_cost_score_free_model(self) -> None:
        m = _model(cost=Decimal("0"))
        engine = _make_engine()
        assert engine._cost_score(m) == 1.0

    def test_latency_score_instant(self) -> None:
        m = _model(latency=0)
        engine = _make_engine()
        assert engine._latency_score(m) == 1.0


# ---------------------------------------------------------------------------
# RoutingEngine with a real registry: health penalty, rules, learned scores
# ---------------------------------------------------------------------------


def _make_resp(content="hello", provider="mock", model="m1", cost="0"):
    return CompletionResponse(
        content=content,
        model=model,
        provider=provider,
        usage=Usage(input_tokens=10, output_tokens=20),
        cost_usd=Decimal(cost),
        latency_ms=100,
        finish_reason=FinishReason.STOP,
    )


def _mock_provider(name="mock"):
    p = AsyncMock()
    p.complete = AsyncMock(return_value=_make_resp(provider=name))
    return p


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


def _build_registry(*names) -> ProviderRegistry:
    reg = ProviderRegistry()
    for n in names:
        reg.register(n, _mock_provider(n))
    return reg


class TestRoutingEngineHealthAndRules:
    """Cover health penalty, custom rules, no-providers, and learned scores."""

    def _make_router(self, config=None, registry=None, rate_manager=None):
        cfg = config or _build_config()
        reg = registry or _build_registry("provA", "provB")
        rm = rate_manager or ProviderRateManager()
        return RoutingEngine(cfg, reg, rm)

    def test_route_with_custom_routing_rule_matches(self):
        """Custom routing rules matching task_type route to specified provider."""
        cfg = _build_config(
            routing=RoutingConfig(
                rules=[
                    RoutingRule(
                        match={"task_type": "code_generation"},
                        provider="provB",
                        model="mB-special",
                    ),
                ],
            ),
        )
        reg = _build_registry("provA", "provB")
        router = self._make_router(config=cfg, registry=reg)

        decision = router.route("Write a Python function to sort a list")

        assert decision.provider == "provB"
        assert decision.model == "mB-special"
        assert "routing rule" in decision.reason.lower() or "Matched" in decision.reason

    def test_route_with_all_providers_unhealthy_falls_back(self):
        """When all providers have health < 0.1, routing falls back to default."""
        cfg = _build_config()
        reg = _build_registry("provA", "provB")

        rm = MagicMock()
        rm.get_health_score = MagicMock(return_value=0.05)

        router = self._make_router(config=cfg, registry=reg, rate_manager=rm)
        decision = router.route("hello world")

        assert "filtered out" in decision.reason.lower() or "default" in decision.reason.lower()

    def test_route_with_health_penalty_applied(self):
        """Providers with low health get penalized in composite scoring."""
        cfg = _build_config()
        reg = _build_registry("provA", "provB")

        # Register models so scoring works
        reg._model_catalog["mA"] = ModelInfo(
            model_id="mA", provider="provA",
            capability_scores={"conversation": 0.9},
            input_cost_per_1m_tokens=Decimal("1"),
            output_cost_per_1m_tokens=Decimal("1"),
        )
        reg._model_catalog["mB"] = ModelInfo(
            model_id="mB", provider="provB",
            capability_scores={"conversation": 0.9},
            input_cost_per_1m_tokens=Decimal("1"),
            output_cost_per_1m_tokens=Decimal("1"),
        )

        rm = MagicMock()
        # provA has low health, provB has full health
        rm.get_health_score = MagicMock(side_effect=lambda p: 0.2 if p == "provA" else 1.0)

        router = self._make_router(config=cfg, registry=reg, rate_manager=rm)
        decision = router.route("hello there")

        # provB should win due to better health score
        assert decision.provider == "provB"

    def test_route_with_learned_scores_integration(self):
        """Learned scores influence provider selection."""
        cfg = _build_config()
        reg = _build_registry("provA", "provB")

        reg._model_catalog["mA"] = ModelInfo(
            model_id="mA", provider="provA",
            capability_scores={"conversation": 0.5},
            input_cost_per_1m_tokens=Decimal("1"),
            output_cost_per_1m_tokens=Decimal("1"),
        )
        reg._model_catalog["mB"] = ModelInfo(
            model_id="mB", provider="provB",
            capability_scores={"conversation": 0.5},
            input_cost_per_1m_tokens=Decimal("1"),
            output_cost_per_1m_tokens=Decimal("1"),
        )

        rm = MagicMock()
        rm.get_health_score = MagicMock(return_value=0.8)

        router = self._make_router(config=cfg, registry=reg, rate_manager=rm)

        # Set learned score that boosts provB for conversation
        learned = MagicMock()
        learned.sample_count = 10
        learned.learned_capability = 0.95
        router.set_learned_scores({
            ("provB", "mB", "conversation"): learned,
        })

        decision = router.route("hello how are you")
        # provB should be boosted
        assert decision.scores.get("capability", 0) > 0 or decision.provider == "provB"

    def test_route_no_providers_available(self):
        """When registry is empty, falls back to default provider."""
        cfg = _build_config()
        reg = ProviderRegistry()  # empty

        router = self._make_router(config=cfg, registry=reg)
        decision = router.route("hello")

        assert "No providers" in decision.reason or "default" in decision.reason.lower()

    def test_capability_score_aggregation_across_task_types(self):
        """Models with multiple capability scores are evaluated per-task."""
        cfg = _build_config()
        reg = _build_registry("provA")

        reg._model_catalog["mA"] = ModelInfo(
            model_id="mA", provider="provA",
            capability_scores={
                "code_generation": 0.95,
                "conversation": 0.3,
                "math": 0.8,
            },
            input_cost_per_1m_tokens=Decimal("1"),
            output_cost_per_1m_tokens=Decimal("1"),
        )

        rm = MagicMock()
        rm.get_health_score = MagicMock(return_value=1.0)

        router = self._make_router(config=cfg, registry=reg, rate_manager=rm)

        code_decision = router.route("Write a Python function to sort items")
        conv_decision = router.route("Hello how are you today?")

        # Both should route to provA (only provider) but with different scores
        assert code_decision.provider == "provA"
        assert conv_decision.provider == "provA"


class TestRouterScoring:
    def test_route_returns_decision(self):
        config = CouncilConfig(
            defaults=DefaultsConfig(provider="alpha"),
            providers={"alpha": ProviderConfig(enabled=True, default_model="m")},
            council=CouncilModeConfig(quorum=1, timeout=5, default_weights={"alpha": 1.0}),
            routing=RoutingConfig(),
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
        config = CouncilConfig(
            defaults=DefaultsConfig(provider="alpha"),
            providers={"alpha": ProviderConfig(enabled=True, default_model="m")},
            council=CouncilModeConfig(quorum=1, timeout=5, default_weights={"alpha": 1.0}),
            routing=RoutingConfig(),
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
# Scoring helpers on a two-provider router
# ---------------------------------------------------------------------------


def _rtr(rules=None):
    cfg = CouncilConfig(
        defaults=DefaultsConfig(provider="openai", model="gpt-4o"),
        providers={"openai": ProviderConfig(default_model="gpt-4o")},
        routing=RoutingConfig(rules=rules or []),
    )
    reg = ProviderRegistry()
    mp = MagicMock()
    reg.register("openai", mp)
    reg.register("groq", mp)
    return RoutingEngine(cfg, reg, ProviderRateManager())


def test_rule_match():
    r = _rtr([RoutingRule(match={"task_type": "code_generation"},
                          provider="groq", model="llama")])
    assert r.route("Write a Python sort function").provider == "groq"


def test_cost_expensive():
    m = ModelInfo(model_id="x", provider="x",
                  input_cost_per_1m_tokens=Decimal("50"),
                  output_cost_per_1m_tokens=Decimal("50"))
    assert _rtr()._cost_score(m) == 0.0


@pytest.mark.parametrize("ms,exp", [(6000, 0.0), (0, 1.0)])
def test_latency_score(ms, exp):
    assert _rtr()._latency_score(
        ModelInfo(model_id="x", provider="x", typical_latency_ms=ms)) == exp
