"""Tests for nvh.core.rate_limiter — token bucket, circuit breaker, rate manager, retry."""

from __future__ import annotations

import time

import pytest

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


class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker(provider="test")
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_request()

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(provider="test", failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert not cb.allow_request()

    def test_success_resets_half_open(self):
        cb = CircuitBreaker(provider="test", failure_threshold=1, initial_cooldown=0)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        # Immediately transition to half-open (cooldown=0)
        assert cb.allow_request()  # transitions to HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_reset(self):
        cb = CircuitBreaker(provider="test", failure_threshold=1)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED


class TestCircuitBreakerTransitions:
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


class TestTokenBucket:
    def test_consume(self):
        bucket = TokenBucket(capacity=10, refill_rate=1.0)
        assert bucket.consume(5)
        assert bucket.consume(5)
        assert not bucket.consume(1)

    def test_refill(self):
        bucket = TokenBucket(capacity=10, refill_rate=100.0)
        bucket.consume(10)
        time.sleep(0.1)
        assert bucket.consume(1)

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


class TestProviderRateManager:
    def test_health_score_healthy(self):
        mgr = ProviderRateManager()
        assert mgr.get_health_score("test") == 1.0

    def test_health_score_after_failures(self):
        mgr = ProviderRateManager()
        for _ in range(3):
            mgr.record_failure("test", ProviderUnavailableError("fail", provider="test"))
        score = mgr.get_health_score("test")
        assert score < 1.0

    def test_reset(self):
        mgr = ProviderRateManager()
        mgr.record_failure("test", ProviderUnavailableError("fail", provider="test"))
        mgr.reset("test")
        assert mgr.get_health_score("test") == 1.0


class TestRateLimiterPaths:
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


class TestRateLimiterRecovery:
    def test_token_bucket_consume_full(self):
        tb = TokenBucket(capacity=10, refill_rate=1.0)
        assert tb.consume(5) is True
        assert tb.consume(5) is True
        assert tb.consume(1) is False  # empty

    def test_circuit_breaker_trip_and_recover(self):
        cb = CircuitBreaker(provider="test", failure_threshold=2, initial_cooldown=0.1)
        assert cb.state == CircuitState.CLOSED
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        # After cooldown it should transition to HALF_OPEN
        time.sleep(0.15)
        assert cb.allow_request() is True
        assert cb.state == CircuitState.HALF_OPEN

    def test_health_score_healthy(self):
        rm = ProviderRateManager()
        score = rm.get_health_score("test_provider")
        assert score == 1.0  # no failures = fully healthy

    def test_health_score_after_failure(self):
        rm = ProviderRateManager()
        rm.record_failure("test_p", Exception("err"))
        score = rm.get_health_score("test_p")
        assert 0 < score < 1.0
