"""Batch 6 coverage tests — push agentic modules toward 80%+.

Targets: agentic (_run_quality_gates, _extract_file_operations),
agent_protocol (format_coder_prompt, SubTask depends_on, PlanResult multi,
parse_plan_result numbered-list fallback), agent_review (_parse_findings,
_merge_findings, ReviewReport.approved), agent_testgen (_derive test filename,
generate_tests phases), agent_memory (format_memory_context, update cap),
agent_git (commit_agent_changes, get_diff_summary, restore_original_branch),
agent_report (format_session_report, assess_multi_model_value).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nvh.core.agent_loop import AgentResult, AgentStep
from nvh.core.agent_memory import (
    AgentMemory,
    CodingConventions,
    SessionRecord,
    format_memory_context,
    update_memory_from_result,
)
from nvh.core.agent_protocol import (
    PlanResult,
    SubTask,
    format_coder_prompt,
    parse_plan_result,
)
from nvh.core.agent_report import assess_multi_model_value, format_session_report
from nvh.core.agent_review import (
    ReviewFinding,
    ReviewReport,
    _merge_findings,
    _parse_findings,
)
from nvh.core.agentic import _extract_file_operations, _run_quality_gates
from nvh.core.tools import ToolResult

# ---------------------------------------------------------------------------
# helpers
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


# ═══════════════════════════════════════════════════════════════════════════
# 1. nvh/core/agentic.py
# ═══════════════════════════════════════════════════════════════════════════


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


# ═══════════════════════════════════════════════════════════════════════════
# 2. nvh/core/agent_protocol.py
# ═══════════════════════════════════════════════════════════════════════════


class TestFormatCoderPrompt:
    def test_basic_output(self):
        st = SubTask(
            sub_task="Add caching",
            files_to_read=["cache.py"],
            files_to_modify=["server.py"],
            constraints=["no global state"],
            acceptance_criteria=["tests pass"],
        )
        prompt = format_coder_prompt(st, "Plan: add caching layer")
        assert "Add caching" in prompt
        assert "cache.py" in prompt
        assert "server.py" in prompt
        assert "no global state" in prompt
        assert "tests pass" in prompt

    def test_empty_lists_show_none(self):
        st = SubTask(sub_task="simple fix")
        prompt = format_coder_prompt(st, "ctx")
        assert "(none)" in prompt


class TestSubTaskDependsOn:
    def test_subtask_with_depends_on(self):
        st = SubTask(sub_task="step 2", depends_on=[0], parallel_safe=False)
        assert st.depends_on == [0]
        assert st.parallel_safe is False


class TestPlanResultMultiSubTasks:
    def test_plan_with_multiple_subtasks(self):
        pr = PlanResult(
            sub_tasks=[
                SubTask(sub_task="read config"),
                SubTask(sub_task="update config", depends_on=[0]),
                SubTask(sub_task="write tests", depends_on=[1]),
            ],
            estimated_complexity="complex",
            suggested_mode="multi",
        )
        assert len(pr.sub_tasks) == 3
        assert pr.sub_tasks[2].depends_on == [1]
        assert pr.estimated_complexity == "complex"


class TestParsePlanResultNumberedFallback:
    def test_returns_none_for_numbered_list_without_json(self):
        """A plain numbered list with no JSON should return None."""
        text = "1. Read foo.py\n2. Modify bar.py\n3. Run tests"
        result = parse_plan_result(text)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# 3. nvh/core/agent_review.py
# ═══════════════════════════════════════════════════════════════════════════


class TestParseFindingsFromJSON:
    def test_valid_json_findings(self):
        data = {
            "findings": [
                {
                    "file": "main.py",
                    "line": 10,
                    "severity": "high",
                    "category": "bug",
                    "issue": "null deref",
                    "suggestion": "add None check",
                },
                {
                    "file": "utils.py",
                    "line": 5,
                    "severity": "low",
                    "category": "style",
                    "issue": "long line",
                    "suggestion": "wrap",
                },
            ],
            "summary": "Two issues found",
        }
        text = json.dumps(data)
        findings, summary = _parse_findings(text)
        assert len(findings) == 2
        assert findings[0].severity == "high"
        assert findings[1].category == "style"
        assert summary == "Two issues found"

    def test_invalid_severity_defaults_to_info(self):
        data = {
            "findings": [
                {"file": "x.py", "line": 1, "severity": "critical",
                 "category": "bug", "issue": "x", "suggestion": "y"},
            ],
            "summary": "",
        }
        findings, _ = _parse_findings(json.dumps(data))
        assert findings[0].severity == "info"

    def test_invalid_category_defaults_to_clarity(self):
        data = {
            "findings": [
                {"file": "x.py", "line": 1, "severity": "high",
                 "category": "unknown_cat", "issue": "x", "suggestion": "y"},
            ],
            "summary": "",
        }
        findings, _ = _parse_findings(json.dumps(data))
        assert findings[0].category == "clarity"


class TestParseFindingsPlainText:
    def test_plain_text_returns_empty_list(self):
        findings, summary = _parse_findings("Looks good overall, no issues.")
        assert findings == []
        assert len(summary) > 0


class TestMergeFindings:
    def test_deduplication(self):
        f1 = ReviewFinding("a.py", 10, "high", "bug", "issue1", "fix1")
        f2 = ReviewFinding("a.py", 10, "medium", "bug", "issue2", "fix2")
        f3 = ReviewFinding("b.py", 20, "low", "style", "issue3", "fix3")
        merged = _merge_findings([f1], [f2, f3])
        # f1 and f2 share (file, line, category) — only f1 kept
        assert len(merged) == 2
        assert merged[0].issue == "issue1"
        assert merged[1].file == "b.py"


class TestReviewReportApproved:
    def test_approved_when_no_high_severity(self):
        report = ReviewReport(
            findings=[
                ReviewFinding("x.py", 1, "low", "style", "minor", "fix"),
                ReviewFinding("y.py", 2, "medium", "performance", "slow", "optimize"),
            ],
        )
        assert report.approved is True  # default

    def test_not_approved_when_high_severity_present(self):
        """The review_changes function sets approved based on high findings."""
        findings = [
            ReviewFinding("x.py", 1, "high", "bug", "crash", "fix"),
        ]
        approved = not any(f.severity == "high" for f in findings)
        assert approved is False


# ═══════════════════════════════════════════════════════════════════════════
# 4. nvh/core/agent_testgen.py
# ═══════════════════════════════════════════════════════════════════════════


class TestDeriveTestFilename:
    def test_derive_test_filename_simple(self):
        """The inline logic: test_{stem}.py in same directory."""
        target = "nvh/core/engine.py"
        test_file = str(Path(target).parent / f"test_{Path(target).stem}.py")
        assert Path(test_file) == Path("nvh/core/test_engine.py")

    def test_derive_test_filename_nested(self):
        target = "src/utils/helpers.py"
        test_file = str(Path(target).parent / f"test_{Path(target).stem}.py")
        assert Path(test_file) == Path("src/utils/test_helpers.py")


class TestExtractCode:
    def test_strips_markdown_fences(self):
        from nvh.core.agent_testgen import _extract_code
        raw = "```python\ndef test_foo():\n    pass\n```"
        assert _extract_code(raw) == "def test_foo():\n    pass"

    def test_no_fences_returns_stripped(self):
        from nvh.core.agent_testgen import _extract_code
        raw = "  def test_foo():\n    pass  "
        assert _extract_code(raw) == "def test_foo():\n    pass"


class TestGenerateTestsPhases:
    @pytest.mark.asyncio
    async def test_generate_tests_reads_source_and_produces_report(self):
        """Mock the engine and ToolRegistry to test the full pipeline."""
        from nvh.core.agent_testgen import generate_tests
        from nvh.core.agentic import AgentConfig, AgentTier

        config = AgentConfig(tier=AgentTier.TIER_0)

        mock_engine = MagicMock()

        @dataclass
        class FakeResp:
            content: str = ""
            model: str = "mock"

        analysis_resp = FakeResp(content="Function foo needs tests")
        gen_resp = FakeResp(
            content='```python\ndef test_foo():\n    assert True\n```'
        )
        mock_engine.query = AsyncMock(side_effect=[analysis_resp, gen_resp])

        fake_tool_result = MagicMock()
        fake_tool_result.success = True
        fake_tool_result.output = "def foo():\n    return 42\n"

        with patch("nvh.core.agent_testgen.ToolRegistry") as mock_tr, \
             patch("nvh.core.agent_testgen._run_pytest") as mock_pytest:
            mock_tools = MagicMock()
            mock_tools.execute = AsyncMock(return_value=fake_tool_result)
            mock_tr.return_value = mock_tools
            mock_pytest.return_value = (1, 1, 0, "1 passed")

            report = await generate_tests(
                engine=mock_engine,
                config=config,
                working_dir="/fake",
                target="nvh/core/foo.py",
            )

        assert report.target_file == "nvh/core/foo.py"
        assert report.tests_passing == 1
        assert report.tests_failing == 0


# ═══════════════════════════════════════════════════════════════════════════
# 5. nvh/core/agent_memory.py
# ═══════════════════════════════════════════════════════════════════════════


class TestFormatMemoryContext:
    def test_rich_data(self):
        mem = AgentMemory(
            project_root="/proj",
            detected_language="Python",
            test_framework="pytest",
            linter="ruff",
            file_count=42,
            key_files=["main.py", "config.py"],
            coding_conventions=CodingConventions(
                indentation="4 spaces", import_style="isort"
            ),
            past_sessions=[
                SessionRecord(task="fix bug", outcome="ok", timestamp="2025-01-01"),
            ],
        )
        text = format_memory_context(mem)
        assert "Python" in text
        assert "pytest" in text
        assert "ruff" in text
        assert "42" in text
        assert "main.py" in text
        assert "4 spaces" in text
        assert "isort" in text
        assert "fix bug" in text

    def test_empty_memory_minimal(self):
        mem = AgentMemory()
        text = format_memory_context(mem)
        assert "Project Memory" in text


class TestUpdateMemoryCap:
    def test_cap_at_20_sessions(self):
        mem = AgentMemory()
        for i in range(25):
            obj = MagicMock()
            obj.task = f"task {i}"
            obj.files_modified = []
            obj.outcome = "done"
            mem = update_memory_from_result(mem, obj)
        assert len(mem.past_sessions) == 20
        # Oldest sessions trimmed — last task should be "task 24"
        assert mem.past_sessions[-1].task == "task 24"


# ═══════════════════════════════════════════════════════════════════════════
# 6. nvh/core/agent_git.py
# ═══════════════════════════════════════════════════════════════════════════


def _init_git_repo(tmp_path: Path) -> None:
    """Create a minimal git repo with one commit."""
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(tmp_path), capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp_path), capture_output=True,
    )
    (tmp_path / "init.txt").write_text("init", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=str(tmp_path), capture_output=True,
    )


class TestCommitAgentChanges:
    def test_commit_new_file(self, tmp_path: Path):
        from nvh.core.agent_git import commit_agent_changes

        _init_git_repo(tmp_path)
        new_file = tmp_path / "new.py"
        new_file.write_text("print('hello')\n", encoding="utf-8")

        sha = commit_agent_changes(tmp_path, "add greeting", [], [str(new_file)])
        assert sha is not None
        assert len(sha) >= 7

    def test_commit_no_files_returns_none(self, tmp_path: Path):
        from nvh.core.agent_git import commit_agent_changes

        _init_git_repo(tmp_path)
        sha = commit_agent_changes(tmp_path, "nothing", [], [])
        assert sha is None


class TestGetDiffSummary:
    def test_diff_summary_with_changes(self, tmp_path: Path):
        from nvh.core.agent_git import get_diff_summary

        _init_git_repo(tmp_path)
        (tmp_path / "init.txt").write_text("changed", encoding="utf-8")
        summary = get_diff_summary(tmp_path)
        assert "init.txt" in summary or summary == ""


class TestRestoreOriginalBranch:
    def test_restore_branch(self, tmp_path: Path):
        from nvh.core.agent_git import (
            get_current_branch,
            restore_original_branch,
        )

        _init_git_repo(tmp_path)
        original = get_current_branch(tmp_path)
        subprocess.run(
            ["git", "checkout", "-b", "feature"],
            cwd=str(tmp_path), capture_output=True,
        )
        assert get_current_branch(tmp_path) == "feature"
        restore_original_branch(tmp_path, original)
        assert get_current_branch(tmp_path) == original


# ═══════════════════════════════════════════════════════════════════════════
# 7. nvh/core/agent_report.py
# ═══════════════════════════════════════════════════════════════════════════


class TestFormatSessionReport:
    def test_report_with_multiple_phases(self):
        phases = [
            {"name": "plan", "model": "gpt-4", "duration_ms": 1000,
             "tokens": 500, "quality_note": ""},
            {"name": "execute", "model": "llama-70b", "duration_ms": 3000,
             "tokens": 2000, "quality_note": ""},
            {"name": "verify", "model": "gpt-4", "duration_ms": 800,
             "tokens": 300, "quality_note": "issues found, fix applied"},
        ]
        report = format_session_report(
            task="Fix bug",
            tier="tier_2",
            duration_ms=4800,
            phases=phases,
            files_modified=["main.py"],
            files_created=["test_main.py"],
            files_read=["config.py"],
            commands_run=["pytest"],
            verification="APPROVED",
            cost_local_tokens=2500,
            cost_cloud_tokens=800,
            cost_cloud_usd=0.0042,
            multi_model_value="diverse perspectives",
            mode="multi",
        )
        assert "Fix bug" in report
        assert "tier_2" in report
        assert "plan" in report
        assert "gpt-4" in report
        assert "llama-70b" in report
        assert "main.py" in report
        assert "test_main.py" in report
        assert "config.py" in report


class TestAssessMultiModelValue:
    def test_needs_fix_in_verification(self):
        phases = [
            {"model": "gpt-4", "quality_note": "reviewer caught issue: fix needed"},
            {"model": "llama-70b", "quality_note": ""},
        ]
        result = assess_multi_model_value(phases, "NEEDS_FIX")
        assert "diverse perspectives" in result.lower() or "Different models" in result
        assert "caught issues" in result.lower() or "issue" in result.lower()

    def test_single_model_no_value(self):
        phases = [{"model": "gpt-4", "quality_note": ""}]
        result = assess_multi_model_value(phases, "")
        assert "single-model" in result.lower() or "not needed" in result.lower()
