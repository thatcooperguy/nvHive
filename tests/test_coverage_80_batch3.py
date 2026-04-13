"""Batch 3 coverage push -- council, agentic, learning, repository."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nvh.core.agent_loop import AgentResult, AgentStep
from nvh.core.agentic import (
    _build_changes_summary,
    _extract_commands,
)
from nvh.core.council import CouncilMember, CouncilOrchestrator
from nvh.core.learning import (
    ALPHA,
    LearnedScoreEntry,
    LearningEngine,
    blend_score,
    ema_update,
    implicit_quality,
)
from nvh.core.tools import ToolResult
from nvh.providers.base import CompletionResponse, FinishReason, Usage
from nvh.storage import repository as repo

# -- helpers ----------------------------------------------------------------

_USAGE = Usage(input_tokens=10, output_tokens=20, total_tokens=30)


def _resp(content: str, provider: str = "p") -> CompletionResponse:
    return CompletionResponse(
        content=content,
        model="m",
        provider=provider,
        usage=_USAGE,
        cost_usd=Decimal("0"),
        latency_ms=10,
        finish_reason=FinishReason.STOP,
    )


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


def _make_agent_result(steps: list[AgentStep]) -> AgentResult:
    return AgentResult(
        task="test",
        final_response="done",
        steps=steps,
        total_iterations=len(steps),
        total_tool_calls=sum(len(s.tool_calls) for s in steps),
        completed=True,
    )


# ===========================================================================
# Module 1: nvh/core/council.py
# ===========================================================================


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


# ===========================================================================
# Module 2: nvh/core/agentic.py
# ===========================================================================


# TestExtractFileOperations lives in test_coverage_80_batch6.py (more thorough copy).


class TestExtractCommands:
    def test_shell_commands_extracted(self):
        step = AgentStep(
            iteration=1,
            thought="",
            tool_calls=[
                {"tool": "shell", "args": {"command": "pytest"}},
                {"tool": "run_code", "args": {"command": "python -c 'p'"}},
            ],
            tool_results=[
                ToolResult(tool_name="shell", success=True, output="ok"),
                ToolResult(tool_name="run_code", success=True, output="ok"),
            ],
            response="",
        )
        cmds = _extract_commands(_make_agent_result([step]))
        assert "pytest" in cmds
        assert "python -c 'p'" in cmds

    def test_no_duplicates(self):
        step = AgentStep(
            iteration=1,
            thought="",
            tool_calls=[
                {"tool": "shell", "args": {"command": "ls"}},
                {"tool": "shell", "args": {"command": "ls"}},
            ],
            tool_results=[
                ToolResult(tool_name="shell", success=True, output=""),
                ToolResult(tool_name="shell", success=True, output=""),
            ],
            response="",
        )
        cmds = _extract_commands(_make_agent_result([step]))
        assert cmds.count("ls") == 1


class TestBuildChangesSummary:
    def test_summary_includes_write_and_shell(self):
        step = AgentStep(
            iteration=1,
            thought="",
            tool_calls=[
                {"tool": "write_file", "args": {"path": "f.py", "content": "x"}},
                {"tool": "shell", "args": {"command": "pytest"}},
                {"tool": "read_file", "args": {"path": "bar.py"}},
            ],
            tool_results=[
                ToolResult(tool_name="write_file", success=True, output="ok"),
                ToolResult(tool_name="shell", success=True, output="passed"),
                ToolResult(tool_name="read_file", success=True, output="data"),
            ],
            response="",
        )
        summary = _build_changes_summary(_make_agent_result([step]))
        assert "f.py" in summary
        assert "pytest" in summary
        assert "bar.py" in summary

    def test_empty_steps(self):
        summary = _build_changes_summary(_make_agent_result([]))
        assert "no tool calls" in summary.lower()


# ===========================================================================
# Module 3: nvh/core/learning.py
# ===========================================================================


class TestEmaUpdate:
    def test_basic_ema(self):
        result = ema_update(0.5, 1.0, alpha=0.2)
        assert result == pytest.approx(0.6)

    def test_default_alpha(self):
        result = ema_update(1.0, 0.0)
        assert result == pytest.approx(1.0 * (1 - ALPHA))


class TestBlendScore:
    def test_below_min_samples_returns_static(self):
        assert blend_score(0.8, 0.5, sample_count=3) == 0.8

    def test_above_full_samples_returns_learned(self):
        assert blend_score(0.8, 0.5, sample_count=25) == 0.5

    def test_interpolation(self):
        result = blend_score(1.0, 0.0, sample_count=12)
        assert 0.0 < result < 1.0


class TestImplicitQuality:
    def test_positive_feedback(self):
        assert implicit_quality("success", False, 1) == 0.9

    def test_negative_feedback(self):
        assert implicit_quality("success", False, -1) == 0.3

    def test_error_status(self):
        assert implicit_quality("error", False, None) == 0.1

    def test_success_no_feedback(self):
        assert implicit_quality("success", False, None) == 0.7


class TestLearningEngineInMemory:
    def test_unknown_provider_returns_static(self):
        engine = LearningEngine()
        assert engine.get_blended_capability("x", "m", "code", 0.75) == 0.75

    def test_cached_score_used(self):
        engine = LearningEngine()
        engine._cache[("x", "m", "code")] = LearnedScoreEntry(
            provider="x", model="m", task_type="code",
            learned_capability=0.9, learned_latency_ms=100,
            learned_reliability=1.0, sample_count=25,
        )
        result = engine.get_blended_capability("x", "m", "code", 0.5)
        assert result == pytest.approx(0.9)


# ===========================================================================
# Module 4: nvh/storage/repository.py
# ===========================================================================


@pytest.fixture
async def _init_mem_db(tmp_path: Path):
    """Initialize an in-memory SQLite DB for repository tests."""
    db_file = tmp_path / "test.db"
    await repo.init_db(db_file)
    yield
    await repo.close_db()


@pytest.mark.asyncio
class TestRepository:
    async def test_init_db_creates_tables(self, tmp_path: Path):
        db_file = tmp_path / "t.db"
        await repo.init_db(db_file)
        try:
            session = repo.get_session()
            assert session is not None
        finally:
            await repo.close_db()

    async def test_log_query_inserts(self, _init_mem_db):
        ql = await repo.log_query(
            mode="single", provider="openai", model="gpt-4o",
            input_tokens=100, output_tokens=50,
            cost_usd=Decimal("0.005"), latency_ms=200,
        )
        assert ql.provider == "openai"
        assert ql.cost_usd == Decimal("0.005")

    async def test_get_spend_accumulates(self, _init_mem_db):
        await repo.log_query(
            mode="single", provider="a", model="m",
            cost_usd=Decimal("0.010"),
        )
        await repo.log_query(
            mode="single", provider="b", model="m",
            cost_usd=Decimal("0.020"),
        )
        spend = await repo.get_spend("daily")
        assert spend >= Decimal("0.030")

    async def test_get_analytics_returns_structure(self, _init_mem_db):
        await repo.log_query(mode="single", provider="x", model="m")
        analytics = await repo.get_analytics()
        assert "queries_today" in analytics
        assert "cost_by_provider" in analytics
        assert "savings" in analytics

    async def test_get_session_raises_before_init(self):
        old_factory = repo._session_factory
        repo._session_factory = None
        try:
            with pytest.raises(RuntimeError, match="not initialized"):
                repo.get_session()
        finally:
            repo._session_factory = old_factory
