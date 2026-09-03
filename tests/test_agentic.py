"""Tests for the agentic coding feature (nvh/core/agentic.py).

Tests tier detection, config building, and the coding agent loop
with mock providers. No real network or GPU required.
"""

from __future__ import annotations

import subprocess
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import nvh.core.agentic as agentic_module
from nvh.core import local_models
from nvh.core.agent_loop import AgentResult, AgentStep
from nvh.core.agentic import (
    _TIER_3_MULTI_MODELS,
    _TIER_MODELS,
    CODING_SYSTEM_PROMPT,
    TIER_BUDGET_GB,
    TIER_DESCRIPTIONS,
    AgentConfig,
    AgentMode,
    AgentTier,
    CodingResult,
    _build_changes_summary,
    _extract_commands,
    _extract_file_operations,
    _run_quality_gates,
    build_agent_config,
    detect_agent_tier,
    run_coding_agent,
)
from nvh.core.tools import ToolResult
from nvh.providers.base import CompletionResponse, Usage
from nvh.providers.registry import ProviderRegistry


def _tag(model: str | None) -> str:
    return (model or "").removeprefix("ollama/")


# ---------------------------------------------------------------------------
# Tier detection
# ---------------------------------------------------------------------------


class TestTierDetection:
    def test_thresholds_are_table_row_starts(self):
        """The agent's floors are where the local-model table starts a row (128 sits in its 96+ row)."""
        row_starts = {tier.min_gb for tier in local_models.LOCAL_MODEL_TIERS}
        assert TIER_BUDGET_GB == {
            AgentTier.TIER_1: 16.0, AgentTier.TIER_2: 24.0, AgentTier.TIER_3: 48.0,
            AgentTier.TIER_4: 96.0, AgentTier.TIER_5: 128.0,
        }
        for tier, floor_gb in TIER_BUDGET_GB.items():
            assert detect_agent_tier(floor_gb) == tier
            assert detect_agent_tier(floor_gb - 0.1) != tier
            if floor_gb < 128:
                assert floor_gb in row_starts
            assert local_models.tier_for(floor_gb).min_gb <= floor_gb

    def test_docstring_no_longer_says_tier_0_is_under_24(self):
        assert "Tier 0 (<24" not in (agentic_module.__doc__ or "")
        assert "Tier 0 (<16" in (agentic_module.__doc__ or "")

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

    def test_tier_1_cloud_orch_table_coder(self):
        config = build_agent_config(AgentTier.TIER_1)
        assert config.orchestrator_provider is None
        assert config.worker_provider == "ollama"
        assert _tag(config.worker_model) == local_models.pick(TIER_BUDGET_GB[AgentTier.TIER_1], "code").tag
        assert config.reviewer_model is None

    def test_tier_2_cloud_orch_table_coder(self):
        config = build_agent_config(AgentTier.TIER_2)
        assert config.orchestrator_provider is None
        assert config.worker_provider == "ollama"
        assert _tag(config.worker_model) == local_models.pick(TIER_BUDGET_GB[AgentTier.TIER_2], "code").tag
        # no reviewer row below 48 GB, even when multi is asked for
        multi = build_agent_config(AgentTier.TIER_2, mode=AgentMode.MULTI)
        assert multi.reviewer_provider is None and multi.reviewer_model is None

    def test_tier_3_single_mode_default(self):
        config = build_agent_config(AgentTier.TIER_3)
        assert config.orchestrator_provider is None
        assert config.worker_provider == "ollama"
        assert _tag(config.worker_model) == local_models.pick(TIER_BUDGET_GB[AgentTier.TIER_3], "code").tag
        assert config.mode == AgentMode.SINGLE
        assert config.reviewer_model is None  # single mode by default

    def test_tier_3_multi_mode(self):
        config = build_agent_config(AgentTier.TIER_3, mode=AgentMode.MULTI)
        floor_gb = TIER_BUDGET_GB[AgentTier.TIER_3]
        worker = local_models.pick(floor_gb, "code")
        reviewer = local_models.pick(floor_gb - worker.runtime_gb, "reasoning")
        assert config.worker_provider == "ollama"
        assert _tag(config.worker_model) == worker.tag
        assert config.reviewer_provider == "ollama"
        assert _tag(config.reviewer_model) == reviewer.tag
        assert config.reviewer_model != config.worker_model
        # the old alternate dict is the same row now
        assert _TIER_3_MULTI_MODELS == _TIER_MODELS[AgentTier.TIER_3]

    def test_tier_4_dual_model(self):
        config = build_agent_config(AgentTier.TIER_4)
        floor_gb = TIER_BUDGET_GB[AgentTier.TIER_4]
        assert config.orchestrator_provider is None
        assert config.worker_provider == "ollama"
        assert _tag(config.worker_model) == local_models.pick(floor_gb, "code").tag
        assert config.max_parallel_workers == local_models.num_parallel_for(floor_gb)
        # Auto mode on Tier 4 → multi
        assert config.mode == AgentMode.MULTI
        assert config.reviewer_provider == "ollama"
        assert config.reviewer_model is not None

    def test_tier_5_fully_local_triple(self):
        config = build_agent_config(AgentTier.TIER_5)
        floor_gb = TIER_BUDGET_GB[AgentTier.TIER_5]
        assert config.orchestrator_provider == "ollama"
        assert config.worker_provider == "ollama"
        assert config.reviewer_provider == "ollama"
        assert config.max_parallel_workers == local_models.num_parallel_for(floor_gb)
        assert _tag(config.orchestrator_model) == local_models.pick(floor_gb, "chat").tag
        # Three different models
        assert len({config.orchestrator_model, config.worker_model, config.reviewer_model}) == 3

    def test_local_models_fit_side_by_side(self):
        """Every tier's local roles load together within the tier floor (a Spark's 112 GB budget too)."""
        sizes = local_models.size_table()
        for tier, floor_gb in TIER_BUDGET_GB.items():
            local_tags = [_tag(model) for _provider, model in _TIER_MODELS[tier].values() if model]
            assert local_tags, tier
            total = sum(sizes[tag] for tag in local_tags)
            assert total <= floor_gb, (tier, local_tags, total)
        spark_budget = 128.0 - local_models.UNIFIED_MEMORY_OS_RESERVE_GB
        tier_5 = [_tag(model) for _p, model in _TIER_MODELS[AgentTier.TIER_5].values() if model]
        assert sum(sizes[tag] for tag in tier_5) <= spark_budget

    def test_every_model_is_a_table_tag(self):
        tags = set(local_models.all_tags())
        for tier_models in _TIER_MODELS.values():
            for provider, model in tier_models.values():
                if provider is None:
                    assert model is None
                else:
                    assert provider == "ollama"
                    assert _tag(model) in tags, model
        assert _TIER_MODELS[AgentTier.TIER_0] == {
            "orchestrator": (None, None), "worker": (None, None), "reviewer": (None, None),
        }

    def test_parallel_workers_follow_the_table(self):
        assert build_agent_config(AgentTier.TIER_0).max_parallel_workers == 1
        for tier, floor_gb in TIER_BUDGET_GB.items():
            assert build_agent_config(tier).max_parallel_workers == local_models.num_parallel_for(floor_gb)

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
        worker = f"ollama/{local_models.pick(24.0, 'code').tag}"
        config = AgentConfig(
            tier=AgentTier.TIER_2,
            orchestrator_model="gpt-4o-mini",
            worker_model=worker,
        )

        result = await run_coding_agent(
            task="Add tests",
            engine=engine,
            config=config,
            working_dir=tmp_path,
        )

        assert result.tier == AgentTier.TIER_2
        assert result.orchestrator_model == "gpt-4o-mini"
        assert result.worker_model == worker
        assert result.duration_ms >= 0


