"""Tests for the agentic coding feature (nvh/core/agentic.py).

Tests tier detection, config building, and the coding agent loop
with mock providers. No real network or GPU required.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nvh.core.agentic import (
    AgentConfig,
    AgentMode,
    AgentTier,
    CodingResult,
    build_agent_config,
    detect_agent_tier,
    run_coding_agent,
)
from nvh.providers.base import CompletionResponse, Usage
from nvh.providers.registry import ProviderRegistry

# ---------------------------------------------------------------------------
# Tier detection
# ---------------------------------------------------------------------------


class TestTierDetection:
    def test_tier_0_no_gpu(self):
        assert detect_agent_tier(0) == AgentTier.TIER_0

    def test_tier_0_small_gpu(self):
        assert detect_agent_tier(8) == AgentTier.TIER_0

    def test_tier_1_16gb(self):
        assert detect_agent_tier(16) == AgentTier.TIER_1

    def test_tier_2_rtx_3090(self):
        assert detect_agent_tier(24) == AgentTier.TIER_2

    def test_tier_2_rtx_4090(self):
        assert detect_agent_tier(24) == AgentTier.TIER_2

    def test_tier_3_a100_48(self):
        assert detect_agent_tier(48) == AgentTier.TIER_3

    def test_tier_3_a100_80(self):
        assert detect_agent_tier(80) == AgentTier.TIER_3

    def test_tier_4_rtx_6000_pro_bse(self):
        assert detect_agent_tier(96) == AgentTier.TIER_4

    def test_tier_5_dgx_spark(self):
        assert detect_agent_tier(128) == AgentTier.TIER_5

    def test_tier_5_multi_gpu(self):
        assert detect_agent_tier(192) == AgentTier.TIER_5

    def test_boundary_16(self):
        assert detect_agent_tier(16) == AgentTier.TIER_1
        assert detect_agent_tier(15.9) == AgentTier.TIER_0

    def test_boundary_24(self):
        assert detect_agent_tier(24) == AgentTier.TIER_2
        assert detect_agent_tier(23.9) == AgentTier.TIER_1

    def test_boundary_48(self):
        assert detect_agent_tier(48) == AgentTier.TIER_3
        assert detect_agent_tier(47.9) == AgentTier.TIER_2

    def test_boundary_96(self):
        assert detect_agent_tier(96) == AgentTier.TIER_4
        assert detect_agent_tier(95.9) == AgentTier.TIER_3

    def test_boundary_128(self):
        assert detect_agent_tier(128) == AgentTier.TIER_5
        assert detect_agent_tier(127.9) == AgentTier.TIER_4


# ---------------------------------------------------------------------------
# Config building
# ---------------------------------------------------------------------------


class TestConfigBuilding:
    def test_tier_0_fully_cloud(self):
        config = build_agent_config(AgentTier.TIER_0)
        assert config.orchestrator_provider is None
        assert config.worker_provider is None
        assert config.max_parallel_workers == 1

    def test_tier_1_cloud_orch_small_worker(self):
        config = build_agent_config(AgentTier.TIER_1)
        assert config.orchestrator_provider is None
        assert config.worker_provider == "ollama"
        assert config.worker_model is not None
        assert "7b" in (config.worker_model or "").lower()

    def test_tier_2_cloud_orch_27b_worker(self):
        config = build_agent_config(AgentTier.TIER_2)
        assert config.orchestrator_provider is None
        assert config.worker_provider == "ollama"
        assert "27b" in (config.worker_model or "").lower() or "gemma" in (config.worker_model or "").lower()

    def test_tier_3_single_mode_default(self):
        config = build_agent_config(AgentTier.TIER_3)
        assert config.orchestrator_provider is None
        assert config.worker_provider == "ollama"
        assert "70b" in (config.worker_model or "").lower()
        assert config.reviewer_model is None  # single mode by default

    def test_tier_3_multi_mode(self):
        config = build_agent_config(AgentTier.TIER_3, mode=AgentMode.MULTI)
        assert config.worker_provider == "ollama"
        assert config.reviewer_provider == "ollama"
        assert config.reviewer_model is not None

    def test_tier_4_dual_model(self):
        config = build_agent_config(AgentTier.TIER_4)
        assert config.worker_provider == "ollama"
        assert config.max_parallel_workers == 2
        # Auto mode on Tier 4 → multi
        assert config.mode == AgentMode.MULTI
        assert config.reviewer_model is not None

    def test_tier_5_fully_local_triple(self):
        config = build_agent_config(AgentTier.TIER_5)
        assert config.orchestrator_provider == "ollama"
        assert config.worker_provider == "ollama"
        assert config.reviewer_provider == "ollama"
        assert config.max_parallel_workers == 4
        # Three different models
        assert config.orchestrator_model != config.worker_model

    def test_fallback_when_ollama_not_in_registry(self):
        registry = ProviderRegistry()
        config = build_agent_config(AgentTier.TIER_2, registry=registry)
        assert config.worker_provider is None

    def test_tier_5_fallback_when_ollama_missing(self):
        registry = ProviderRegistry()
        config = build_agent_config(AgentTier.TIER_5, registry=registry)
        assert config.orchestrator_provider is None
        assert config.worker_provider is None


# ---------------------------------------------------------------------------
# Coding agent loop (with mock engine)
# ---------------------------------------------------------------------------


class _MockEngine:
    """Minimal mock engine that returns canned responses."""

    def __init__(self):
        self.query_calls: list[dict] = []
        self.registry = ProviderRegistry()
        self._plan_response = "1. Read the file\n2. Fix the bug\n3. Verify"
        self._execute_response = "I have completed the task. The bug is fixed."
        self._verify_response = "APPROVED — changes look correct."
        self._call_count = 0

    async def query(self, prompt="", **kwargs):
        self._call_count += 1
        self.query_calls.append({"prompt": prompt, **kwargs})

        # First call = planning phase
        if self._call_count == 1:
            content = self._plan_response
        # Last-ish call with "reviewing" = verification
        elif "reviewing" in prompt.lower() or "check" in prompt.lower():
            content = self._verify_response
        else:
            content = self._execute_response

        return CompletionResponse(
            content=content,
            model="mock-model",
            provider="mock",
            usage=Usage(input_tokens=10, output_tokens=50, total_tokens=60),
            cost_usd=Decimal("0.001"),
            latency_ms=100,
        )

    async def initialize(self):
        pass


class TestCodingAgentLoop:
    @pytest.mark.asyncio
    async def test_three_phase_loop_runs(self, tmp_path: Path):
        """The agent must go through plan → execute → verify phases."""
        engine = _MockEngine()
        config = AgentConfig(tier=AgentTier.TIER_0)

        result = await run_coding_agent(
            task="Fix the bug in main.py",
            engine=engine,
            config=config,
            working_dir=tmp_path,
        )

        assert isinstance(result, CodingResult)
        # Phase 1 (plan) should have been called
        assert "Read the file" in result.plan
        # Phase 3 (verify) should have been called
        assert "APPROVED" in result.verification
        # At least 2 engine.query calls: plan + execute (worker produces
        # no tool calls so it completes immediately) + verify
        assert len(engine.query_calls) >= 2

    @pytest.mark.asyncio
    async def test_skips_verification_when_disabled(self, tmp_path: Path):
        engine = _MockEngine()
        config = AgentConfig(tier=AgentTier.TIER_0, verify_results=False)

        result = await run_coding_agent(
            task="Add a comment to main.py",
            engine=engine,
            config=config,
            working_dir=tmp_path,
        )

        # Verification should not have run
        assert result.verification == ""
        # Only plan + execute calls (no verify)
        assert len(engine.query_calls) == 2

    @pytest.mark.asyncio
    async def test_handles_planning_failure(self, tmp_path: Path):
        engine = _MockEngine()
        engine.query = AsyncMock(side_effect=Exception("network error"))
        config = AgentConfig(tier=AgentTier.TIER_0)

        result = await run_coding_agent(
            task="Fix the bug",
            engine=engine,
            config=config,
            working_dir=tmp_path,
        )

        assert not result.completed
        assert "Planning failed" in result.error
        assert result.plan == ""

    @pytest.mark.asyncio
    async def test_result_tracks_tier_and_models(self, tmp_path: Path):
        engine = _MockEngine()
        config = AgentConfig(
            tier=AgentTier.TIER_2,
            orchestrator_model="gpt-4o-mini",
            worker_model="ollama/qwen2.5-coder:32b",
        )

        result = await run_coding_agent(
            task="Add tests",
            engine=engine,
            config=config,
            working_dir=tmp_path,
        )

        assert result.tier == AgentTier.TIER_2
        assert result.orchestrator_model == "gpt-4o-mini"
        assert result.worker_model == "ollama/qwen2.5-coder:32b"
        assert result.duration_ms >= 0
