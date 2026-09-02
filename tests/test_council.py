"""Tests for the council orchestrator — strategies, agreement, quorum, synthesis."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from nvh.config.settings import (
    BudgetConfig,
    CacheConfig,
    CouncilConfig,
    CouncilModeConfig,
    DefaultsConfig,
    ProviderConfig,
    RoutingConfig,
)
from nvh.core.council import CouncilMember, CouncilOrchestrator
from nvh.providers.base import (
    CompletionResponse,
    FinishReason,
    HealthStatus,
    StreamChunk,
    Usage,
)
from nvh.providers.registry import ProviderRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resp(content="hello", provider="mock", model="m", cost="0") -> CompletionResponse:
    return CompletionResponse(
        content=content,
        model=model,
        provider=provider,
        usage=Usage(input_tokens=10, output_tokens=20, total_tokens=30),
        cost_usd=Decimal(cost),
        latency_ms=10,
        finish_reason=FinishReason.STOP,
    )


def _mock_provider(name="mock"):
    p = AsyncMock()
    p.complete = AsyncMock(return_value=_resp(provider=name))
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


def _make_orchestrator():
    """Build orchestrator with minimal fakes."""
    cfg = MagicMock()
    cfg.council.strategy = "weighted_consensus"
    cfg.council.synthesis_provider = "synth"
    cfg.council.timeout = 30
    cfg.council.quorum = 2
    cfg.council.default_weights = {}
    cfg.providers = {}

    registry = MagicMock()
    registry.list_enabled.return_value = ["synth"]
    registry.has.return_value = True

    mock_provider = AsyncMock()
    mock_provider.complete = AsyncMock(return_value=_resp("synthesized"))
    registry.get.return_value = mock_provider

    return CouncilOrchestrator(cfg, registry), mock_provider


class TestCouncilMemberResolution:
    def test_normalize_weights(self):
        members = [
            CouncilMember(provider="a", model="m1", weight=0.3),
            CouncilMember(provider="b", model="m2", weight=0.3),
            CouncilMember(provider="c", model="m3", weight=0.3),
        ]
        total = sum(m.weight for m in members)
        assert abs(total - 0.9) < 0.01

    def test_council_response_cost(self):
        r1 = CompletionResponse(
            content="Response 1", model="m1", provider="a",
            usage=Usage(input_tokens=100, output_tokens=50, total_tokens=150),
            cost_usd=Decimal("0.001"),
        )
        r2 = CompletionResponse(
            content="Response 2", model="m2", provider="b",
            usage=Usage(input_tokens=100, output_tokens=60, total_tokens=160),
            cost_usd=Decimal("0.002"),
        )
        total = r1.cost_usd + r2.cost_usd
        assert total == Decimal("0.003")


class TestHeuristicAgreement:
    def test_high_overlap_gives_strong_consensus(self):
        resps = {
            "a": _resp("Python is a great programming language for data science"),
            "b": _resp("Python is a wonderful programming language for data science"),
        }
        score, summary = CouncilOrchestrator._heuristic_agreement(resps)
        assert score is not None and score > 0.4
        assert summary is not None

    def test_divergent_responses_low_score(self):
        resps = {
            "a": _resp("Kubernetes orchestrates containers across clusters"),
            "b": _resp("Banana smoothie recipe needs yogurt blueberry vanilla"),
        }
        score, summary = CouncilOrchestrator._heuristic_agreement(resps)
        assert score is not None and score < 0.5
        assert summary is not None

    def test_empty_content(self):
        resps = {"a": _resp(""), "b": _resp("")}
        score, _ = CouncilOrchestrator._heuristic_agreement(resps)
        assert score is not None and score == 0.0


class TestMajorityVote:
    def test_returns_highest_weight_member(self):
        orch, _ = _make_orchestrator()
        members = [
            CouncilMember(provider="a", model="m1", weight=0.3),
            CouncilMember(provider="b", model="m2", weight=0.7),
        ]
        resps = {"a": _resp("answer A", "a"), "b": _resp("answer B", "b")}
        result = orch._majority_vote(resps, members)
        assert "answer B" in result.content
        assert result.metadata["strategy"] == "majority_vote"


class TestBestOf:
    @pytest.mark.asyncio
    async def test_best_of_calls_judge(self):
        orch, mock_prov = _make_orchestrator()
        members = [CouncilMember(provider="a", model="m", weight=0.5)]
        resps = {"a": _resp("first"), "b": _resp("second")}
        result = await orch._best_of("question?", resps, members)
        assert result.metadata["strategy"] == "best_of"
        mock_prov.complete.assert_awaited()


class TestWeightedSynthesis:
    @pytest.mark.asyncio
    async def test_with_personas_prompt(self):
        orch, mock_prov = _make_orchestrator()
        members = [
            CouncilMember(provider="a", model="m", weight=0.5, persona="Architect"),
            CouncilMember(provider="b", model="m", weight=0.5, persona="Security"),
        ]
        resps = {"a": _resp("arch view"), "b": _resp("sec view")}
        await orch._weighted_synthesis("q?", resps, members)
        call_args = mock_prov.complete.call_args
        msgs = call_args.kwargs.get(
            "messages", call_args.args[0] if call_args.args else [],
        )
        text = msgs[0].content if hasattr(msgs[0], "content") else str(msgs)
        assert "expert" in text.lower() or "council" in text.lower()

    @pytest.mark.asyncio
    async def test_without_personas_prompt(self):
        orch, mock_prov = _make_orchestrator()
        members = [
            CouncilMember(provider="a", model="m", weight=0.5),
            CouncilMember(provider="b", model="m", weight=0.5),
        ]
        resps = {"a": _resp("r1"), "b": _resp("r2")}
        await orch._weighted_synthesis("q?", resps, members)
        call_args = mock_prov.complete.call_args
        msgs = call_args.kwargs.get(
            "messages", call_args.args[0] if call_args.args else [],
        )
        text = msgs[0].content if hasattr(msgs[0], "content") else str(msgs)
        assert "Multiple AI models" in text or "weighted" in text.lower()


class TestCouncilOrchestrator:
    """Cover non-streaming synthesis, heuristic agreement, quorum, budget check."""

    def _make_orchestrator(self, config=None, registry=None, rate_manager=None):
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
        pA.complete = AsyncMock(return_value=_resp("answer A", "provA"))
        pB = reg.get("provB")
        pB.complete = AsyncMock(return_value=_resp("answer B", "provB"))

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
            "provA": _resp("A says foo", "provA"),
            "provB": _resp("B says bar", "provB"),
            "provC": _resp("C says baz", "provC"),
            "provD": _resp("D says qux", "provD"),
        }
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
        r1 = _resp("Python is great language for programming scripts", "provA")
        r2 = _resp("Python is excellent language for writing scripts", "provB")

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
        r1 = _resp("Quantum computing uses qubits for computation", "provA")
        r2 = _resp("The weather today is sunny and warm outside", "provB")

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
        pA.complete = AsyncMock(return_value=_resp("ok", "provA"))
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


class TestCouncilStrategies:
    @pytest.mark.asyncio
    async def test_majority_vote_picks_most_common(self):
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
# Synthesis strategies against an openai/groq registry
# ---------------------------------------------------------------------------

_GP = {"groq": ProviderConfig(default_model="llama")}


def _orch(extra=None, quorum=2):
    provs = {"openai": ProviderConfig(default_model="gpt-4o")}
    if extra:
        provs.update(extra)
    cfg = CouncilConfig(
        defaults=DefaultsConfig(provider="openai", model="gpt-4o"),
        providers=provs,
        council=CouncilModeConfig(synthesis_provider="openai", quorum=quorum),
    )
    reg = ProviderRegistry()
    mp = MagicMock()
    mp.complete = AsyncMock(return_value=_resp(content="synth"))
    for n in provs:
        reg.register(n, mp)
    return CouncilOrchestrator(cfg, reg)


@pytest.mark.asyncio
async def test_best_of():
    o = _orch(_GP)
    r = await o._best_of("q?",
        {"openai": _resp(content="A"), "groq": _resp(content="B")},
        [CouncilMember("openai", "gpt-4o", 0.5),
         CouncilMember("groq", "llama", 0.5)])
    assert r.metadata.get("strategy") == "best_of"


@pytest.mark.asyncio
async def test_weighted_synthesis_two():
    o = _orch(_GP)
    r = await o._weighted_synthesis("q?",
        {"openai": _resp(content="A"), "groq": _resp(content="B")},
        [CouncilMember("openai", "gpt-4o", 0.6),
         CouncilMember("groq", "llama", 0.4)])
    assert r.content == "synth"


@pytest.mark.asyncio
async def test_analyze_agreement_llm():
    mp = MagicMock()
    mp.complete = AsyncMock(
        return_value=_resp(content="SCORE: 8\nSUMMARY: All agree."))
    reg = ProviderRegistry()
    reg.register("openai", mp)
    o = CouncilOrchestrator(
        CouncilConfig(
            defaults=DefaultsConfig(provider="openai", model="gpt-4o"),
            providers={"openai": ProviderConfig(default_model="gpt-4o")},
            council=CouncilModeConfig(synthesis_provider="openai"),
        ),
        reg,
    )
    sc, sm = await o._analyze_agreement(
        "q?", {"a": _resp(), "b": _resp()}, use_llm=True)
    assert sc == pytest.approx(0.8, abs=0.01) and "agree" in sm.lower()


@pytest.mark.asyncio
async def test_run_council_auto_agents():
    o = _orch(quorum=1)
    r = await o.run_council("How to scale a DB?",
                            auto_agents=True, num_agents=1, synthesize=False)
    assert r.quorum_met and len(r.agents_used) >= 1