# ---------------------------------------------------------------------------
# Result summarisation and quality gates
# ---------------------------------------------------------------------------


def _make_step(tool_calls: list[dict], tool_results: list[ToolResult]) -> AgentStep:
    return AgentStep(
        iteration=1,
        thought="",
        tool_calls=tool_calls,
        tool_results=tool_results,
        response="",
    )


def _make_agent_result(steps: list[AgentStep]) -> AgentResult:
    return AgentResult(
        task="test",
        final_response="done",
        steps=steps,
        total_iterations=len(steps),
        total_tool_calls=sum(len(s.tool_calls) for s in steps),
        completed=True,
    )


class TestExtractCommands:
    def test_shell_commands_extracted(self):
        step = _make_step(
            [
                {"tool": "shell", "args": {"command": "pytest"}},
                {"tool": "run_code", "args": {"command": "python -c 'p'"}},
            ],
            [
                ToolResult(tool_name="shell", success=True, output="ok"),
                ToolResult(tool_name="run_code", success=True, output="ok"),
            ],
        )
        cmds = _extract_commands(_make_agent_result([step]))
        assert "pytest" in cmds
        assert "python -c 'p'" in cmds

    def test_no_duplicates(self):
        step = _make_step(
            [
                {"tool": "shell", "args": {"command": "ls"}},
                {"tool": "shell", "args": {"command": "ls"}},
            ],
            [
                ToolResult(tool_name="shell", success=True, output=""),
                ToolResult(tool_name="shell", success=True, output=""),
            ],
        )
        cmds = _extract_commands(_make_agent_result([step]))
        assert cmds.count("ls") == 1


