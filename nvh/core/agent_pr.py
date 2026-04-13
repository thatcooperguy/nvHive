"""Git-to-PR workflow for the nvhive agent.

Creates a branch, commits staged files, pushes to origin, and opens a GitHub
pull request — all via subprocess (no SSH required, uses ``gh`` CLI over HTTPS).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_RUN_KW: dict[str, object] = dict(
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    stdin=subprocess.DEVNULL,
    timeout=60,
)


@dataclass
class PRResult:
    """Outcome of a create-PR attempt."""

    branch_name: str = ""
    commit_sha: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    error: str | None = None


def slugify(text: str, max_len: int = 40) -> str:
    """Convert arbitrary text into a URL/branch-safe slug.

    >>> slugify("Fix the auth bug in main.py")
    'fix-the-auth-bug-in-main-py'
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-")


def check_gh_auth() -> tuple[bool, str]:
    """Return ``(True, status_msg)`` when ``gh`` is installed and authenticated."""
    if shutil.which("gh") is None:
        return False, "gh CLI is not installed. Install from https://cli.github.com/"
    try:
        proc = subprocess.run(["gh", "auth", "status"], **_RUN_KW)  # type: ignore[arg-type]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Failed to run gh: {exc}"
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "unknown error").strip()
        return False, f"gh is not authenticated: {msg}"
    return True, (proc.stdout or proc.stderr or "authenticated").strip()


def _run(cmd: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, **_RUN_KW)  # type: ignore[arg-type]


async def create_pr(
    working_dir: str | Path,
    task: str,
    files_modified: list[str],
    files_created: list[str],
    summary: str,
) -> PRResult:
    """Drive the full branch -> commit -> push -> PR flow.

    Never raises; errors are captured in :pyattr:`PRResult.error`.
    """
    cwd = str(working_dir)
    date_tag = datetime.now(UTC).strftime("%Y%m%d")
    branch = f"agent/{slugify(task)}-{date_tag}"
    result = PRResult(branch_name=branch)

    try:
        # -- create & switch to branch ----------------------------------------
        proc = _run(["git", "checkout", "-b", branch], cwd)
        if proc.returncode != 0:
            result.error = f"git checkout -b failed: {proc.stderr.strip()}"
            return result

        # -- stage files -------------------------------------------------------
        all_files = [*files_modified, *files_created]
        if not all_files:
            result.error = "No files to commit."
            return result
        proc = _run(["git", "add", "--", *all_files], cwd)
        if proc.returncode != 0:
            result.error = f"git add failed: {proc.stderr.strip()}"
            return result

        # -- commit ------------------------------------------------------------
        msg = f"agent: {task}"
        proc = _run(["git", "commit", "-m", msg], cwd)
        if proc.returncode != 0:
            result.error = f"git commit failed: {proc.stderr.strip()}"
            return result
        sha_proc = _run(["git", "rev-parse", "HEAD"], cwd)
        result.commit_sha = sha_proc.stdout.strip() or None

        # -- push --------------------------------------------------------------
        proc = _run(["git", "push", "-u", "origin", branch], cwd)
        if proc.returncode != 0:
            result.error = f"git push failed: {proc.stderr.strip()}"
            return result

        # -- open PR -----------------------------------------------------------
        proc = _run(
            [
                "gh", "pr", "create",
                "--title", task,
                "--body", summary,
                "--head", branch,
            ],
            cwd,
        )
        if proc.returncode != 0:
            result.error = f"gh pr create failed: {proc.stderr.strip()}"
            return result

        url = proc.stdout.strip()
        result.pr_url = url
        # Extract PR number from URL (e.g. .../pull/42)
        m = re.search(r"/pull/(\d+)", url)
        if m:
            result.pr_number = int(m.group(1))

    except subprocess.TimeoutExpired:
        result.error = "A subprocess timed out."
    except OSError as exc:
        result.error = f"OS error: {exc}"

    return result
