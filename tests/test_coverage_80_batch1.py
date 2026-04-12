"""Tests to push settings, file_lock, and rate_limiter toward 80% coverage.

Covers UNCOVERED paths only — see existing tests in test_coverage_boost.py,
test_file_lock.py, and test_providers.py for baseline coverage.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from nvh.config.settings import (
    CacheConfig,
    CouncilConfig,
    CouncilWeights,
    ProfileConfig,
    RoutingConfig,
    _interpolate_env,
    _load_yaml,
    generate_default_config,
    load_config,
    save_config,
)
from nvh.core.file_lock import (
    AgentFileChange,
    FileLock,
    FileLockCoordinator,
    LockType,
    plan_sequential_changes,
)
from nvh.core.rate_limiter import (
    CircuitBreaker,
    ProviderRateManager,
    TokenBucket,
    retry_with_backoff,
)
from nvh.providers.base import (
    CircuitState,
    ProviderUnavailableError,
    RateLimitError,
)

# ═══════════════════════════════════════════════════════════════════════════
# Module 1: nvh/config/settings.py — uncovered paths
# ═══════════════════════════════════════════════════════════════════════════


class TestSettingsUncovered:
    """Paths not covered by test_coverage_boost.py."""

    def test_interpolate_env_unset_no_default(self) -> None:
        """${VAR} with no default and VAR unset => empty string + warning."""
        os.environ.pop("__TOTALLY_MISSING__", None)
        result = _interpolate_env("prefix-${__TOTALLY_MISSING__}-suffix")
        assert result == "prefix--suffix"

    def test_interpolate_env_in_list(self) -> None:
        """Env-var interpolation recurses into lists."""
        with patch.dict(os.environ, {"_LIST_VAR": "item"}):
            result = _interpolate_env(["${_LIST_VAR}", "plain"])
        assert result == ["item", "plain"]

    def test_interpolate_env_non_string_passthrough(self) -> None:
        """Non-string values (int, None, etc.) pass through unchanged."""
        assert _interpolate_env(42) == 42
        assert _interpolate_env(None) is None
        assert _interpolate_env(True) is True

    def test_load_yaml_invalid_syntax(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(":\n  - [invalid\n")
        with pytest.raises(ValueError, match="invalid YAML"):
            _load_yaml(bad)

    def test_load_yaml_non_dict(self, tmp_path: Path) -> None:
        bad = tmp_path / "list.yaml"
        bad.write_text("- one\n- two\n")
        with pytest.raises(ValueError, match="must be a YAML mapping"):
            _load_yaml(bad)

    def test_profile_merge_via_arg(self, tmp_path: Path) -> None:
        """load_config with profile= merges profile section."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({
            "defaults": {"timeout": 10},
            "profiles": {
                "fast": {"defaults": {"timeout": 5, "stream": False}},
            },
        }))
        with patch("nvh.config.settings._find_project_config", return_value=None):
            cfg = load_config(config_path=cfg_file, profile="fast")
        assert cfg.defaults.timeout == 5
        assert cfg.defaults.stream is False

    def test_profile_merge_via_env(self, tmp_path: Path) -> None:
        """HIVE_PROFILE env var triggers profile merge."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({
            "defaults": {"timeout": 10},
            "profiles": {
                "slow": {"defaults": {"timeout": 120}},
            },
        }))
        with (
            patch("nvh.config.settings._find_project_config", return_value=None),
            patch.dict(os.environ, {"HIVE_PROFILE": "slow"}),
        ):
            cfg = load_config(config_path=cfg_file)
        assert cfg.defaults.timeout == 120

    def test_council_weights_normalization(self) -> None:
        cw = CouncilWeights(weights={"a": 2.0, "b": 3.0})
        assert abs(sum(cw.weights.values()) - 1.0) < 0.01

    def test_routing_config_defaults(self) -> None:
        rc = RoutingConfig()
        assert rc.weights["capability"] == 0.4
        assert rc.rules == []

    def test_cache_config_defaults(self) -> None:
        cc = CacheConfig()
        assert cc.enabled is True
        assert cc.ttl_seconds == 86400
        assert cc.cache_nonzero_temp is False

    def test_save_and_reload(self, tmp_path: Path) -> None:
        cfg = CouncilConfig(defaults={"timeout": 77})
        path = save_config(cfg, tmp_path / "out.yaml")
        assert path.is_file()
        with patch("nvh.config.settings._find_project_config", return_value=None):
            reloaded = load_config(config_path=path)
        assert reloaded.defaults.timeout == 77

    def test_generate_default_config_is_valid_yaml(self) -> None:
        text = generate_default_config()
        parsed = yaml.safe_load(text)
        assert isinstance(parsed, dict)
        assert "defaults" in parsed

    def test_profile_config_advisors_alias(self) -> None:
        pc = ProfileConfig(**{"advisors": {"mock": {"default_model": "m"}}})
        assert "mock" in pc.providers


# ═══════════════════════════════════════════════════════════════════════════
# Module 2: nvh/core/file_lock.py — uncovered paths
# ═══════════════════════════════════════════════════════════════════════════


class TestFileLockUncovered:
    """Paths not covered by test_file_lock.py."""

    @pytest.fixture
    def coord(self):
        return FileLockCoordinator(
            default_timeout=5.0, max_wait_seconds=1.0,
        )

    def test_file_lock_is_expired(self) -> None:
        lock = FileLock(
            path="/f", lock_type=LockType.WRITE,
            agent_id="a", acquired_at=time.monotonic() - 100,
            timeout_seconds=1.0,
        )
        assert lock.is_expired

    def test_file_lock_not_expired(self) -> None:
        lock = FileLock(
            path="/f", lock_type=LockType.WRITE,
            agent_id="a", acquired_at=time.monotonic(),
            timeout_seconds=60.0,
        )
        assert not lock.is_expired

    @pytest.mark.asyncio
    async def test_release_nonexistent(self, coord) -> None:
        assert not await coord.release("/no/such/file", "agent-1")

    @pytest.mark.asyncio
    async def test_read_blocked_by_write_no_wait(self, coord) -> None:
        await coord.acquire("/f", "a1", LockType.WRITE)
        assert not await coord.acquire("/f", "a2", LockType.READ, wait=False)

    @pytest.mark.asyncio
    async def test_wait_timeout_returns_false(self, coord) -> None:
        """When wait=True but lock never freed, acquire returns False."""
        coord.max_wait_seconds = 0.2
        await coord.acquire("/f", "a1", LockType.WRITE)
        result = await coord.acquire("/f", "a2", LockType.WRITE, wait=True)
        assert result is False

    @pytest.mark.asyncio
    async def test_wait_succeeds_after_release(self, coord) -> None:
        """Waiter acquires lock once holder releases."""
        import asyncio

        coord.max_wait_seconds = 3.0
        await coord.acquire("/f", "holder", LockType.WRITE)

        async def release_soon():
            await asyncio.sleep(0.15)
            await coord.release("/f", "holder")

        task = asyncio.create_task(release_soon())
        got = await coord.acquire("/f", "waiter", LockType.WRITE, wait=True)
        assert got is True
        await task

    @pytest.mark.asyncio
    async def test_multi_agent_conflict_detection(self, coord) -> None:
        conflicts = await coord.check_conflicts({
            "agent-a": "/shared.py",
            "agent-b": "/shared.py",
        })
        # Two agents proposing same file => at least 1 conflict
        assert len(conflicts) >= 1

    @pytest.mark.asyncio
    async def test_plan_sequential_no_priority(self) -> None:
        """plan_sequential_changes without priority picks first."""
        changes = [
            AgentFileChange(agent_id="a", file_path="/x.py", action="modify", content="va"),
            AgentFileChange(agent_id="b", file_path="/x.py", action="modify", content="vb"),
        ]
        ordered = plan_sequential_changes(changes)
        x_change = [c for c in ordered if "x.py" in c.file_path][0]
        assert x_change.agent_id == "a"


# ═══════════════════════════════════════════════════════════════════════════
# Module 3: nvh/core/rate_limiter.py — uncovered paths
# ═══════════════════════════════════════════════════════════════════════════


class TestRateLimiterUncovered:
    """Paths not covered by test_providers.py."""

    def test_check_available_circuit_open(self) -> None:
        mgr = ProviderRateManager()
        br = mgr.get_breaker("p")
        br.state = CircuitState.OPEN
        br._opened_at = time.monotonic()
        br._cooldown = 9999
        with pytest.raises(ProviderUnavailableError):
            mgr.check_available("p")

    def test_check_available_rate_limited(self) -> None:
        mgr = ProviderRateManager()
        mgr.set_retry_after("p", 60)
        with pytest.raises(RateLimitError):
            mgr.check_available("p")

    def test_set_retry_after(self) -> None:
        mgr = ProviderRateManager()
        mgr.set_retry_after("p", 10)
        assert mgr._retry_after["p"] > time.monotonic()

    def test_reset_clears_retry_after(self) -> None:
        mgr = ProviderRateManager()
        mgr.set_retry_after("p", 10)
        mgr.reset("p")
        assert "p" not in mgr._retry_after

    def test_record_failure_rate_limit_sets_retry(self) -> None:
        mgr = ProviderRateManager()
        err = RateLimitError("slow down", provider="p", retry_after=5.0)
        mgr.record_failure("p", err)
        # Should set retry_after, NOT trip breaker
        assert mgr._retry_after["p"] > time.monotonic()
        assert mgr.get_breaker("p").state == CircuitState.CLOSED

    def test_token_bucket_time_until_available(self) -> None:
        bucket = TokenBucket(capacity=10, refill_rate=10.0)
        bucket.consume(10)
        wait = bucket.time_until_available(5)
        assert wait > 0

    def test_circuit_breaker_half_open_failure_reopens(self) -> None:
        cb = CircuitBreaker(
            provider="t", failure_threshold=1, initial_cooldown=0.5,
        )
        cb.record_failure()  # -> OPEN
        assert cb.state == CircuitState.OPEN
        # Force transition to HALF_OPEN by pretending cooldown elapsed
        cb._opened_at = time.monotonic() - 1.0
        assert cb.allow_request()  # transitions to HALF_OPEN
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_failure()  # probe failed -> OPEN, cooldown doubled
        assert cb.state == CircuitState.OPEN
        assert cb._cooldown == 1.0  # doubled from 0.5

    def test_health_score_open(self) -> None:
        mgr = ProviderRateManager()
        br = mgr.get_breaker("p")
        br.state = CircuitState.OPEN
        assert mgr.get_health_score("p") == 0.0

    def test_health_score_half_open(self) -> None:
        mgr = ProviderRateManager()
        br = mgr.get_breaker("p")
        br.state = CircuitState.HALF_OPEN
        assert mgr.get_health_score("p") == 0.3

    @pytest.mark.asyncio
    async def test_retry_with_backoff_succeeds_first(self) -> None:
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            return "ok"

        result = await retry_with_backoff(factory, max_attempts=3)
        assert result == "ok"
        assert calls == 1

    @pytest.mark.asyncio
    async def test_retry_with_backoff_retries_then_succeeds(self) -> None:
        attempt = 0

        async def factory():
            nonlocal attempt
            attempt += 1
            if attempt < 3:
                raise ProviderUnavailableError("down", provider="x")
            return "recovered"

        result = await retry_with_backoff(
            factory, max_attempts=3, initial_delay=0.01, max_delay=0.05,
        )
        assert result == "recovered"
        assert attempt == 3

    @pytest.mark.asyncio
    async def test_retry_with_backoff_exhausted(self) -> None:
        async def factory():
            raise ProviderUnavailableError("down", provider="x")

        with pytest.raises(ProviderUnavailableError):
            await retry_with_backoff(
                factory, max_attempts=2, initial_delay=0.01,
            )
