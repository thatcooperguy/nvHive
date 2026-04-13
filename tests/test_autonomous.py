"""Tests for nvh.core.autonomous — autonomous execution engine."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nvh.core.autonomous import (
    AutonomousConfig,
    ExecutionReport,
    format_execution_report,
    run_autonomous,
)


@dataclass
class _Resp:
    content: str


def _engine(responses: list[str]) -> MagicMock:
    e = MagicMock()
    e.query = AsyncMock(side_effect=[_Resp(r) for r in responses])
    return e


def test_autonomous_config_defaults():
    cfg = AutonomousConfig()
    assert cfg.max_plan_revisions == 2
    assert cfg.max_execution_steps == 15
    assert cfg.run_tests_after is True
    assert cfg.require_pre_qa_approval is True
    assert cfg.auto_commit is False
    assert cfg.auto_pr is False


def test_execution_report_construction():
    r = ExecutionReport(
        task="Add logging", plan="1. Find\n2. Add",
        pre_qa_verdict="APPROVED", pre_qa_feedback="OK",
        steps_completed=2, steps_total=5,
        post_qa_summary="Done", post_qa_verdict="PASSED",
        files_modified=["app.py"], files_created=["log.py"],
        tests_run=3, tests_passed=3, tests_failed=0,
        warnings=[], duration_ms=1234, cost_usd=Decimal("0.05"),
    )
    assert r.task == "Add logging"
    assert r.pre_qa_verdict == "APPROVED"
    assert r.steps_completed == 2
    assert r.tests_passed == 3
    assert r.cost_usd == Decimal("0.05")


def test_format_execution_report_non_empty():
    r = ExecutionReport(
        task="Refactor module", plan="1. Read\n2. Refactor",
        pre_qa_verdict="APPROVED", pre_qa_feedback="",
        steps_completed=4, steps_total=10,
        post_qa_summary="Completed", post_qa_verdict="PASSED",
        files_modified=["core.py"], warnings=["lint issue"], duration_ms=500,
    )
    text = format_execution_report(r)
    assert "AUTONOMOUS EXECUTION REPORT" in text
    assert "Refactor module" in text
    assert "4/10 steps" in text
    assert "PASSED" in text
    assert "lint issue" in text
    assert "500ms" in text


@pytest.mark.asyncio
async def test_run_autonomous_plan_rejected():
    report = await run_autonomous("Do something", _engine([""]),
                                  AutonomousConfig(), Path("."))
    assert report.pre_qa_verdict == "REJECTED"
    assert report.post_qa_verdict == "FAILED"
    assert report.steps_completed == 0


@pytest.mark.asyncio
async def test_run_autonomous_qa_rejects_plan():
    report = await run_autonomous(
        "Dangerous task",
        _engine(["1. Step one\n2. Step two", "REJECT: too dangerous"]),
        AutonomousConfig(), Path("."))
    assert report.pre_qa_verdict == "REJECTED"
    assert "dangerous" in report.pre_qa_feedback.lower()


@pytest.mark.asyncio
async def test_run_autonomous_happy_path():
    report = await run_autonomous(
        "Fix bug",
        _engine(["1. Read\n2. Edit", "APPROVE — good",
                 "Task complete.", "PASSED. All correct."]),
        AutonomousConfig(run_tests_after=False, max_execution_steps=5),
        Path("."))
    assert report.pre_qa_verdict == "APPROVED"
    assert report.post_qa_verdict == "PASSED"
    assert report.plan.startswith("1.")


@pytest.mark.asyncio
async def test_run_autonomous_no_pre_qa():
    report = await run_autonomous(
        "Quick fix",
        _engine(["1. Do it", "Done.", "PASSED — good."]),
        AutonomousConfig(require_pre_qa_approval=False, run_tests_after=False),
        Path("."))
    assert report.pre_qa_verdict == "APPROVED"
    assert report.post_qa_verdict == "PASSED"
