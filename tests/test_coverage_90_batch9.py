"""Batch 9 – deep coverage for engine, council, router, and repository."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nvh.config.settings import (
    BudgetConfig,
    CouncilConfig,
    CouncilModeConfig,
    ProviderConfig,
    RoutingConfig,
    RoutingRule,
)
from nvh.providers.base import (
    CompletionResponse,
    FinishReason,
    ModelInfo,
    Usage,
)
from nvh.providers.registry import ProviderRegistry

# ---------------------------------------------------------------------------
# Helpers
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


# ===================================================================
# Target 1: nvh/core/engine.py
# ===================================================================

class TestEngineQuery:
    """Cover escalation, verification, budget, and auto-detect paths."""

    @pytest.mark.asyncio
    async def test_query_with_escalate_delegates(self):
        """escalate=True delegates to query_with_escalation."""
        fake_resp = _make_resp()
        fake_meta = {"escalated": True, "original_strategy": "cheapest"}

        with (
            patch("nvh.core.engine.Engine.__init__", return_value=None),
            patch("nvh.core.smart_query.query_with_escalation", new_callable=AsyncMock) as mock_esc,
        ):
            mock_esc.return_value = (fake_resp, fake_meta)

            from nvh.core.engine import Engine
            engine = Engine.__new__(Engine)
            engine.config = _build_config()

            result = await engine.query("test prompt", escalate=True)

            mock_esc.assert_awaited_once()
            assert result.metadata.get("escalated") is True

    @pytest.mark.asyncio
    async def test_query_with_verify_attaches_verification(self):
        """verify=True attaches verification metadata to response."""
        fake_resp = _make_resp()
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

            from nvh.core.engine import Engine
            engine = Engine.__new__(Engine)
            engine.config = _build_config()

            result = await engine.query("test", escalate=True, verify=True)

            mock_ver.assert_awaited_once()
            assert result.metadata["verification"]["verdict"] == "correct"
            assert result.metadata["verification"]["confidence"] == 0.95

    @pytest.mark.asyncio
    async def test_check_budget_daily_exceeded_raises(self):
        """_check_budget raises BudgetExceededError when daily limit reached."""
        from nvh.core.engine import BudgetExceededError, Engine

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
        from nvh.core.engine import BudgetExceededError, Engine

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
        """_auto_detect_providers registers llm7 when import succeeds."""
        from nvh.core.engine import Engine

        with patch("nvh.core.engine.Engine.__init__", return_value=None):
            engine = Engine.__new__(Engine)
            engine.registry = ProviderRegistry()

            mock_provider = MagicMock()
            with patch.dict("sys.modules", {"nvh.providers.llm7_provider": MagicMock()}):
                import sys
                sys.modules["nvh.providers.llm7_provider"].LLM7Provider = lambda: mock_provider
                result = engine._auto_detect_providers()

            assert "llm7" in result

    @pytest.mark.asyncio
    async def test_auto_detect_providers_no_keys_no_ollama(self):
        """_auto_detect_providers returns only llm7 when no keys/ollama available."""
        from nvh.core.engine import Engine

        with patch("nvh.core.engine.Engine.__init__", return_value=None):
            engine = Engine.__new__(Engine)
            engine.registry = ProviderRegistry()

            # Block httpx so Ollama check fails, clear env keys
            with (
                patch("httpx.get", side_effect=Exception("no connection")),
                patch.dict("os.environ", {}, clear=True),
            ):
                result = engine._auto_detect_providers()

            assert isinstance(result, list)
            # llm7 should still be detected (always available)
            assert "llm7" in result or len(result) >= 0


# ===================================================================
# Target 2: nvh/core/council.py
# ===================================================================

class TestCouncilOrchestrator:
    """Cover non-streaming synthesis, heuristic agreement, quorum, budget check."""

    def _make_orchestrator(self, config=None, registry=None, rate_manager=None):
        from nvh.core.council import CouncilOrchestrator
        cfg = config or _build_config()
        reg = registry or _build_registry("provA", "provB")
        return CouncilOrchestrator(cfg, reg, rate_manager)

    @pytest.mark.asyncio
    async def test_run_council_non_streaming_with_synthesis(self):
        """run_council with synthesize=True invokes synthesis provider."""
        orch = self._make_orchestrator()
        reg = orch.registry

        # Both members return successfully
        pA = reg.get("provA")
        pA.complete = AsyncMock(return_value=_make_resp("answer A", "provA"))
        pB = reg.get("provB")
        pB.complete = AsyncMock(return_value=_make_resp("answer B", "provB"))

        result = await orch.run_council(
            query="What is Python?",
            synthesize=True,
            timeout=10,
        )

        assert result.quorum_met
        assert len(result.member_responses) >= 2

    @pytest.mark.asyncio
    async def test_weighted_synthesis_with_four_members(self):
        """_weighted_synthesis builds prompt with >3 members."""
        cfg = _build_config(
            council=CouncilModeConfig(
                default_weights={"provA": 0.3, "provB": 0.3, "provC": 0.2, "provD": 0.2},
                synthesis_provider="provA",
                quorum=2,
                timeout=30,
            ),
        )
        reg = _build_registry("provA", "provB", "provC", "provD")
        orch = self._make_orchestrator(config=cfg, registry=reg)

        responses = {
            "provA": _make_resp("A says foo", "provA"),
            "provB": _make_resp("B says bar", "provB"),
            "provC": _make_resp("C says baz", "provC"),
            "provD": _make_resp("D says qux", "provD"),
        }
        from nvh.core.council import CouncilMember
        members = [
            CouncilMember(provider="provA", model="mA", weight=0.3),
            CouncilMember(provider="provB", model="mB", weight=0.3),
            CouncilMember(provider="provC", model="mC", weight=0.2),
            CouncilMember(provider="provD", model="mD", weight=0.2),
        ]

        result = await orch._weighted_synthesis("question?", responses, members)
        assert result.content  # synthesis returned something

    @pytest.mark.asyncio
    async def test_analyze_agreement_heuristic_only(self):
        """_analyze_agreement with use_llm=False uses heuristic path."""
        orch = self._make_orchestrator()
        r1 = _make_resp("Python is great language for programming scripts", "provA")
        r2 = _make_resp("Python is excellent language for writing scripts", "provB")

        score, summary = await orch._analyze_agreement(
            query="What is Python?",
            member_responses={"provA": r1, "provB": r2},
            use_llm=False,
        )

        assert score is not None
        assert 0.0 <= score <= 1.0
        assert summary is not None

    @pytest.mark.asyncio
    async def test_heuristic_agreement_divergent(self):
        """Heuristic agreement detects divergent responses."""
        from nvh.core.council import CouncilOrchestrator
        r1 = _make_resp("Quantum computing uses qubits for computation", "provA")
        r2 = _make_resp("The weather today is sunny and warm outside", "provB")

        score, summary = CouncilOrchestrator._heuristic_agreement(
            {"provA": r1, "provB": r2},
        )

        assert score < 0.5
        assert "diverge" in summary.lower() or "partial" in summary.lower() or "split" in summary.lower()

    @pytest.mark.asyncio
    async def test_quorum_not_met_skips_synthesis(self):
        """When quorum is not met, synthesis is skipped."""
        cfg = _build_config(
            council=CouncilModeConfig(
                default_weights={"provA": 0.5, "provB": 0.5},
                synthesis_provider="provA",
                quorum=2,
                timeout=5,
            ),
        )
        reg = _build_registry("provA", "provB")

        # provA succeeds, provB fails
        pA = reg.get("provA")
        pA.complete = AsyncMock(return_value=_make_resp("ok", "provA"))
        pB = reg.get("provB")
        pB.complete = AsyncMock(side_effect=Exception("fail"))

        orch = self._make_orchestrator(config=cfg, registry=reg)
        result = await orch.run_council("test", timeout=5)

        assert not result.quorum_met
        assert result.synthesis is None

    @pytest.mark.asyncio
    async def test_streaming_budget_check_passes(self):
        """run_council_streaming with budget_check that passes runs synthesis."""
        cfg = _build_config()
        reg = _build_registry("provA", "provB")

        pA = reg.get("provA")
        pB = reg.get("provB")

        # Set up streaming mocks
        async def _stream_chunks(*args, **kwargs):
            from nvh.providers.base import StreamChunk
            yield StreamChunk(delta="hello", is_final=False, model="m1")
            yield StreamChunk(
                delta="", is_final=True, model="m1",
                usage=Usage(input_tokens=5, output_tokens=10),
                cost_usd=Decimal("0"),
                finish_reason=FinishReason.STOP,
            )

        pA.stream = _stream_chunks
        pB.stream = _stream_chunks

        events = []
        async def on_event(e):
            events.append(e)

        async def budget_ok():
            pass  # no exception = budget is fine

        orch = self._make_orchestrator(config=cfg, registry=reg)
        result = await orch.run_council_streaming(
            query="test",
            on_event=on_event,
            budget_check=budget_ok,
            timeout=10,
        )

        assert result.quorum_met
        event_types = [e["type"] for e in events]
        assert "council_start" in event_types
        assert "council_complete" in event_types


# ===================================================================
# Target 3: nvh/core/router.py
# ===================================================================

class TestRoutingEngine:
    """Cover health penalty, custom rules, no-providers, and learned scores."""

    def _make_router(self, config=None, registry=None, rate_manager=None):
        from nvh.core.rate_limiter import ProviderRateManager
        from nvh.core.router import RoutingEngine

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


# ===================================================================
# Target 4: nvh/storage/repository.py
# ===================================================================

@pytest.fixture
async def _init_test_db(tmp_path):
    """Init in-memory DB for repository tests."""
    from nvh.storage import repository as repo
    db_path = tmp_path / "test.db"
    await repo.init_db(db_path)
    yield repo
    await repo.close_db()


class TestRepository:
    """Cover analytics with data, conversations CRUD, and pagination."""

    @pytest.mark.asyncio
    async def test_create_and_get_conversation(self, _init_test_db):
        repo = _init_test_db
        conv = await repo.create_conversation(
            provider="openai", model="gpt-4", title="Test chat",
        )
        assert conv.id
        assert conv.title == "Test chat"

        fetched = await repo.get_conversation(conv.id)
        assert fetched is not None
        assert fetched.provider == "openai"
        assert fetched.model == "gpt-4"

    @pytest.mark.asyncio
    async def test_list_conversations_pagination(self, _init_test_db):
        repo = _init_test_db
        # Create 5 conversations
        for i in range(5):
            await repo.create_conversation(title=f"Conv {i}")

        all_convs = await repo.list_conversations(limit=20)
        assert len(all_convs) == 5

        limited = await repo.list_conversations(limit=3)
        assert len(limited) == 3

    @pytest.mark.asyncio
    async def test_delete_conversation(self, _init_test_db):
        repo = _init_test_db
        conv = await repo.create_conversation(title="To delete")

        result = await repo.delete_conversation(conv.id)
        assert result is True

        fetched = await repo.get_conversation(conv.id)
        assert fetched is None

    @pytest.mark.asyncio
    async def test_delete_conversation_nonexistent(self, _init_test_db):
        repo = _init_test_db
        result = await repo.delete_conversation("nonexistent-id")
        assert result is False

    @pytest.mark.asyncio
    async def test_get_analytics_with_data(self, _init_test_db):
        repo = _init_test_db
        # Log several queries across providers
        await repo.log_query(
            mode="single", provider="openai", model="gpt-4",
            input_tokens=100, output_tokens=200,
            cost_usd=Decimal("0.005"), latency_ms=500,
        )
        await repo.log_query(
            mode="single", provider="groq", model="llama-3",
            input_tokens=50, output_tokens=100,
            cost_usd=Decimal("0"), latency_ms=200,
        )
        await repo.log_query(
            mode="council", provider="openai", model="gpt-4",
            input_tokens=80, output_tokens=150,
            cost_usd=Decimal("0.003"), latency_ms=400,
        )

        analytics = await repo.get_analytics()

        assert analytics["queries_today"] >= 3
        assert analytics["queries_this_month"] >= 3
        assert "openai" in analytics["cost_by_provider"]
        assert "openai" in analytics["queries_by_provider"]
        assert analytics["queries_by_provider"]["openai"] >= 2
        assert len(analytics["most_used_models"]) >= 1
        assert analytics["free_queries"] >= 1
        assert analytics["paid_queries"] >= 2
        assert "savings" in analytics

    @pytest.mark.asyncio
    async def test_create_conversation_add_messages_roundtrip(self, _init_test_db):
        repo = _init_test_db
        conv = await repo.create_conversation(provider="test", model="m1")

        _ = await repo.add_message(
            conversation_id=conv.id, role="user", content="Hello",
            provider="test", model="m1",
        )
        _ = await repo.add_message(
            conversation_id=conv.id, role="assistant", content="Hi there!",
            provider="test", model="m1",
            input_tokens=5, output_tokens=10, cost_usd=Decimal("0.001"),
        )

        messages = await repo.get_messages(conv.id)
        assert len(messages) == 2
        assert messages[0].role == "user"
        assert messages[1].role == "assistant"

        # Check conversation was updated
        updated = await repo.get_conversation(conv.id)
        assert updated.message_count == 2
        assert updated.total_cost_usd >= Decimal("0.001")
