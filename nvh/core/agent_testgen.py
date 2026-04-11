"""Test Generation Agent — read source, identify gaps, write pytest tests, iterate."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nvh.core.agent_loop import run_agent_loop  # noqa: F401 (public re-export)
from nvh.core.agentic import AgentConfig
from nvh.core.tools import ToolRegistry

logger = logging.getLogger(__name__)

_TEST_GEN_SYSTEM = (
    "You are a test generation expert. Write comprehensive pytest tests:\n"
    "- Use pytest (never unittest).\n"
    "- Mock external deps (network, fs, db) with unittest.mock.\n"
    "- Test happy path and error cases.\n"
    "- Names: test_<function>_<scenario>. One assertion per test when practical.\n"
    "- Add a one-line docstring per test. Output ONLY valid Python, no commentary."
)


@dataclass
class CoverageGap:
    """A source file with low test coverage."""

    file: str
    current_coverage: float
    missing_lines: list[int] = field(default_factory=list)


@dataclass
class TestGenReport:
    """Result of a test generation run."""

    target_file: str
    test_file: str
    tests_generated: int
    tests_passing: int
    tests_failing: int
    coverage_before: float | None
    coverage_after: float | None
    duration_ms: int
    model_used: str


def find_coverage_gaps(working_dir: str | Path) -> list[CoverageGap]:
    """Run ``pytest --cov`` and return files with <50% coverage."""
    wd = Path(working_dir)
    try:
        subprocess.run(
            ["python", "-m", "pytest", "--cov=nvh", "--cov-report=json", "-q"],
            cwd=wd, capture_output=True, text=True, timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    cov_path = wd / "coverage.json"
    if not cov_path.exists():
        return []
    try:
        data = json.loads(cov_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    gaps: list[CoverageGap] = []
    for fname, info in data.get("files", {}).items():
        pct = info.get("summary", {}).get("percent_covered", 100.0)
        if pct < 50:
            gaps.append(CoverageGap(fname, pct, info.get("missing_lines", [])))
    return sorted(gaps, key=lambda g: g.current_coverage)

async def generate_tests(
    engine: Any, config: AgentConfig, working_dir: str | Path,
    target: str, on_step: Any = None,
) -> TestGenReport:
    """Generate pytest tests for *target* and iterate until they pass.

    *target*: file path, ``"--coverage-gaps"``, or ``"--for-pr"``.
    """
    t0 = time.monotonic()
    wd = Path(working_dir)
    tools = ToolRegistry(workspace=str(wd))
    model_used = config.worker_model or "default"
    emit = (lambda p, d: on_step(p, d)) if on_step else (lambda p, d: None)

    # Phase 0 — resolve target
    if target == "--coverage-gaps":
        gaps = find_coverage_gaps(wd)
        if not gaps:
            return _empty(t0, "(no gaps)", model_used)
        target = gaps[0].file
    elif target == "--for-pr":
        proc = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1", "--", "*.py"],
            cwd=wd, capture_output=True, text=True, timeout=15,
        )
        changed = [f for f in proc.stdout.strip().splitlines()
                    if f and not f.startswith("test")]
        if not changed:
            return _empty(t0, "(no changed files)", model_used)
        target = changed[0]

    test_file = str(Path(target).parent / f"test_{Path(target).stem}.py")
    emit("read", target)

    # Phase 1 — read source via tool registry
    rr = await tools.execute("read_file", {"path": target})
    if not rr.success:
        return _empty(t0, target, model_used)
    source = rr.output

    # Phase 2 — identify untested code (orchestrator)
    emit("analyze", "identifying untested functions")
    analysis = await engine.query(
        prompt=f"List functions needing tests and key scenarios.\n\n```python\n{source}\n```",
        model=config.orchestrator_model, provider=config.orchestrator_provider,
        system_prompt="You are a code analyst. List untested functions and scenarios.",
    )

    # Phase 3 — generate tests (worker)
    emit("generate", "writing pytest tests")
    mod = target.replace("/", ".").replace("\\", ".").removesuffix(".py")
    gen = await engine.query(
        prompt=(
            f"Write pytest tests.\n\n# Analysis:\n{analysis.content}\n\n"
            f"# Source:\n```python\n{source}\n```\n\nImport as: from {mod} import *"
        ),
        model=config.worker_model, provider=config.worker_provider,
        system_prompt=_TEST_GEN_SYSTEM,
    )
    test_code = _extract_code(gen.content)
    model_used = gen.model

    # Phase 4 — write test file
    emit("write", test_file)
    await tools.execute("write_file", {"path": test_file, "content": test_code})

    # Phase 5 — run pytest
    emit("verify", "running pytest")
    total, passed, failed, output = _run_pytest(wd, test_file)

    # Phase 6 — retry once on failure
    if failed > 0:
        emit("retry", "fixing failing tests")
        fix = await engine.query(
            prompt=(
                f"Fix these failing tests.\n\n```python\n{test_code}\n```\n\n"
                f"Errors:\n```\n{output[-2000:]}\n```\n\nOutput ONLY corrected file."
            ),
            model=config.worker_model, provider=config.worker_provider,
            system_prompt=_TEST_GEN_SYSTEM,
        )
        test_code = _extract_code(fix.content)
        await tools.execute("write_file", {"path": test_file, "content": test_code})
        total, passed, failed, output = _run_pytest(wd, test_file)

    return TestGenReport(
        target_file=target, test_file=test_file,
        tests_generated=total, tests_passing=passed, tests_failing=failed,
        coverage_before=None, coverage_after=None,
        duration_ms=int((time.monotonic() - t0) * 1000), model_used=model_used,
    )


# -- helpers -----------------------------------------------------------------

def _run_pytest(wd: Path, test_file: str) -> tuple[int, int, int, str]:
    """Run pytest -v on one file, return (total, passed, failed, raw_output)."""
    try:
        proc = subprocess.run(
            ["python", "-m", "pytest", test_file, "-v"],
            cwd=wd, capture_output=True, text=True, timeout=60,
        )
        out = proc.stdout + proc.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0, 0, 0, "pytest execution failed"
    p = int(m.group(1)) if (m := re.search(r"(\d+) passed", out)) else 0
    f = int(m.group(1)) if (m := re.search(r"(\d+) failed", out)) else 0
    return p + f, p, f, out


def _extract_code(content: str) -> str:
    """Strip markdown fences from LLM output."""
    m = re.search(r"```(?:python)?\s*\n(.*?)```", content, re.DOTALL)
    return m.group(1).strip() if m else content.strip()


def _empty(t0: float, target: str, model: str) -> TestGenReport:
    return TestGenReport(
        target_file=target, test_file="", tests_generated=0,
        tests_passing=0, tests_failing=0, coverage_before=None,
        coverage_after=None, duration_ms=int((time.monotonic() - t0) * 1000),
        model_used=model,
    )
