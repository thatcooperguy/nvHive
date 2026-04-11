"""Git integration for the nvhive agentic coding feature.

Handles branch creation, committing agent changes, and generating diff summaries.
All operations shell out to ``git`` via subprocess.
"""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

_RUN_KW = dict(capture_output=True, text=True, encoding="utf-8", errors="replace")


def _git(working_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(working_dir),
        **_RUN_KW,
    )


def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-")


def is_git_repo(working_dir: Path) -> bool:
    """Return True if *working_dir* is inside a git repository."""
    try:
        r = _git(working_dir, "rev-parse", "--is-inside-work-tree")
        return r.returncode == 0 and r.stdout.strip() == "true"
    except Exception:
        return False


def get_current_branch(working_dir: Path) -> str:
    """Return the current branch name, or empty string on error."""
    try:
        r = _git(working_dir, "rev-parse", "--abbrev-ref", "HEAD")
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def create_agent_branch(working_dir: Path, task: str) -> str:
    """Create and check out ``agent/<slug>-<date>``; return the branch name.

    If the branch already exists, a numeric suffix is appended (``-2``, ``-3``, …).
    Returns an empty string on failure.
    """
    try:
        stamp = datetime.now(UTC).strftime("%Y%m%d")
        base = f"agent/{_slugify(task)}-{stamp}"
        candidate = base
        counter = 1
        while True:
            check = _git(working_dir, "rev-parse", "--verify", candidate)
            if check.returncode != 0:
                break
            counter += 1
            candidate = f"{base}-{counter}"
        r = _git(working_dir, "checkout", "-b", candidate)
        return candidate if r.returncode == 0 else ""
    except Exception:
        return ""


def commit_agent_changes(
    working_dir: Path,
    task: str,
    files_modified: list[str],
    files_created: list[str],
) -> str | None:
    """Stage listed files and commit. Return the commit SHA, or None."""
    try:
        all_files = [*files_modified, *files_created]
        if not all_files:
            return None
        for f in all_files:
            _git(working_dir, "add", "--", f)
        diff = _git(working_dir, "diff", "--cached", "--quiet")
        if diff.returncode == 0:
            return None
        parts = []
        if files_created:
            parts.append(f"create {len(files_created)} file(s)")
        if files_modified:
            parts.append(f"modify {len(files_modified)} file(s)")
        summary = ", ".join(parts)
        msg = f"agent: {task}\n\n{summary}"
        r = _git(working_dir, "commit", "-m", msg)
        if r.returncode != 0:
            return None
        sha = _git(working_dir, "rev-parse", "HEAD")
        return sha.stdout.strip() if sha.returncode == 0 else None
    except Exception:
        return None


def get_diff_summary(working_dir: Path) -> str:
    """Return ``git diff --stat`` output for staged and unstaged changes."""
    try:
        r = _git(working_dir, "diff", "--stat", "HEAD")
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def restore_original_branch(working_dir: Path, original_branch: str) -> None:
    """Check out *original_branch* (best-effort, never raises)."""
    try:
        _git(working_dir, "checkout", original_branch)
    except Exception:
        pass
