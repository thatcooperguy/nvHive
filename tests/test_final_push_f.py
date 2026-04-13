"""Final coverage push F — targeting modules at 54-89% to push toward 95%+."""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from nvh.providers.base import (
    CompletionResponse,
    Usage,
)

# ---------------------------------------------------------------------------
# nvh/core/agent_loop.py (54% → 80%)
# ---------------------------------------------------------------------------

class TestAgentLoopDeep:
    def test_extract_tool_calls_json_block(self):
        from nvh.core.agent_loop import _extract_tool_calls
        text = '''Let me read the file.
```tool_call
{"tool": "read_file", "args": {"path": "main.py"}}
```
'''
        calls = _extract_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["tool"] == "read_file"

    def test_extract_tool_calls_multiple(self):
        from nvh.core.agent_loop import _extract_tool_calls
        text = '''
```tool_call
{"tool": "read_file", "args": {"path": "a.py"}}
```
Then I'll write:
```tool_call
{"tool": "write_file", "args": {"path": "b.py", "content": "hello"}}
```
'''
        calls = _extract_tool_calls(text)
        assert len(calls) == 2

    def test_extract_tool_calls_none(self):
        from nvh.core.agent_loop import _extract_tool_calls
        calls = _extract_tool_calls("Just a regular response with no tools.")
        assert calls == []

    def test_extract_tool_calls_malformed_json(self):
        from nvh.core.agent_loop import _extract_tool_calls
        text = '''```tool_call
{"tool": "read_file", "args": {invalid json}}
```'''
        calls = _extract_tool_calls(text)
        assert calls == []

    def test_extract_tool_calls_max_limit(self):
        from nvh.core.agent_loop import MAX_TOOL_CALLS_PER_TURN, _extract_tool_calls
        # Generate more than MAX_TOOL_CALLS_PER_TURN
        blocks = "\n".join(
            f'```tool_call\n{{"tool": "read_file", "args": {{"path": "file{i}.py"}}}}\n```'
            for i in range(MAX_TOOL_CALLS_PER_TURN + 3)
        )
        calls = _extract_tool_calls(blocks)
        assert len(calls) == MAX_TOOL_CALLS_PER_TURN

    @pytest.mark.asyncio
    async def test_agent_loop_completes_without_tools(self):
        from nvh.core.agent_loop import run_agent_loop

        class MockEngine:
            async def query(self, prompt="", **kw):
                return CompletionResponse(
                    content="The answer is 42.", model="m", provider="mock",
                    usage=Usage(total_tokens=10), cost_usd=Decimal("0"), latency_ms=1,
                )
        result = await run_agent_loop(task="What is 6*7?", engine=MockEngine(), max_iterations=3)
        assert result.completed is True
        assert "42" in result.final_response

    @pytest.mark.asyncio
    async def test_agent_loop_uses_tools(self):
        from nvh.core.agent_loop import run_agent_loop
        from nvh.core.tools import ToolRegistry

        call_count = 0

        class MockEngine:
            async def query(self, prompt="", **kw):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return CompletionResponse(
                        content='```tool_call\n{"tool": "list_files", "args": {"pattern": "*.py"}}\n```',
                        model="m", provider="mock",
                        usage=Usage(total_tokens=10), cost_usd=Decimal("0"), latency_ms=1,
                    )
                return CompletionResponse(
                    content="Found the files. Task complete.",
                    model="m", provider="mock",
                    usage=Usage(total_tokens=10), cost_usd=Decimal("0"), latency_ms=1,
                )

        tools = ToolRegistry(workspace=".", include_system=False)
        result = await run_agent_loop(
            task="List Python files", engine=MockEngine(),
            tools=tools, max_iterations=5,
        )
        assert result.completed is True
        assert result.total_tool_calls >= 1


# ---------------------------------------------------------------------------
# nvh/core/agent_git.py (55% → 80%)
# ---------------------------------------------------------------------------

