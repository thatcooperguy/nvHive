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

from nvh.core.agent_loop import AgentResult, AgentStep
from nvh.core.agentic import (
    CODING_SYSTEM_PROMPT,
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

    def test_tier_2_cloud_orch_multimodal_worker(self):
        config = build_agent_config(AgentTier.TIER_2)
        assert config.orchestrator_provider is None
        assert config.worker_provider == "ollama"
        assert "vision" in (config.worker_model or "").lower()

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
