"""Rate limiting (token bucket) and circuit breaker per provider."""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field

from nvh.providers.base import (
    CircuitState,
    ProviderUnavailableError,
    RateLimitError,
)

# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

@dataclass
class CircuitBreaker:
    """Per-provider circuit breaker with exponential recovery cooldown."""

    provider: str
    failure_threshold: int = 5
    window_seconds: float = 60.0
    initial_cooldown: float = 30.0
    max_cooldown: float = 300.0

    state: CircuitState = CircuitState.CLOSED
    _failures: list[float] = field(default_factory=list)
    _opened_at: float = 0.0
    _cooldown: float = 0.0  # set in __post_init__

    def __post_init__(self) -> None:
        self._cooldown = self.initial_cooldown

    def record_success(self) -> None:
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.CLOSED
            self._cooldown = self.initial_cooldown
            self._failures.clear()

    def record_failure(self) -> None:
        now = time.monotonic()
        self._failures.append(now)
        # Trim old failures outside the window
        cutoff = now - self.window_seconds
        self._failures = [t for t in self._failures if t > cutoff]

        if self.state == CircuitState.HALF_OPEN:
            # Probe failed — reopen with doubled cooldown
            self.state = CircuitState.OPEN
            self._opened_at = now
            self._cooldown = min(self._cooldown * 2, self.max_cooldown)
        elif len(self._failures) >= self.failure_threshold:
            self.state = CircuitState.OPEN
            self._opened_at = now

    def allow_request(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self._cooldown:
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        # HALF_OPEN — allow one probe
        return True

    def reset(self) -> None:
        self.state = CircuitState.CLOSED
        self._failures.clear()
        self._cooldown = self.initial_cooldown


# ---------------------------------------------------------------------------
# Rate Limiter (Token Bucket)
# ---------------------------------------------------------------------------

@dataclass
class TokenBucket:
    """Simple token bucket rate limiter."""

    capacity: int  # max tokens
    refill_rate: float  # tokens per second
    tokens: float = 0.0
    last_refill: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if self.tokens == 0.0:
            self.tokens = float(self.capacity)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    def consume(self, amount: int = 1) -> bool:
        self._refill()
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False

    def time_until_available(self, amount: int = 1) -> float:
        self._refill()
        if self.tokens >= amount:
            return 0.0
        deficit = amount - self.tokens
        return deficit / self.refill_rate


# ---------------------------------------------------------------------------
# Provider Rate Manager
# ---------------------------------------------------------------------------

class ProviderRateManager:
    """Manages circuit breakers and rate limits across all providers."""

    def __init__(self) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}
        self._buckets: dict[str, TokenBucket] = {}
        self._retry_after: dict[str, float] = {}  # provider -> earliest retry time

    def get_breaker(self, provider: str) -> CircuitBreaker:
        if provider not in self._breakers:
            self._breakers[provider] = CircuitBreaker(provider=provider)
        return self._breakers[provider]

    def get_bucket(self, provider: str) -> TokenBucket:
        if provider not in self._buckets:
            # Default: 60 requests per minute
            self._buckets[provider] = TokenBucket(capacity=60, refill_rate=1.0)
        return self._buckets[provider]

    def check_available(self, provider: str) -> None:
        """Raise if the provider is not available (circuit open or rate limited)."""
        breaker = self.get_breaker(provider)
        if not breaker.allow_request():
            raise ProviderUnavailableError(
                f"Advisor '{provider}' is temporarily unavailable (too many recent failures).\n"
                f"NVHive will retry automatically after the cooldown period.\n"
                f"To reconfigure: nvh {provider}",
                provider=provider,
            )

        # Check retry-after from 429s
        retry_time = self._retry_after.get(provider, 0)
        if retry_time > time.monotonic():
            wait = retry_time - time.monotonic()
            raise RateLimitError(
                f"Rate limited by '{provider}' — trying next advisor... (or wait {wait:.0f}s)\n"
                f"To switch default: nvh config set defaults.provider <other_provider>",
                provider=provider,
                retry_after=wait,
            )

    def record_success(self, provider: str) -> None:
        self.get_breaker(provider).record_success()

    def record_failure(self, provider: str, error: Exception) -> None:
        breaker = self.get_breaker(provider)
        if isinstance(error, RateLimitError) and error.retry_after:
            # Don't trip circuit breaker on rate limits
            self._retry_after[provider] = time.monotonic() + error.retry_after
        else:
            breaker.record_failure()

    def set_retry_after(self, provider: str, seconds: float) -> None:
        self._retry_after[provider] = time.monotonic() + seconds

    def reset(self, provider: str) -> None:
        if provider in self._breakers:
            self._breakers[provider].reset()
        self._retry_after.pop(provider, None)

    def get_health_score(self, provider: str) -> float:
        """Return a health score from 0.0 to 1.0 for routing decisions."""
        breaker = self.get_breaker(provider)
        if breaker.state == CircuitState.OPEN:
            return 0.0
        if breaker.state == CircuitState.HALF_OPEN:
            return 0.3
        # Score based on recent failure count
        recent_failures = len(breaker._failures)
        if recent_failures == 0:
            return 1.0
        return max(0.2, 1.0 - (recent_failures / breaker.failure_threshold))


# ---------------------------------------------------------------------------
# Retry Helper
# ---------------------------------------------------------------------------

async def retry_with_backoff(
    coro_factory,
    max_attempts: int = 3,
    initial_delay: float = 1.0,
    multiplier: float = 2.0,
    max_delay: float = 30.0,
    retryable_errors: tuple[type[Exception], ...] = (ProviderUnavailableError,),
):
    """Retry an async callable with exponential backoff and jitter."""
    last_error: Exception | None = None
    delay = initial_delay

    for attempt in range(max_attempts):
        try:
            return await coro_factory()
        except retryable_errors as e:
            last_error = e
            if attempt == max_attempts - 1:
                break
            # Add jitter: ±25% of delay
            jitter = delay * (0.75 + random.random() * 0.5)
            await asyncio.sleep(jitter)
            delay = min(delay * multiplier, max_delay)

    raise last_error  # type: ignore[misc]