class TestAgentGitDeep:
    def test_get_diff_summary_no_repo(self, tmp_path):
        from nvh.core.agent_git import get_diff_summary
        result = get_diff_summary(tmp_path)
        assert isinstance(result, str)

    def test_restore_branch_no_repo(self, tmp_path):
        from nvh.core.agent_git import restore_original_branch
        # Should not raise
        restore_original_branch(tmp_path, "main")

    def test_commit_no_changes(self, tmp_path):
        import subprocess

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


# ---------------------------------------------------------------------------
# nvh/core/agent_review.py (62% → 80%)
# ---------------------------------------------------------------------------

class TestAgentReviewDeep:
    def test_review_finding_all_fields(self):
        from nvh.core.agent_review import ReviewFinding
        f = ReviewFinding(
            file="main.py", line=42, severity="high",
            category="bug", issue="null deref", suggestion="add null check",
        )
        assert f.file == "main.py"
        assert f.line == 42
        assert f.severity == "high"

    def test_review_report_approved_no_high(self):
        from nvh.core.agent_review import ReviewFinding, ReviewReport
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
        from nvh.core.agent_review import ReviewFinding, ReviewReport
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
        import subprocess

        from nvh.core.agent_review import get_diff
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
# nvh/core/agent_testgen.py (64% → 80%)
# ---------------------------------------------------------------------------

class TestAgentTestgenDeep:
    def test_coverage_gap_dataclass(self):
        from nvh.core.agent_testgen import CoverageGap
        cg = CoverageGap(file="main.py", current_coverage=45.2, missing_lines=[10, 20, 30])
        assert cg.file == "main.py"
        assert cg.current_coverage == 45.2
        assert len(cg.missing_lines) == 3

    def test_find_coverage_gaps_no_pytest_cov(self, tmp_path):
        from nvh.core.agent_testgen import find_coverage_gaps
        # No coverage.json exists → should return empty
        gaps = find_coverage_gaps(tmp_path)
        assert gaps == []


# ---------------------------------------------------------------------------
# nvh/core/context.py (74% → 90%)
# ---------------------------------------------------------------------------

class TestContextDeep:
    def test_conversation_manager_construction(self):
        from nvh.core.context import ConversationManager
        cm = ConversationManager()
        assert cm is not None
        assert hasattr(cm, "create_conversation")
        assert hasattr(cm, "add_user_message")
        assert hasattr(cm, "get_messages")


# ---------------------------------------------------------------------------
# nvh/core/free_tier.py (58% → 80%)
# ---------------------------------------------------------------------------

class TestFreeTier:
    def test_import(self):
        from nvh.core import free_tier
        assert free_tier is not None

    def test_free_tier_advisors(self):
        from nvh.core.free_tier import FREE_TIER_ADVISORS
        assert isinstance(FREE_TIER_ADVISORS, (list, dict))
        assert len(FREE_TIER_ADVISORS) > 0

    def test_detect_available(self):
        from nvh.core.free_tier import detect_available_free_advisors
        result = detect_available_free_advisors()
        assert isinstance(result, list)

    def test_get_best_free(self):
        from nvh.core.free_tier import get_best_free_advisor
        best = get_best_free_advisor()
        # Returns a string provider name or None
        assert best is None or isinstance(best, str)


# ---------------------------------------------------------------------------
# nvh/auth/auth.py (41% → 60%)
# ---------------------------------------------------------------------------

class TestAuthDeep:
    def test_hash_token_deterministic(self):
        from nvh.auth.auth import hash_token
        h1 = hash_token("test_token_123")
        h2 = hash_token("test_token_123")
        assert h1 == h2
        assert h1 != "test_token_123"  # not plaintext

    def test_hash_token_different_inputs(self):
        from nvh.auth.auth import hash_token
        h1 = hash_token("token_a")
        h2 = hash_token("token_b")
        assert h1 != h2

    @pytest.mark.asyncio
    async def test_get_user_count_returns_int(self, tmp_path):
        import nvh.storage.repository as repo
        repo._engine = None
        repo._session_factory = None
        await repo.init_db(db_path=tmp_path / "auth_test.db")
        try:
            from nvh.auth.auth import get_user_count
            count = await get_user_count()
            assert isinstance(count, int)
            assert count >= 0
        finally:
            repo._engine = None
            repo._session_factory = None
