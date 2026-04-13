"""Tests for nvh.core.agent_pr."""
from __future__ import annotations

from unittest.mock import patch

from nvh.core.agent_pr import PRResult, check_gh_auth, slugify


class TestSlugify:
    def test_basic(self) -> None:
        assert slugify("Fix the auth bug in main.py") == "fix-the-auth-bug-in-main-py"

    def test_strips_leading_trailing(self) -> None:
        assert slugify("---hello---") == "hello"

    def test_max_len(self) -> None:
        result = slugify("a very long task description here", max_len=10)
        assert len(result) <= 10 and not result.endswith("-")

    def test_empty_string(self) -> None:
        assert slugify("") == ""

    def test_special_characters(self) -> None:
        assert slugify("hello@world! #2024") == "hello-world-2024"

    def test_already_clean(self) -> None:
        assert slugify("clean-slug") == "clean-slug"


class TestPRResult:
    def test_defaults(self) -> None:
        r = PRResult()
        assert r.branch_name == "" and r.commit_sha is None
        assert r.pr_url is None and r.pr_number is None and r.error is None

    def test_with_values(self) -> None:
        r = PRResult(
            branch_name="agent/fix-bug-20260412", commit_sha="abc123",
            pr_url="https://github.com/o/r/pull/7", pr_number=7,
        )
        assert r.pr_number == 7 and r.error is None

    def test_error_state(self) -> None:
        r = PRResult(error="something went wrong")
        assert r.error == "something went wrong" and r.pr_url is None


class TestCheckGhAuth:
    def test_gh_not_installed(self) -> None:
        with patch("nvh.core.agent_pr.shutil.which", return_value=None):
            ok, msg = check_gh_auth()
        assert ok is False and "not installed" in msg

    def test_gh_not_authenticated(self) -> None:
        with (
            patch("nvh.core.agent_pr.shutil.which", return_value="/usr/bin/gh"),
            patch("nvh.core.agent_pr.subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "not logged in"
            mock_run.return_value.stdout = ""
            ok, msg = check_gh_auth()
        assert ok is False and "not authenticated" in msg

    def test_gh_authenticated(self) -> None:
        with (
            patch("nvh.core.agent_pr.shutil.which", return_value="/usr/bin/gh"),
            patch("nvh.core.agent_pr.subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "Logged in to github.com"
            mock_run.return_value.stderr = ""
            ok, msg = check_gh_auth()
        assert ok is True and "Logged in" in msg
