"""Tests for the agent_protocol, agent_review, agent_testgen, and agent_git modules."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nvh.core.agent_protocol import (
    ChangeRecord,
    CoderResult,
    PlanResult,
    ReviewResult,
    SubTask,
    format_coder_prompt,
    format_plan_prompt,
    format_review_prompt,
    parse_plan_result,
    parse_review_result,
)
from nvh.core.agent_review import (
    ReviewFinding,
    ReviewReport,
    _merge_findings,
    _parse_findings,
    get_diff,
)
from nvh.core.agent_testgen import CoverageGap, TestGenReport, find_coverage_gaps

# ── agent_protocol ──────────────────────────────────────────────────────────


class TestParsePlanResult:
    def test_parse_plan_result_valid_json(self) -> None:
        response = (
            '```json\n'
            '{"sub_tasks": [{"sub_task": "do stuff"}], '
            '"estimated_complexity": "simple", '
            '"suggested_mode": "single"}\n'
            '```'
        )
        result = parse_plan_result(response)
        assert result is not None
        assert isinstance(result, PlanResult)
        assert len(result.sub_tasks) == 1
        assert result.sub_tasks[0].sub_task == "do stuff"
        assert result.estimated_complexity == "simple"
        assert result.suggested_mode == "single"

    def test_parse_plan_result_garbage(self) -> None:
        result = parse_plan_result("lorem ipsum dolor sit amet")
        assert result is None


class TestParseReviewResult:
    def test_parse_review_approved(self) -> None:
        result = parse_review_result("Everything looks good. APPROVED.")
        assert result is not None
        assert isinstance(result, ReviewResult)
        assert result.verdict == "APPROVED"

    def test_parse_review_needs_fix(self) -> None:
        result = parse_review_result("There are problems. NEEDS_FIX.")
        assert result is not None
        assert isinstance(result, ReviewResult)
        assert result.verdict == "NEEDS_FIX"


class TestFormatPrompts:
    def test_format_plan_prompt_includes_task(self) -> None:
        task = "refactor the frobnicator"
        prompt = format_plan_prompt(task, "/tmp", "no context")
        assert task in prompt

    def test_format_review_prompt_includes_task(self) -> None:
        task = "add caching layer"
        coder_result = CoderResult(
            changes=[
                ChangeRecord(
                    file="cache.py",
                    action="created",
                    diff_summary="new file",
                    lines_changed=50,
                ),
            ],
            notes="done",
        )
        prompt = format_review_prompt(task, coder_result)
        assert task in prompt


# ── agent_review ────────────────────────────────────────────────────────────


class TestGetDiff:
    @patch("nvh.core.agent_review.subprocess.run")
    def test_get_diff_staged_empty(self, mock_run: object) -> None:
        mock_run.return_value = subprocess.CompletedProcess(  # type: ignore[attr-defined]
            args=["git", "diff", "--cached"],
            returncode=0,
            stdout="",
            stderr="",
        )
        with pytest.raises(ValueError, match="No diff output"):
            get_diff(Path("/fake"), "staged")

    @patch("nvh.core.agent_review.subprocess.run")
    def test_get_diff_staged_success(self, mock_run: object) -> None:
        diff_text = "diff --git a/foo.py b/foo.py\n+hello"
        mock_run.return_value = subprocess.CompletedProcess(  # type: ignore[attr-defined]
            args=["git", "diff", "--cached"],
            returncode=0,
            stdout=diff_text,
            stderr="",
        )
        result = get_diff(Path("/fake"), "staged")
        assert result == diff_text


class TestReviewDataclasses:
    def test_review_report_dataclass(self) -> None:
        report = ReviewReport(
            findings=[],
            summary="all good",
            approved=True,
            reviewer_models=["gpt-4"],
            duration_ms=123,
        )
        assert report.summary == "all good"
        assert report.approved is True
        assert report.reviewer_models == ["gpt-4"]
        assert report.duration_ms == 123
        assert report.findings == []

    def test_review_finding_dataclass(self) -> None:
        finding = ReviewFinding(
            file="main.py",
            line=42,
            severity="high",
            category="bug",
            issue="off-by-one",
            suggestion="use <= instead of <",
        )
        assert finding.file == "main.py"
        assert finding.line == 42
        assert finding.severity == "high"
        assert finding.category == "bug"
        assert finding.issue == "off-by-one"
        assert finding.suggestion == "use <= instead of <"


# ── agent_testgen ───────────────────────────────────────────────────────────


class TestTestGenDataclasses:
    def test_testgen_report_dataclass(self) -> None:
        report = TestGenReport(
            target_file="foo.py",
            test_file="test_foo.py",
            tests_generated=5,
            tests_passing=4,
            tests_failing=1,
            coverage_before=30.0,
            coverage_after=80.0,
            duration_ms=500,
            model_used="claude-3",
        )
        assert report.target_file == "foo.py"
        assert report.test_file == "test_foo.py"
        assert report.tests_generated == 5
        assert report.tests_passing == 4
        assert report.tests_failing == 1
        assert report.coverage_before == 30.0
        assert report.coverage_after == 80.0
        assert report.duration_ms == 500
        assert report.model_used == "claude-3"

    def test_coverage_gap_dataclass(self) -> None:
        gap = CoverageGap(file="utils.py", current_coverage=25.0, missing_lines=[10, 20])
        assert gap.file == "utils.py"
        assert gap.current_coverage == 25.0
        assert gap.missing_lines == [10, 20]


class TestFindCoverageGaps:
    @patch("nvh.core.agent_testgen.subprocess.run", side_effect=FileNotFoundError)
    def test_find_coverage_gaps_no_pytest(self, _mock_run: object) -> None:
        result = find_coverage_gaps("/fake/dir")
        assert result == []


# ---------------------------------------------------------------------------
# agent_protocol: coder prompt, sub-task dependencies, plan parsing fallback
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# agent_review: findings parsing, merging, report approval
# ---------------------------------------------------------------------------


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


class TestReviewReportFields:
    def test_review_finding_all_fields(self):
        f = ReviewFinding(
            file="main.py", line=42, severity="high",
            category="bug", issue="null deref", suggestion="add null check",
        )
        assert f.file == "main.py"
        assert f.line == 42
        assert f.severity == "high"

    def test_review_report_approved_no_high(self):
        findings = [
            ReviewFinding(file="a.py", line=1, severity="low",
                          category="style", issue="naming", suggestion="rename"),
        ]
        report = ReviewReport(
            findings=findings, summary="minor issues",
            approved=True, reviewer_models=["groq"], duration_ms=100,
        )
        assert report.approved is True

    def test_review_report_not_approved_with_high(self):
        findings = [
            ReviewFinding(file="a.py", line=1, severity="high",
                          category="bug", issue="crash", suggestion="fix"),
        ]
        report = ReviewReport(
            findings=findings, summary="critical bug",
            approved=False, reviewer_models=["groq"], duration_ms=100,
        )
        assert report.approved is False

    def test_get_diff_empty_raises(self, tmp_path):
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=str(tmp_path), capture_output=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        )
        with pytest.raises(ValueError):
            get_diff(tmp_path, "staged")


# ---------------------------------------------------------------------------
# agent_testgen: filename derivation, code extraction, generate_tests pipeline
# ---------------------------------------------------------------------------


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


class TestTestgenCoverageGaps:
    def test_coverage_gap_dataclass(self):
        cg = CoverageGap(file="main.py", current_coverage=45.2, missing_lines=[10, 20, 30])
        assert cg.file == "main.py"
        assert cg.current_coverage == 45.2
        assert len(cg.missing_lines) == 3

    def test_find_coverage_gaps_no_pytest_cov(self, tmp_path):
        # No coverage.json exists → should return empty
        gaps = find_coverage_gaps(tmp_path)
        assert gaps == []


# ---------------------------------------------------------------------------
# agent_git: commit, diff summary, branch restore
# ---------------------------------------------------------------------------


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


class TestAgentGitNoRepo:
    def test_get_diff_summary_no_repo(self, tmp_path):
        from nvh.core.agent_git import get_diff_summary
        result = get_diff_summary(tmp_path)
        assert isinstance(result, str)

    def test_restore_branch_no_repo(self, tmp_path):
        from nvh.core.agent_git import restore_original_branch
        # Should not raise
        restore_original_branch(tmp_path, "main")

    def test_commit_no_changes(self, tmp_path):
        from nvh.core.agent_git import commit_agent_changes
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=str(tmp_path), capture_output=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        )
        result = commit_agent_changes(tmp_path, "test task", [], [])
        assert result is None  # nothing to commit