class TestBuildChangesSummary:
    def test_summary_includes_write_and_shell(self):
        step = _make_step(
            [
                {"tool": "write_file", "args": {"path": "f.py", "content": "x"}},
                {"tool": "shell", "args": {"command": "pytest"}},
                {"tool": "read_file", "args": {"path": "bar.py"}},
            ],
            [
                ToolResult(tool_name="write_file", success=True, output="ok"),
                ToolResult(tool_name="shell", success=True, output="passed"),
                ToolResult(tool_name="read_file", success=True, output="data"),
            ],
        )
        summary = _build_changes_summary(_make_agent_result([step]))
        assert "f.py" in summary
        assert "pytest" in summary
        assert "bar.py" in summary

    def test_empty_steps(self):
        summary = _build_changes_summary(_make_agent_result([]))
        assert "no tool calls" in summary.lower()


class TestRunQualityGates:
    """Cover _run_quality_gates with real subprocess mocks."""

    @pytest.mark.asyncio
    async def test_py_file_passes_lint(self, tmp_path: Path):
        """A syntactically valid .py file should pass quality gates."""
        f = tmp_path / "good.py"
        f.write_text("x = 1\n", encoding="utf-8")
        with patch("subprocess.run") as mock_run:
            # ruff passes, syntax passes
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            passed, output = await _run_quality_gates(tmp_path, [str(f)])
        assert passed is True
        assert "passed" in output.lower()

    @pytest.mark.asyncio
    async def test_no_python_files_returns_none(self, tmp_path: Path):
        """Non-Python files should result in (None, '')."""
        passed, output = await _run_quality_gates(tmp_path, ["data.json"])
        assert passed is None
        assert output == ""

    @pytest.mark.asyncio
    async def test_ruff_failure_marks_not_passed(self, tmp_path: Path):
        """When ruff reports errors, quality gates should fail."""
        f = tmp_path / "bad.py"
        f.write_text("import os\n", encoding="utf-8")
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # ruff check fails
                return subprocess.CompletedProcess(
                    args=[], returncode=1,
                    stdout="bad.py:1: F401 unused import", stderr=""
                )
            # syntax check passes
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )

        with patch("subprocess.run", side_effect=side_effect):
            passed, output = await _run_quality_gates(tmp_path, [str(f)])
        assert passed is False
        assert "FAILED" in output

    @pytest.mark.asyncio
    async def test_ruff_not_installed_skipped(self, tmp_path: Path):
        """When ruff is not found, gate is skipped but syntax still runs."""
        f = tmp_path / "ok.py"
        f.write_text("x = 1\n", encoding="utf-8")
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise FileNotFoundError("ruff not found")
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )

        with patch("subprocess.run", side_effect=side_effect):
            passed, output = await _run_quality_gates(tmp_path, [str(f)])
        assert "skipped" in output.lower()


