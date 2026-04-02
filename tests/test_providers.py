"""Tests for provider base models and registry."""

from decimal import Decimal

from nvh.core.rate_limiter import CircuitBreaker, ProviderRateManager, TokenBucket
from nvh.providers.base import (
    CircuitState,
    CompletionResponse,
    Message,
    ModelInfo,
    StreamChunk,
    TaskType,
    Usage,
)


class TestDataModels:
    def test_usage_defaults(self):
        u = Usage()
        assert u.input_tokens == 0
        assert u.output_tokens == 0

    def test_completion_response(self):
        r = CompletionResponse(
            content="Hello",
            model="gpt-4o",
            provider="openai",
            usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
            cost_usd=Decimal("0.001"),
            latency_ms=500,
        )
        assert r.content == "Hello"
        assert r.cost_usd == Decimal("0.001")
        assert not r.cache_hit

    def test_stream_chunk(self):
        chunk = StreamChunk(delta="Hello", model="gpt-4o", provider="openai")
        assert chunk.delta == "Hello"
        assert not chunk.is_final

    def test_message(self):
        m = Message(role="user", content="Hello")
        assert m.role == "user"

    def test_model_info(self):
        info = ModelInfo(
            model_id="gpt-4o",
            provider="openai",
            context_window=128000,
            capability_scores={"code_generation": 0.88},
        )
        assert info.capability_scores["code_generation"] == 0.88

    def test_task_types(self):
        assert TaskType.CODE_GENERATION.value == "code_generation"
        assert TaskType.MATH.value == "math"


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


class TestTokenBucket:
    def test_consume(self):
        bucket = TokenBucket(capacity=10, refill_rate=1.0)
        assert bucket.consume(5)
        assert bucket.consume(5)
        assert not bucket.consume(1)

    def test_refill(self):
        bucket = TokenBucket(capacity=10, refill_rate=100.0)
        bucket.consume(10)
        import time
        time.sleep(0.1)
        assert bucket.consume(1)


class TestProviderRateManager:
    def test_health_score_healthy(self):
        mgr = ProviderRateManager()
        assert mgr.get_health_score("test") == 1.0

    def test_health_score_after_failures(self):
        mgr = ProviderRateManager()
        from nvh.providers.base import ProviderUnavailableError
        for _ in range(3):
            mgr.record_failure("test", ProviderUnavailableError("fail", provider="test"))
        score = mgr.get_health_score("test")
        assert score < 1.0

    def test_reset(self):
        mgr = ProviderRateManager()
        from nvh.providers.base import ProviderUnavailableError
        mgr.record_failure("test", ProviderUnavailableError("fail", provider="test"))
        mgr.reset("test")
        assert mgr.get_health_score("test") == 1.0
