"""Structured coordination protocol for multi-model agent handoffs.

Defines JSON schemas for communication between planner, coder, and reviewer models.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


@dataclass
class SubTask:
    """One unit of work in an agent plan."""

    sub_task: str
    files_to_read: list[str] = field(default_factory=list)
    files_to_modify: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    depends_on: list[int] = field(default_factory=list)
    parallel_safe: bool = True


@dataclass
class PlanResult:
    """Planner output: a list of sub-tasks with metadata."""

    sub_tasks: list[SubTask]
    estimated_complexity: str  # "simple" | "moderate" | "complex"
    suggested_mode: str  # "single" | "multi"


@dataclass
class ChangeRecord:
    """Coder output per file."""

    file: str
    action: str  # "modified" | "created" | "read"
    diff_summary: str
    lines_changed: int


@dataclass
class CoderResult:
    """Coder output: list of changes and notes."""

    changes: list[ChangeRecord]
    notes: str = ""


@dataclass
class ReviewIssue:
    """One issue found during review."""

    file: str
    line: int | None
    severity: str  # "high" | "medium" | "low"
    issue: str
    fix: str


@dataclass
class ReviewResult:
    """Reviewer output: verdict and issues."""

    verdict: str  # "APPROVED" | "NEEDS_FIX"
    issues: list[ReviewIssue]


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def format_plan_prompt(task: str, working_dir: str, memory_context: str) -> str:
    """Build the structured planning prompt that instructs the planner to output JSON."""
    return (
        "You are a planning agent. Analyze the task and produce a JSON plan.\n\n"
        f"## Task\n{task}\n\n"
        f"## Working Directory\n{working_dir}\n\n"
        f"## Memory Context\n{memory_context}\n\n"
        "## Output Format\n"
        "Return ONLY a JSON object with this schema:\n```json\n"
        '{"sub_tasks": [{"sub_task": "description", "files_to_read": ["path"], '
        '"files_to_modify": ["path"], "constraints": ["constraint"], '
        '"acceptance_criteria": ["criterion"], "depends_on": [], '
        '"parallel_safe": true}], '
        '"estimated_complexity": "simple|moderate|complex", '
        '"suggested_mode": "single|multi"}\n```\n'
    )


def format_coder_prompt(sub_task: SubTask, plan_context: str) -> str:
    """Build the task prompt for the coder."""
    files_read = ", ".join(sub_task.files_to_read) or "(none)"
    files_mod = ", ".join(sub_task.files_to_modify) or "(none)"
    constraints = "\n".join(f"- {c}" for c in sub_task.constraints) or "(none)"
    criteria = "\n".join(f"- {c}" for c in sub_task.acceptance_criteria) or "(none)"
    return (
        "You are a coding agent. Implement the following sub-task.\n\n"
        f"## Plan Context\n{plan_context}\n\n"
        f"## Sub-Task\n{sub_task.sub_task}\n\n"
        f"## Files to Read\n{files_read}\n\n"
        f"## Files to Modify\n{files_mod}\n\n"
        f"## Constraints\n{constraints}\n\n"
        f"## Acceptance Criteria\n{criteria}\n"
    )


def format_review_prompt(task: str, coder_result: CoderResult) -> str:
    """Build the review prompt."""
    changes_text = "\n".join(
        f"- {c.file} ({c.action}, {c.lines_changed} lines): {c.diff_summary}"
        for c in coder_result.changes
    )
    return (
        "You are a code reviewer. Review the changes below.\n\n"
        f"## Original Task\n{task}\n\n"
        f"## Changes Made\n{changes_text}\n\n"
        f"## Coder Notes\n{coder_result.notes}\n\n"
        "## Instructions\n"
        'Respond with a JSON object: {"verdict": "APPROVED|NEEDS_FIX", '
        '"issues": [{"file": "", "line": null, "severity": "high|medium|low", '
        '"issue": "", "fix": ""}]}\n'
    )


# ---------------------------------------------------------------------------
# Parsers (lenient: try JSON first, fall back to keyword matching)
# ---------------------------------------------------------------------------

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    """Try to extract a JSON object from text, with or without code fences."""
    # Try fenced code blocks first
    m = _JSON_BLOCK_RE.search(text)
    candidate = m.group(1).strip() if m else text.strip()
    # Find the outermost { ... }
    start = candidate.find("{")
    if start == -1:
        return None
    depth, end = 0, start
    for i in range(start, len(candidate)):
        if candidate[i] == "{":
            depth += 1
        elif candidate[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    try:
        return json.loads(candidate[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None


def parse_plan_result(response: str) -> PlanResult | None:
    """Extract a PlanResult from the LLM response. Returns None if unparseable."""
    data = _extract_json(response)
    if not data or "sub_tasks" not in data:
        return None
    try:
        sub_tasks = [SubTask(**st) for st in data["sub_tasks"]]
        return PlanResult(
            sub_tasks=sub_tasks,
            estimated_complexity=data.get("estimated_complexity", "moderate"),
            suggested_mode=data.get("suggested_mode", "single"),
        )
    except (TypeError, KeyError):
        return None


def parse_review_result(response: str) -> ReviewResult | None:
    """Extract a ReviewResult from the LLM response. Falls back to keyword matching."""
    data = _extract_json(response)
    if data and "verdict" in data:
        try:
            issues = [ReviewIssue(**iss) for iss in data.get("issues", [])]
            return ReviewResult(verdict=data["verdict"], issues=issues)
        except (TypeError, KeyError):
            pass
    # Fallback: keyword matching
    upper = response.upper()
    if "APPROVED" in upper:
        return ReviewResult(verdict="APPROVED", issues=[])
    if "NEEDS_FIX" in upper:
        return ReviewResult(verdict="NEEDS_FIX", issues=[])
    return None


def parse_qa_verdict(text: str) -> str:
    """Parse a QA verdict from response text.

    Looks for PASSED, PARTIAL, or FAILED in the text (case-insensitive).
    Returns the matching verdict string, defaulting to "FAILED".

    This is the single source of truth for post-QA verdict parsing,
    used by iterative_loop, autonomous, and parallel_pipeline.
    """
    upper = text.upper()
    for v in ("PASSED", "PARTIAL", "FAILED"):
        if v in upper:
            return v
    return "FAILED"
