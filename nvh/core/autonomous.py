"""Autonomous execution engine — plan, QA, execute, verify, report.

Takes a high-level task and works toward completion WITHOUT user input:
1. PLAN — orchestrator LLM creates a step-by-step plan
2. PRE-QA — different LLM reviews the plan for risks and gaps
3. EXECUTE — worker LLM executes each step using tools
4. POST-QA — reviewer LLM verifies what was done
5. REPORT — structured summary of outcomes
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from nvh.core.agent_loop import AgentResult, run_agent_loop
from nvh.core.tools import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class AutonomousConfig:
    """Configuration for an autonomous execution run."""
    max_plan_revisions: int = 2
    max_execution_steps: int = 15
    run_tests_after: bool = True
    require_pre_qa_approval: bool = True
    auto_commit: bool = False
    auto_pr: bool = False


@dataclass
class ExecutionReport:
    """Structured outcome of an autonomous run."""
    task: str
    plan: str
    pre_qa_verdict: str       # APPROVED | REVISED | REJECTED
    pre_qa_feedback: str
    steps_completed: int
    steps_total: int
    post_qa_summary: str
    post_qa_verdict: str      # PASSED | PARTIAL | FAILED
    files_modified: list[str] = field(default_factory=list)
    files_created: list[str] = field(default_factory=list)
    tests_run: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    warnings: list[str] = field(default_factory=list)
    duration_ms: int = 0
    cost_usd: Decimal = Decimal("0")


_PLAN_PROMPT = (
    "You are an expert software engineering planner.\n"
    "Produce a numbered step-by-step plan for the task.  Per step include: "
    "what to do, expected outcome, risk (low/medium/high).\n\n"
    "Task: {task}\nWorking directory: {working_dir}\n\n"
    "Respond with the plan ONLY."
)

_QA_REVIEW_PROMPT = (
    "You are a senior QA reviewer.  Review this plan for completeness, "
    "safety, feasibility, and error handling.\n\n"
    "Task: {task}\nPlan:\n{plan}\n\n{feedback_context}\n\n"
    "Respond with EXACTLY one of:\n"
    "  APPROVE — plan is solid\n"
    "  REVISE: <feedback>\n"
    "  REJECT: <reason>"
)

_POST_QA_PROMPT = (
    "You are a QA reviewer verifying completed work.\n\n"
    "Task: {task}\nPlan:\n{plan}\n\nChanges:\n{changes}\n\n"
    "Test results:\n{test_results}\n\n"
    "Did the agent accomplish the task?  What worked, what's incomplete, "
    "what risks remain?  End with a verdict: PASSED, PARTIAL, or FAILED"
)


def _parse_qa_verdict(response: str) -> tuple[str, str]:
    """Extract verdict and feedback from a QA response."""
    text = response.strip()
    upper = text.upper()
    for prefix, verdict in [("APPROVE", "APPROVED"), ("REJECT", "REJECTED"), ("REVISE", "REVISED")]:
        if upper.startswith(prefix) or prefix in upper:
            return verdict, text
    return "REJECTED", text


def _parse_post_qa_verdict(response: str) -> str:
    from nvh.core.agent_protocol import parse_qa_verdict

    return parse_qa_verdict(response)


def _resp_text(response: Any) -> str:
    return response.content.strip() if hasattr(response, "content") else str(response)


async def _run_tests(working_dir: Path) -> tuple[int, int, int, str]:
    """Run pytest and return (run, passed, failed, output)."""
    try:
        result = subprocess.run(
            ["python", "-m", "pytest", "--tb=short", "-q"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", cwd=str(working_dir), timeout=120,
        )
        output = (result.stdout or "") + (result.stderr or "")
        passed = int(m.group(1)) if (m := re.search(r"(\d+) passed", output)) else 0
        failed = int(m.group(1)) if (m := re.search(r"(\d+) failed", output)) else 0
        return passed + failed, passed, failed, output
    except Exception as exc:
        return 0, 0, 0, f"pytest error: {exc}"


def _collect_changes(agent_result: AgentResult | None) -> tuple[list[str], list[str], str]:
    """Extract modified/created file lists and a change summary."""
    if agent_result is None:
        return [], [], "No execution performed."
    modified: list[str] = []
    created: list[str] = []
    for step in agent_result.steps:
        for call in step.tool_calls:
            tool, path = call.get("tool", ""), call.get("args", {}).get("path", "")
            if not path:
                continue
            if tool == "write_file" and path not in created and path not in modified:
                created.append(path)
            elif tool in ("edit_file", "replace_in_file") and path not in modified:
                modified.append(path)
    parts = []
    if modified:
        parts.append(f"Modified: {', '.join(modified)}")
    if created:
        parts.append(f"Created: {', '.join(created)}")
    return modified, created, "\n".join(parts) or "No file changes."


ProgressCallback = Callable[[str, str, float], Any]


def _abort_report(task: str, plan: str, verdict: str, feedback: str,
                  warnings: list[str], elapsed: int) -> ExecutionReport:
    return ExecutionReport(
        task=task, plan=plan, pre_qa_verdict=verdict,
        pre_qa_feedback=feedback, steps_completed=0, steps_total=0,
        post_qa_summary="", post_qa_verdict="FAILED",
        warnings=warnings, duration_ms=elapsed,
    )


async def run_autonomous(
    task: str,
    engine: Any,
    config: AutonomousConfig | None = None,
    working_dir: Path | None = None,
    on_progress: ProgressCallback | None = None,
) -> ExecutionReport:
    """Run the full autonomous pipeline: plan -> pre-QA -> execute -> post-QA -> report."""
    cfg = config or AutonomousConfig()
    wdir = working_dir or Path(".")
    t0 = time.perf_counter_ns()
    elapsed = lambda: (time.perf_counter_ns() - t0) // 1_000_000  # noqa: E731

    def _progress(phase: str, message: str, pct: float) -> None:
        if on_progress:
            on_progress(phase, message, pct)

    # ── Phase 1: PLAN ─────────────────────────────────────────────
    _progress("PLAN", "Generating execution plan...", 0.0)
    plan = _resp_text(await engine.query(
        prompt=_PLAN_PROMPT.format(task=task, working_dir=wdir), stream=False))
    if not plan:
        return _abort_report(task, "", "REJECTED",
                             "Orchestrator produced an empty plan.",
                             ["Empty plan generated"], elapsed())

    # ── Phase 2: PRE-QA ───────────────────────────────────────────
    _progress("PRE-QA", "Reviewing plan...", 0.15)
    pre_qa_verdict, pre_qa_feedback, feedback_ctx = "APPROVED", "", ""

    if cfg.require_pre_qa_approval:
        for rnd in range(cfg.max_plan_revisions + 1):
            qa_text = _resp_text(await engine.query(
                prompt=_QA_REVIEW_PROMPT.format(
                    task=task, plan=plan, feedback_context=feedback_ctx),
                stream=False))
            pre_qa_verdict, pre_qa_feedback = _parse_qa_verdict(qa_text)

            if pre_qa_verdict == "APPROVED":
                break
            if pre_qa_verdict == "REJECTED":
                return _abort_report(task, plan, "REJECTED", pre_qa_feedback,
                                     ["Plan rejected by QA reviewer"], elapsed())
            if rnd < cfg.max_plan_revisions:
                _progress("PRE-QA", f"Revising plan (round {rnd + 1})...", 0.20)
                feedback_ctx = f"Previous QA feedback:\n{qa_text}"
                plan = _resp_text(await engine.query(
                    prompt=_PLAN_PROMPT.format(task=task, working_dir=wdir)
                    + f"\n\n{feedback_ctx}", stream=False))
            else:
                return _abort_report(
                    task, plan, "REJECTED",
                    f"Not approved after {cfg.max_plan_revisions} revisions: {pre_qa_feedback}",
                    ["Max plan revisions exhausted"], elapsed())

    # ── Phase 3: EXECUTE ──────────────────────────────────────────
    _progress("EXECUTE", "Executing approved plan...", 0.30)
    tools = ToolRegistry(workspace=str(wdir))
    agent_result: AgentResult | None = None
    try:
        agent_result = await run_agent_loop(
            task=f"Execute this plan step by step:\n\n{plan}\n\nOriginal task: {task}",
            engine=engine, tools=tools, max_iterations=cfg.max_execution_steps)
    except Exception as exc:
        logger.error("Execution failed: %s", exc)

    steps_completed = agent_result.total_iterations if agent_result else 0
    modified, created, changes_summary = _collect_changes(agent_result)

    # ── Phase 4: POST-QA ──────────────────────────────────────────
    _progress("POST-QA", "Verifying results...", 0.75)
    test_output, tests_run, tests_passed, tests_failed = "Tests skipped.", 0, 0, 0
    if cfg.run_tests_after and (modified or created):
        tests_run, tests_passed, tests_failed, test_output = await _run_tests(wdir)

    post_qa_text = _resp_text(await engine.query(
        prompt=_POST_QA_PROMPT.format(
            task=task, plan=plan, changes=changes_summary, test_results=test_output),
        stream=False))
    post_qa_verdict = _parse_post_qa_verdict(post_qa_text)

    # ── Phase 5: REPORT ───────────────────────────────────────────
    _progress("REPORT", "Assembling report...", 0.90)
    warnings: list[str] = []
    if agent_result and not agent_result.completed:
        warnings.append("Agent loop hit iteration limit or error")
    if tests_failed > 0:
        warnings.append(f"{tests_failed} test(s) failed")

    report = ExecutionReport(
        task=task, plan=plan,
        pre_qa_verdict=pre_qa_verdict, pre_qa_feedback=pre_qa_feedback,
        steps_completed=steps_completed, steps_total=cfg.max_execution_steps,
        post_qa_summary=post_qa_text, post_qa_verdict=post_qa_verdict,
        files_modified=modified, files_created=created,
        tests_run=tests_run, tests_passed=tests_passed, tests_failed=tests_failed,
        warnings=warnings, duration_ms=elapsed())

    if cfg.auto_commit and (modified or created):
        _progress("REPORT", "Committing changes...", 0.95)
        try:
            subprocess.run(["git", "add"] + modified + created,
                           cwd=str(wdir), capture_output=True, timeout=30)
            subprocess.run(["git", "commit", "-m", f"auto: {task[:72]}"],
                           cwd=str(wdir), capture_output=True, timeout=30)
        except Exception as exc:
            warnings.append(f"Auto-commit failed: {exc}")

    if cfg.auto_pr and (modified or created):
        _progress("REPORT", "Creating PR...", 0.98)
        try:
            from nvh.core.agent_pr import create_pr
            await create_pr(engine, task, wdir)
        except Exception as exc:
            warnings.append(f"Auto-PR failed: {exc}")

    _progress("REPORT", "Done.", 1.0)
    return report


def format_execution_report(report: ExecutionReport) -> str:
    """Return a formatted text summary of the execution report."""
    lines = [
        "=" * 60, "AUTONOMOUS EXECUTION REPORT", "=" * 60,
        f"Task: {report.task}", "-" * 60,
        f"Plan verdict: {report.pre_qa_verdict}",
    ]
    if report.pre_qa_feedback:
        lines.append(f"QA feedback: {report.pre_qa_feedback[:200]}")
    lines += ["-" * 60, f"Execution: {report.steps_completed}/{report.steps_total} steps"]
    if report.files_modified:
        lines.append(f"Files modified: {', '.join(report.files_modified)}")
    if report.files_created:
        lines.append(f"Files created: {', '.join(report.files_created)}")
    tests_line = f"Tests: {report.tests_passed}/{report.tests_run} passed"
    if report.tests_failed:
        tests_line += f", {report.tests_failed} failed"
    lines += ["-" * 60, tests_line, f"Post-QA verdict: {report.post_qa_verdict}"]
    if report.post_qa_summary:
        s = report.post_qa_summary
        lines.append(f"Summary: {s[:300]}{'...' if len(s) > 300 else ''}")
    if report.warnings:
        lines += ["-" * 60, "Warnings:"] + [f"  - {w}" for w in report.warnings]
    lines += ["-" * 60, f"Duration: {report.duration_ms}ms",
              f"Cost: ${report.cost_usd}", "=" * 60]
    return "\n".join(lines)