class TestExtractFileOperations:
    """Cover _extract_file_operations with mixed read/write calls."""

    def test_read_then_write_is_modified(self):
        """A file that was read then written should be 'modified'."""
        steps = [
            _make_step(
                [
                    {"tool": "read_file", "args": {"path": "a.py"}},
                    {"tool": "write_file", "args": {"path": "a.py"}},
                ],
                [
                    ToolResult(tool_name="read_file", success=True, output="content"),
                    ToolResult(tool_name="write_file", success=True, output="ok"),
                ],
            )
        ]
        result = _make_agent_result(steps)
        modified, created, read = _extract_file_operations(result)
        assert "a.py" in modified
        assert "a.py" in read
        assert "a.py" not in created

    def test_write_without_read_is_created(self):
        """A file written without prior read should be 'created'."""
        steps = [
            _make_step(
                [{"tool": "write_file", "args": {"path": "new.py"}}],
                [ToolResult(tool_name="write_file", success=True, output="ok")],
            )
        ]
        result = _make_agent_result(steps)
        modified, created, read = _extract_file_operations(result)
        assert "new.py" in created
        assert "new.py" not in modified

    def test_mixed_operations(self):
        """Mix of read-only, create, and modify in one result."""
        steps = [
            _make_step(
                [
                    {"tool": "read_file", "args": {"path": "r.py"}},
                    {"tool": "read_file", "args": {"path": "m.py"}},
                    {"tool": "write_file", "args": {"path": "m.py"}},
                    {"tool": "write_file", "args": {"path": "c.py"}},
                ],
                [
                    ToolResult(tool_name="read_file", success=True, output="x"),
                    ToolResult(tool_name="read_file", success=True, output="y"),
                    ToolResult(tool_name="write_file", success=True, output="ok"),
                    ToolResult(tool_name="write_file", success=True, output="ok"),
                ],
            )
        ]
        result = _make_agent_result(steps)
        modified, created, read = _extract_file_operations(result)
        assert "r.py" in read
        assert "m.py" in modified
        assert "c.py" in created


class TestAgenticModuleSurface:
    def test_coding_system_prompt_has_tools(self):
        assert "{tool_descriptions}" in CODING_SYSTEM_PROMPT
        assert "read" in CODING_SYSTEM_PROMPT.lower()
        assert "write" in CODING_SYSTEM_PROMPT.lower()

    def test_tier_descriptions_all_tiers(self):
        for tier in AgentTier:
            assert tier in TIER_DESCRIPTIONS
            assert len(TIER_DESCRIPTIONS[tier]) > 10

    def test_tier_descriptions_name_the_table_picks(self):
        """The display text is generated from the same rows, so it cannot name a model the tier does not load."""
        assert "cloud" in TIER_DESCRIPTIONS[AgentTier.TIER_0].lower()
        for tier in TIER_BUDGET_GB:
            for _provider, model in _TIER_MODELS[tier].values():
                if model:
                    assert _tag(model) in TIER_DESCRIPTIONS[tier], (tier, model)
        for retired in ("70B", "7B", "Llama", "Nemotron planner", "Qwen reviewer"):
            assert not any(retired in text for text in TIER_DESCRIPTIONS.values()), retired

    def test_agent_mode_enum(self):
        assert AgentMode.AUTO == "auto"
        assert AgentMode.SINGLE == "single"
        assert AgentMode.MULTI == "multi"

    def test_build_changes_summary_empty(self):
        result = AgentResult(
            task="test", final_response="done",
            steps=[], total_iterations=0, total_tool_calls=0, completed=True,
        )
        summary = _build_changes_summary(result)
        assert "no tool calls" in summary.lower()

    def test_extract_commands_empty(self):
        result = AgentResult(
            task="test", final_response="done",
            steps=[], total_iterations=0, total_tool_calls=0, completed=True,
        )
        cmds = _extract_commands(result)
        assert cmds == []

    def test_coding_result_defaults(self):
        r = CodingResult(task="test", plan="plan", final_summary="done")
        assert r.completed is False
        assert r.total_cost_usd == Decimal("0")
        assert r.tier == AgentTier.TIER_0
        assert r.quality_gate_passed is None
