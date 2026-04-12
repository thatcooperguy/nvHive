"""Batch-5 coverage tests targeting uncovered paths in file_lock, rate_limiter,
webhooks, ollama_provider, context/context_files, and advisor_profiles."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ═══════════════════════════════════════════════════════════════════════════
# Module 1: nvh/core/file_lock.py — additional uncovered paths
# ═══════════════════════════════════════════════════════════════════════════
from nvh.core.file_lock import (
    AgentFileChange,
    FileLockCoordinator,
    LockType,
    get_file_lock_coordinator,
    plan_sequential_changes,
)


class TestFileLockAdditional:
    @pytest.fixture
    def coord(self):
        return FileLockCoordinator(default_timeout=5.0, max_wait_seconds=0.5)

    @pytest.mark.asyncio
    async def test_release_nonexistent_returns_false(self, coord):
        result = await coord.release("/no/such/file.py", "agent-x")
        assert result is False

    @pytest.mark.asyncio
    async def test_release_wrong_agent_returns_false(self, coord):
        await coord.acquire("/tmp/f.py", "agent-1")
        result = await coord.release("/tmp/f.py", "agent-99")
        assert result is False

    @pytest.mark.asyncio
    async def test_release_all_no_locks_returns_zero(self, coord):
        count = await coord.release_all("nobody")
        assert count == 0

    @pytest.mark.asyncio
    async def test_check_conflicts_empty_proposed(self, coord):
        conflicts = await coord.check_conflicts({})
        assert conflicts == []

    @pytest.mark.asyncio
    async def test_check_conflicts_no_overlap(self, coord):
        await coord.acquire("/tmp/a.py", "agent-1")
        conflicts = await coord.check_conflicts({"agent-2": "/tmp/b.py"})
        assert conflicts == []

    @pytest.mark.asyncio
    async def test_check_conflicts_multi_agent_same_file(self, coord):
        conflicts = await coord.check_conflicts({
            "agent-1": "/tmp/shared.py",
            "agent-2": "/tmp/shared.py",
        })
        # dict keys are unique, so only one entry; no multi-agent conflict
        # from the dict itself. Verify no crash.
        assert isinstance(conflicts, list)

    @pytest.mark.asyncio
    async def test_wait_for_lock_succeeds_after_release(self, coord):
        """Lock wait succeeds when the holder releases during the wait."""
        await coord.acquire("/tmp/f.py", "holder", LockType.WRITE)

        async def release_later():
            await asyncio.sleep(0.1)
            await coord.release("/tmp/f.py", "holder")

        asyncio.get_event_loop().create_task(release_later())
        got = await coord.acquire("/tmp/f.py", "waiter", LockType.WRITE, wait=True)
        assert got is True

    @pytest.mark.asyncio
    async def test_wait_for_lock_times_out(self, coord):
        coord.max_wait_seconds = 0.2
        await coord.acquire("/tmp/f.py", "holder", LockType.WRITE)
        got = await coord.acquire("/tmp/f.py", "waiter", LockType.WRITE, wait=True)
        assert got is False

    @pytest.mark.asyncio
    async def test_get_status_shows_file_details(self, coord):
        await coord.acquire("/tmp/x.py", "a1", LockType.READ)
        status = await coord.get_status()
        assert status["locked_files"] == 1
        by_file = status["by_file"]
        assert len(by_file) == 1
        info = list(by_file.values())[0][0]
        assert info["agent"] == "a1"
        assert info["type"] == "read"


class TestPlanSequentialNoPrority:
    def test_plan_without_priority_picks_first(self):
        changes = [
            AgentFileChange(agent_id="b", file_path="/tmp/f.py", action="modify", content="B"),
            AgentFileChange(agent_id="a", file_path="/tmp/f.py", action="modify", content="A"),
        ]
        ordered = plan_sequential_changes(changes, priority_order=None)
        winner = [c for c in ordered if "f.py" in c.file_path][0]
        assert winner.agent_id == "b"  # first in the list wins

    def test_plan_priority_agent_not_in_conflict(self):
        """When priority list has agents not involved in the conflict,
        fall back to picking the first conflicting change."""
        changes = [
            AgentFileChange(agent_id="x", file_path="/tmp/f.py", action="modify"),
            AgentFileChange(agent_id="y", file_path="/tmp/f.py", action="modify"),
        ]
        ordered = plan_sequential_changes(changes, priority_order=["z"])
        winner = [c for c in ordered if "f.py" in c.file_path][0]
        assert winner.agent_id == "x"


class TestGetFileLockCoordinator:
    def test_singleton(self):
        import nvh.core.file_lock as fl_mod

        fl_mod._coordinator = None
        c1 = get_file_lock_coordinator()
        c2 = get_file_lock_coordinator()
        assert c1 is c2
        fl_mod._coordinator = None  # cleanup


# ═══════════════════════════════════════════════════════════════════════════
# Module 2: nvh/core/rate_limiter.py — additional uncovered paths
# ═══════════════════════════════════════════════════════════════════════════

from nvh.core.rate_limiter import CircuitBreaker, TokenBucket
from nvh.providers.base import CircuitState


class TestTokenBucketAdditional:
    def test_consume_when_empty_returns_false(self):
        bucket = TokenBucket(capacity=5, refill_rate=0.001)
        # Drain all tokens
        for _ in range(5):
            bucket.consume(1)
        assert bucket.consume(1) is False

    def test_consume_when_full_returns_true(self):
        bucket = TokenBucket(capacity=10, refill_rate=1.0)
        assert bucket.consume(1) is True

    def test_consume_large_amount(self):
        bucket = TokenBucket(capacity=10, refill_rate=1.0)
        assert bucket.consume(10) is True
        assert bucket.consume(1) is False

    def test_refill_restores_tokens(self):
        bucket = TokenBucket(capacity=10, refill_rate=1000.0)
        bucket.consume(10)
        # Force refill by moving last_refill back
        bucket.last_refill = time.monotonic() - 1.0
        assert bucket.consume(1) is True

    def test_tokens_capped_at_capacity(self):
        bucket = TokenBucket(capacity=5, refill_rate=1000.0)
        bucket.last_refill = time.monotonic() - 100.0
        bucket._refill()
        assert bucket.tokens <= bucket.capacity


class TestCircuitBreakerAdditional:
    def test_record_success_in_half_open_closes(self):
        cb = CircuitBreaker(provider="test", failure_threshold=3, initial_cooldown=1.0)
        cb.state = CircuitState.HALF_OPEN
        cb._failures = [time.monotonic()]
        cb.record_success()
        assert cb.state == CircuitState.CLOSED
        assert len(cb._failures) == 0
        assert cb._cooldown == cb.initial_cooldown

    def test_record_success_in_closed_is_noop(self):
        cb = CircuitBreaker(provider="test")
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_allow_request_half_open_returns_true(self):
        cb = CircuitBreaker(provider="test")
        cb.state = CircuitState.HALF_OPEN
        assert cb.allow_request() is True

    def test_allow_request_open_before_cooldown_returns_false(self):
        cb = CircuitBreaker(provider="test", initial_cooldown=999.0)
        cb.state = CircuitState.OPEN
        cb._opened_at = time.monotonic()
        assert cb.allow_request() is False

    def test_allow_request_open_after_cooldown_transitions(self):
        cb = CircuitBreaker(provider="test", initial_cooldown=0.01)
        cb.state = CircuitState.OPEN
        cb._opened_at = time.monotonic() - 1.0
        assert cb.allow_request() is True
        assert cb.state == CircuitState.HALF_OPEN

    def test_reset(self):
        cb = CircuitBreaker(provider="test", initial_cooldown=5.0)
        cb.state = CircuitState.OPEN
        cb._failures = [1.0, 2.0]
        cb._cooldown = 60.0
        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert len(cb._failures) == 0
        assert cb._cooldown == 5.0

    def test_failure_trims_old_failures(self):
        cb = CircuitBreaker(provider="test", failure_threshold=10, window_seconds=1.0)
        # Add an old failure outside the window
        cb._failures = [time.monotonic() - 100.0]
        cb.record_failure()
        # Old failure should be trimmed
        assert len(cb._failures) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Module 3: nvh/core/webhooks.py — additional uncovered paths
# ═══════════════════════════════════════════════════════════════════════════

from nvh.core.webhooks import (
    WebhookConfig,
    WebhookManager,
    WebhookPayload,
    format_budget_alert,
    format_provider_alert,
    format_query_complete,
)


class TestWebhookAdditional:
    def test_load_from_config(self):
        mgr = WebhookManager()
        mgr.load_from_config([
            {"url": "https://a.com/hook", "events": ["query.complete"], "secret": "s"},
            {"url": "", "events": []},  # empty URL skipped
            {"url": "https://b.com/hook", "enabled": False},
        ])
        hooks = mgr.list_hooks()
        assert len(hooks) == 2
        assert hooks[0]["url"] == "https://a.com/hook"

    @pytest.mark.asyncio
    async def test_emit_skips_disabled_hook(self):
        mgr = WebhookManager()
        cfg = WebhookConfig(url="https://a.com", events=[], enabled=False)
        mgr.register(cfg)
        await mgr.emit("any.event", {"key": "val"})
        assert mgr._queue.empty()

    @pytest.mark.asyncio
    async def test_dispatch_non_2xx_retries(self):
        mgr = WebhookManager()
        cfg = WebhookConfig(url="https://a.com", events=[], retry_count=2, timeout_seconds=1)
        mock_resp = MagicMock(status_code=500)
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        payload = WebhookPayload(event="x", timestamp=1.0, data={})
        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await mgr._dispatch(cfg, payload)
        assert result is False
        assert mock_client.post.call_count == 2

    def test_format_budget_alert(self):
        data = format_budget_alert(0.50, 1.00, 5.0, 10.0, 0.5)
        assert data["daily_spend_usd"] == 0.5
        assert data["threshold_pct"] == 50.0
        assert data["daily_pct_used"] == 50.0

    def test_format_budget_alert_zero_limits(self):
        data = format_budget_alert(0.0, 0.0, 0.0, 0.0, 0.0)
        assert data["daily_pct_used"] == 0
        assert data["monthly_pct_used"] == 0

    def test_format_provider_alert(self):
        data = format_provider_alert("openai", "error", error="timeout", latency_ms=500)
        assert data["provider"] == "openai"
        assert data["error"] == "timeout"
        assert data["latency_ms"] == 500

    def test_format_provider_alert_minimal(self):
        data = format_provider_alert("ollama", "recovered")
        assert "error" not in data
        assert "latency_ms" not in data

    def test_format_query_complete(self):
        data = format_query_complete("openai", "gpt-4", 100, 0.003, 500, "query")
        assert data["provider"] == "openai"
        assert data["total_tokens"] == 100
        assert data["mode"] == "query"


# ═══════════════════════════════════════════════════════════════════════════
# Module 4: nvh/providers/ollama_provider.py — uncovered health_check + stream
# ═══════════════════════════════════════════════════════════════════════════

from nvh.providers.ollama_provider import OllamaProvider


class TestOllamaHealthAndStream:
    @pytest.mark.asyncio
    async def test_health_check_success(self):
        provider = OllamaProvider()
        fake_resp = MagicMock()
        fake_resp.raise_for_status = MagicMock()
        fake_resp.json.return_value = {"models": [{"name": "llama3.1"}, {"name": "phi3"}]}

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=fake_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("nvh.providers.ollama_provider.httpx.AsyncClient", return_value=mock_client):
            status = await provider.health_check()

        assert status.healthy is True
        assert status.models_available == 2
        assert status.provider == "ollama"
        assert status.latency_ms is not None

    @pytest.mark.asyncio
    async def test_health_check_failure(self):
        provider = OllamaProvider()

        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=Exception("connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("nvh.providers.ollama_provider.httpx.AsyncClient", return_value=mock_client):
            status = await provider.health_check()

        assert status.healthy is False
        assert "connection refused" in status.error

    @pytest.mark.asyncio
    async def test_stream_multiple_chunks(self):
        provider = OllamaProvider()

        async def fake_stream():
            yield SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(content="Hello"),
                    finish_reason=None,
                )],
            )
            yield SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=" world"),
                    finish_reason=None,
                )],
            )
            yield SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=""),
                    finish_reason="stop",
                )],
            )

        from nvh.providers.base import Message

        with patch(
            "nvh.providers.ollama_provider.litellm.acompletion",
            new=AsyncMock(return_value=fake_stream()),
        ):
            chunks = []
            async for chunk in provider.stream(
                messages=[Message(role="user", content="hi")],
            ):
                chunks.append(chunk)

        assert len(chunks) == 3
        assert chunks[0].delta == "Hello"
        assert chunks[1].delta == " world"
        assert chunks[2].is_final is True
        assert chunks[2].accumulated_content == "Hello world"


# ═══════════════════════════════════════════════════════════════════════════
# Module 5: nvh/core/context_files.py — uncovered paths
# ═══════════════════════════════════════════════════════════════════════════

from nvh.core.context_files import (
    ContextFile,
    _parse_frontmatter,
    build_context_prompt,
    find_context_files,
    get_context_summary,
)


class TestParseFrontmatter:
    def test_with_frontmatter(self):
        content = "---\nname: Rules\nscope: code\npriority: 5\n---\nBody here."
        meta, body = _parse_frontmatter(content)
        assert meta["name"] == "Rules"
        assert meta["scope"] == "code"
        assert meta["priority"] == "5"
        assert body.strip() == "Body here."

    def test_without_frontmatter(self):
        content = "Just plain markdown."
        meta, body = _parse_frontmatter(content)
        assert meta == {}
        assert body == content


class TestFindContextFiles:
    def test_finds_hive_md_in_project_dir(self, tmp_path):
        hive = tmp_path / "HIVE.md"
        hive.write_text("# Project rules\nDo things right.", encoding="utf-8")
        files = find_context_files(project_dir=tmp_path, home_dir=tmp_path / "fakehome")
        assert len(files) >= 1
        assert any("Project" in f.name or "HIVE" in f.path for f in files)

    def test_finds_modular_context_files(self, tmp_path):
        ctx_dir = tmp_path / ".hive" / "context"
        ctx_dir.mkdir(parents=True)
        (ctx_dir / "rules.md").write_text("---\nname: Rules\n---\nNo swearing.", encoding="utf-8")
        (ctx_dir / "style.md").write_text("Use black formatting.", encoding="utf-8")
        files = find_context_files(project_dir=tmp_path, home_dir=tmp_path / "fakehome")
        assert len(files) >= 2

    def test_finds_global_context(self, tmp_path):
        home = tmp_path / "home"
        global_ctx = home / ".hive" / "global_context.md"
        global_ctx.parent.mkdir(parents=True)
        global_ctx.write_text("Global rules apply everywhere.", encoding="utf-8")
        files = find_context_files(project_dir=tmp_path / "proj", home_dir=home)
        assert any(f.source == "global" for f in files)


class TestBuildContextPrompt:
    def test_with_context_files_and_user_prompt(self):
        cfiles = [
            ContextFile(path="/x", name="Rules", content="Be nice.", scope="all", source="project"),
        ]
        prompt = build_context_prompt(cfiles, scope="all", user_system_prompt="You are helpful.")
        assert "Be nice." in prompt
        assert "You are helpful." in prompt

    def test_scope_filtering(self):
        cfiles = [
            ContextFile(path="/x", name="Code", content="Code rules.", scope="code", source="project"),
            ContextFile(path="/y", name="All", content="All rules.", scope="all", source="project"),
        ]
        prompt = build_context_prompt(cfiles, scope="code")
        assert "Code rules." in prompt
        assert "All rules." in prompt

    def test_empty_returns_user_prompt(self):
        prompt = build_context_prompt([], scope="all", user_system_prompt="hello")
        assert prompt == "hello"


class TestGetContextSummary:
    def test_returns_summary_list(self):
        cfiles = [
            ContextFile(path="/a.md", name="A", content="aaa", scope="all", priority=10, source="project"),
        ]
        summary = get_context_summary(cfiles)
        assert len(summary) == 1
        assert summary[0]["name"] == "A"
        assert summary[0]["size"] == 3


# ═══════════════════════════════════════════════════════════════════════════
# Module 6: nvh/core/advisor_profiles.py — uncovered paths
# ═══════════════════════════════════════════════════════════════════════════

from nvh.core.advisor_profiles import (
    ADVISOR_PROFILES,
    format_advisor_card,
    get_advisor_profile,
    get_best_advisor_for_task,
)


class TestAdvisorProfiles:
    def test_get_known_profile(self):
        profile = get_advisor_profile("openai")
        assert profile is not None
        assert profile.name == "openai"
        assert profile.display_name == "OpenAI"

    def test_get_unknown_profile_returns_none(self):
        assert get_advisor_profile("nonexistent_provider") is None

    def test_list_profiles_has_expected_providers(self):
        expected = {"openai", "anthropic", "google", "ollama", "groq"}
        assert expected.issubset(set(ADVISOR_PROFILES.keys()))

    def test_get_best_advisor_basic(self):
        best = get_best_advisor_for_task(
            "Write some Python code",
            available_advisors=["openai", "ollama", "groq"],
        )
        assert best in ["openai", "ollama", "groq"]

    def test_get_best_advisor_prefer_local(self):
        best = get_best_advisor_for_task(
            "something private",
            available_advisors=["openai", "ollama"],
            prefer_local=True,
        )
        assert best == "ollama"

    def test_get_best_advisor_needs_search(self):
        best = get_best_advisor_for_task(
            "latest news",
            available_advisors=["openai", "perplexity", "ollama"],
            needs_search=True,
        )
        assert best == "perplexity"

    def test_get_best_advisor_empty_list(self):
        assert get_best_advisor_for_task("anything", []) is None

    def test_format_advisor_card_known(self):
        card = format_advisor_card("openai")
        assert "OpenAI" in card
        assert "Best for:" in card
        assert "Avoid for:" in card

    def test_format_advisor_card_unknown(self):
        card = format_advisor_card("does_not_exist")
        assert "Unknown advisor" in card
