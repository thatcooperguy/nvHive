"""Code Review Agent — multi-model review for diffs, PRs, and commits."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nvh.core.agentic import AgentConfig, AgentMode

logger = logging.getLogger(__name__)

_VALID_SEVERITIES = {"high", "medium", "low", "info"}
_VALID_CATEGORIES = {
    "bug", "security", "style", "performance", "test-gap", "clarity",
}


@dataclass
class ReviewFinding:
    """A single finding tied to a diff location."""
    file: str
    line: int | None
    severity: str   # high | medium | low | info
    category: str   # bug | security | style | performance | test-gap | clarity
    issue: str
    suggestion: str


@dataclass
class ReviewReport:
    """Aggregated review report from one or more models."""
    findings: list[ReviewFinding] = field(default_factory=list)
    summary: str = ""
    approved: bool = True
    reviewer_models: list[str] = field(default_factory=list)
    duration_ms: int = 0


def get_diff(working_dir: Path, source: str) -> str:
    """Return diff text. *source*: ``"staged"``, git range, or PR number."""
    if source == "staged":
        cmd = ["git", "diff", "--cached"]
    elif source.startswith("HEAD"):
        cmd = ["git", "diff", source]
    elif source.isdigit():
        cmd = ["gh", "pr", "diff", source]
    else:
        raise ValueError(f"Unknown diff source: {source!r}")

    result = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        cwd=str(working_dir), timeout=30,
    )
    if result.returncode != 0:
        raise ValueError(f"Diff command failed: {result.stderr.strip()}")
    diff = result.stdout.strip()
    if not diff:
        raise ValueError(f"No diff output for source {source!r}")
    return diff


_REVIEW_PROMPT = (
    "You are an expert code reviewer. Review the diff and report findings.\n"
    "RULES:\n"
    "- Focus on correctness/bugs first, security second, style last.\n"
    "- Reference specific file:line from the diff hunks.\n"
    "- Do NOT suggest changes outside the diff scope.\n"
    "- One finding per issue, be concise.\n\n"
    'Respond with JSON: {"findings": [{"file": "path", "line": 42, '
    '"severity": "high|medium|low|info", '
    '"category": "bug|security|style|performance|test-gap|clarity", '
    '"issue": "...", "suggestion": "..."}], '
    '"summary": "1-2 sentence assessment"}\n'
    "Empty findings + positive summary if the diff is correct.\n\n"
    "DIFF:\n```\n%s\n```"
)


def _parse_findings(text: str) -> tuple[list[ReviewFinding], str]:
    """Best-effort extraction of structured findings from model output."""
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end])
            def _sev(v: Any) -> str:
                return v if v in _VALID_SEVERITIES else "info"

            def _cat(v: Any) -> str:
                return v if v in _VALID_CATEGORIES else "clarity"

            findings = [
                ReviewFinding(
                    file=str(f.get("file", "")),
                    line=f.get("line"),
                    severity=_sev(f.get("severity")),
                    category=_cat(f.get("category")),
                    issue=str(f.get("issue", "")),
                    suggestion=str(f.get("suggestion", "")),
                ) for f in data.get("findings", [])
            ]
            return findings, str(data.get("summary", ""))
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
    return [], text.strip()[:500]


def _merge_findings(
    a: list[ReviewFinding], b: list[ReviewFinding],
) -> list[ReviewFinding]:
    """Deduplicate findings from two reviewers by file+line+category."""
    seen: set[tuple[str, int | None, str]] = set()
    merged: list[ReviewFinding] = []
    for f in [*a, *b]:
        key = (f.file, f.line, f.category)
        if key not in seen:
            seen.add(key)
            merged.append(f)
    return merged


async def review_changes(
    engine: Any,
    config: AgentConfig,
    working_dir: Path,
    diff_source: str,
    on_step: Callable[..., Any] | None = None,
) -> ReviewReport:
    """Review code changes using one or two models."""
    t0 = time.monotonic()

    def _step(phase: str, detail: str = "") -> None:
        if on_step is not None:
            on_step(phase, detail)

    _step("diff", f"Retrieving diff from {diff_source!r}")
    diff_text = get_diff(working_dir, diff_source)
    prompt = _REVIEW_PROMPT % diff_text[:60_000]

    # Phase 1 — orchestrator
    orch_model = config.orchestrator_model or "default"
    _step("review", f"Orchestrator review ({orch_model})")
    orch_resp = await engine.query(
        prompt=prompt, provider=config.orchestrator_provider,
        model=config.orchestrator_model, stream=False, use_cache=False,
    )
    orch_findings, orch_summary = _parse_findings(orch_resp.content)
    models_used = [orch_model]

    # Phase 2 — reviewer (multi-model only)
    reviewer_findings: list[ReviewFinding] = []
    reviewer_summary = ""
    is_multi = (
        config.mode == AgentMode.MULTI
        and config.reviewer_model
        and config.reviewer_model != config.orchestrator_model
    )
    if is_multi:
        rev_model = config.reviewer_model or "default"
        _step("review", f"Reviewer review ({rev_model})")
        rev_resp = await engine.query(
            prompt=prompt, provider=config.reviewer_provider,
            model=config.reviewer_model, stream=False, use_cache=False,
        )
        reviewer_findings, reviewer_summary = _parse_findings(
            rev_resp.content,
        )
        models_used.append(rev_model)

    # Phase 3 — synthesize
    if is_multi and (orch_findings or reviewer_findings):
        all_findings = _merge_findings(orch_findings, reviewer_findings)
        summary = (
            f"Orchestrator: {orch_summary} "
            f"| Reviewer: {reviewer_summary}"
        )
    else:
        all_findings = orch_findings
        summary = orch_summary

    approved = not any(f.severity == "high" for f in all_findings)
    elapsed = int((time.monotonic() - t0) * 1000)
    _step("done", f"{'Approved' if approved else 'Issues found'} ({elapsed}ms)")

    return ReviewReport(
        findings=all_findings, summary=summary,
        approved=approved, reviewer_models=models_used,
        duration_ms=elapsed,
    )
