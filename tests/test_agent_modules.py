"""Tests for agent_protocol, agent_review, and agent_testgen modules."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from nvh.core.agent_protocol import (
    ChangeRecord,
    CoderResult,
    PlanResult,
    ReviewResult,
    format_plan_prompt,
    format_review_prompt,
    parse_plan_result,
    parse_review_result,
)
from nvh.core.agent_review import ReviewFinding, ReviewReport, get_diff
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
