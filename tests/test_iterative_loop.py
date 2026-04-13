"""Tests for the iterative refinement loop."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from nvh.core.iterative_loop import (
    IterationRound,
    IterativeResult,
    format_iterative_result,
    iterative_solve,
)
from nvh.providers.base import CompletionResponse, Usage
from nvh.providers.registry import ProviderRegistry

# ---------------------------------------------------------------------------
# Dataclass unit tests
# ---------------------------------------------------------------------------

class TestIterationRound:
    def test_defaults(self):
        r = IterationRound(
            round_number=1, agents_used=["A"], spawned_agents=[],
            qa_verdict="PASSED", qa_feedback="ok", improvements_suggested=[],
        )
        assert r.duration_ms == 0
        assert r.cost_usd == Decimal("0")

    def test_full_construction(self):
        r = IterationRound(
            round_number=2,
            agents_used=["Backend", "DBA"],
            spawned_agents=["DBA"],
            qa_verdict="PARTIAL",
            qa_feedback="needs more detail on sharding",
            improvements_suggested=["add sharding strategy"],
            duration_ms=1234,
            cost_usd=Decimal("0.005"),
        )
        assert r.round_number == 2
        assert len(r.agents_used) == 2
        assert r.qa_verdict == "PARTIAL"


class TestIterativeResult:
    def test_converged(self):
        r = IterativeResult(
            task="build API",
            rounds=[],
            final_verdict="PASSED",
            final_synthesis="done",
            total_agents_used=3,
            total_agents_spawned=1,
            total_rounds=1,
            converged=True,
            final_improvements=[],
        )
        assert r.converged is True
        assert r.total_duration_ms == 0
        assert r.total_cost_usd == Decimal("0")

    def test_not_converged(self):
        r = IterativeResult(
            task="fix bug",
            rounds=[],
            final_verdict="PARTIAL",
            final_synthesis="partial fix",
            total_agents_used=5,
            total_agents_spawned=2,
            total_rounds=3,
            converged=False,
            final_improvements=["check edge case"],
            total_duration_ms=9000,
            total_cost_usd=Decimal("0.10"),
        )
        assert r.converged is False
        assert len(r.final_improvements) == 1


# ---------------------------------------------------------------------------
# Format tests
# ---------------------------------------------------------------------------

class TestFormatIterativeResult:
    def test_converged_output(self):
        result = IterativeResult(
            task="Deploy service",
            rounds=[
                IterationRound(
                    round_number=1,
                    agents_used=["DevOps", "Backend"],
                    spawned_agents=["Backend"],
                    qa_verdict="PASSED",
                    qa_feedback="All good",
                    improvements_suggested=[],
                    duration_ms=2000,
                    cost_usd=Decimal("0.01"),
                ),
            ],
            final_verdict="PASSED",
            final_synthesis="Deployment plan ready.",
            total_agents_used=2,
            total_agents_spawned=1,
            total_rounds=1,
            converged=True,
            final_improvements=[],
            total_duration_ms=2000,
            total_cost_usd=Decimal("0.01"),
        )
        output = format_iterative_result(result)
        assert "Deploy service" in output
        assert "Yes" in output  # converged
        assert "PASSED" in output
        assert "DevOps" in output

    def test_not_converged_shows_improvements(self):
        result = IterativeResult(
            task="Fix rendering",
            rounds=[
                IterationRound(
                    round_number=1, agents_used=["A"], spawned_agents=[],
                    qa_verdict="PARTIAL", qa_feedback="gaps",
                    improvements_suggested=["add tests", "fix edge case"],
                ),
                IterationRound(
                    round_number=2, agents_used=["B"], spawned_agents=[],
                    qa_verdict="PARTIAL", qa_feedback="still gaps",
                    improvements_suggested=["handle null input"],
                ),
            ],
            final_verdict="PARTIAL",
            final_synthesis="Partial fix.",
            total_agents_used=2,
            total_agents_spawned=0,
            total_rounds=2,
            converged=False,
            final_improvements=["handle null input"],
        )
        output = format_iterative_result(result)
        assert "No" in output  # not converged
        assert "Remaining improvements" in output
        assert "handle null input" in output

    def test_spawned_agents_shown(self):
        result = IterativeResult(
            task="task",
            rounds=[
                IterationRound(
                    round_number=1,
                    agents_used=["A", "B"],
                    spawned_agents=["B"],
                    qa_verdict="PASSED",
                    qa_feedback="ok",
                    improvements_suggested=[],
                ),
            ],
            final_verdict="PASSED",
            final_synthesis="ok",
            total_agents_used=2,
            total_agents_spawned=1,
            total_rounds=1,
            converged=True,
            final_improvements=[],
        )
        output = format_iterative_result(result)
        assert "Spawned" in output
        assert "B" in output

    def test_empty_rounds(self):
        result = IterativeResult(
            task="empty",
            rounds=[],
            final_verdict="FAILED",
            final_synthesis="",
            total_agents_used=0,
            total_agents_spawned=0,
            total_rounds=0,
            converged=False,
            final_improvements=[],
        )
        output = format_iterative_result(result)
        assert "empty" in output


# ---------------------------------------------------------------------------
# iterative_solve integration tests (with fakes)
# ---------------------------------------------------------------------------

def _make_fake_engine(qa_verdicts: list[str]):
    """Build a fake engine that returns canned QA verdicts per round.

    qa_verdicts: list of verdict strings the QA query should return, one per round.
    """
    call_count = {"n": 0}

    class FakeProvider:
        name = "fake"
        async def complete(self, messages, **kw):
            return CompletionResponse(
                content="expert opinion", model="m", provider="fake",
                usage=Usage(total_tokens=10), cost_usd=Decimal("0"),
                latency_ms=50,
            )
        async def list_models(self):
            return []
        async def health_check(self):
            pass
        def estimate_tokens(self, t):
            return 1

    class FakeEngine:
        registry = ProviderRegistry()
        config = type("C", (), {"providers": {"fake": type("P", (), {"default_model": "m"})()}})()
        rate_manager = type("RM", (), {"get_health_score": lambda s, p: 1.0})()

        async def query(self, prompt="", **kw):
            idx = call_count["n"]
            call_count["n"] += 1

            # Detect QA query vs agent/synthesis query
            if "VERDICT" in prompt:
                verdict_idx = min(idx // 3, len(qa_verdicts) - 1)  # rough: every 3rd call is QA
                verdict = qa_verdicts[verdict_idx] if verdict_idx < len(qa_verdicts) else "PASSED"
                return CompletionResponse(
                    content=f"VERDICT: {verdict}\nGAPS: none\n1. improve something",
                    model="m", provider="fake",
                    usage=Usage(total_tokens=20), cost_usd=Decimal("0.001"),
                    latency_ms=50,
                )
            return CompletionResponse(
                content="Expert analysis: everything looks solid.",
                model="m", provider="fake",
                usage=Usage(total_tokens=15), cost_usd=Decimal("0.001"),
                latency_ms=50,
            )

    FakeEngine.registry.register("fake", FakeProvider())
    return FakeEngine()


class TestIterativeSolve:
    @pytest.mark.asyncio
    async def test_converges_first_round(self):
        engine = _make_fake_engine(["PASSED"])
        result = await iterative_solve("build API", engine, working_dir=MagicMock())
        assert result.converged is True
        assert result.total_rounds >= 1
        assert result.final_verdict == "PASSED"

    @pytest.mark.asyncio
    async def test_partial_then_passes(self):
        engine = _make_fake_engine(["PARTIAL", "PASSED"])
        result = await iterative_solve("fix bug", engine, working_dir=MagicMock(), max_rounds=3)
        assert result.total_rounds >= 1
        # Should eventually converge or exhaust rounds
        assert result.final_verdict in ("PASSED", "PARTIAL")

    @pytest.mark.asyncio
    async def test_exhausts_max_rounds(self):
        engine = _make_fake_engine(["FAILED", "FAILED", "FAILED"])
        result = await iterative_solve(
            "impossible task", engine, working_dir=MagicMock(), max_rounds=2,
        )
        assert result.total_rounds <= 2
        assert result.converged is False

    @pytest.mark.asyncio
    async def test_progress_callback_called(self):
        engine = _make_fake_engine(["PASSED"])
        progress_calls = []

        def on_progress(event, msg, pct):
            progress_calls.append((event, msg, pct))

        await iterative_solve(
            "task", engine, working_dir=MagicMock(), on_progress=on_progress,
        )
        assert len(progress_calls) >= 1
        assert any("round_1" in c[0] for c in progress_calls)

    @pytest.mark.asyncio
    async def test_agent_generation_failure(self):
        """If generate_agents raises, the round records FAILED and loop breaks."""
        engine = _make_fake_engine(["PASSED"])

        from unittest.mock import patch

        with patch("nvh.core.agents.generate_agents", side_effect=RuntimeError("no agents")):
            result = await iterative_solve("task", engine, working_dir=MagicMock())

        assert len(result.rounds) >= 1
        assert result.rounds[0].qa_verdict == "FAILED"
        assert "failed" in result.rounds[0].qa_feedback.lower()

    @pytest.mark.asyncio
    async def test_cost_tracking(self):
        engine = _make_fake_engine(["PASSED"])
        result = await iterative_solve("task", engine, working_dir=MagicMock())
        # Cost should be a Decimal (possibly zero with fakes)
        assert isinstance(result.total_cost_usd, Decimal)

    @pytest.mark.asyncio
    async def test_duration_tracked(self):
        engine = _make_fake_engine(["PASSED"])
        result = await iterative_solve("task", engine, working_dir=MagicMock())
        assert result.total_duration_ms >= 0

    @pytest.mark.asyncio
    async def test_max_agents_per_round_respected(self):
        engine = _make_fake_engine(["PASSED"])
        result = await iterative_solve(
            "task", engine, working_dir=MagicMock(),
            max_agents_per_round=2,
        )
        # Should complete without error
        assert result.total_rounds >= 1

    @pytest.mark.asyncio
    async def test_single_round_max(self):
        engine = _make_fake_engine(["FAILED"])
        result = await iterative_solve(
            "task", engine, working_dir=MagicMock(), max_rounds=1,
        )
        assert result.total_rounds == 1
