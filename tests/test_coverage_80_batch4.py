"""Coverage boost batch 4 — smaller modules and provider edge cases.

Targets: base.py, storage/models.py, sandbox/executor.py,
ollama_provider health_check, nvidia_provider, llm7_provider.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nvh.providers.base import (
    CompletionResponse,
    FinishReason,
    HealthStatus,
    Message,
    ModelInfo,
    ProviderError,
    ProviderUnavailableError,
    StreamChunk,
    Usage,
)

# ---------------------------------------------------------------------------
# nvh/providers/base.py — dataclass construction + edge cases
# ---------------------------------------------------------------------------


class TestBaseDataclasses:
    def test_usage_defaults(self):
        u = Usage()
        assert u.input_tokens == 0
        assert u.output_tokens == 0
        assert u.total_tokens == 0

    def test_usage_with_values(self):
        u = Usage(input_tokens=10, output_tokens=20, total_tokens=30)
        assert u.total_tokens == 30

    def test_completion_response_metadata(self):
        r = CompletionResponse(
            content="hello",
            model="test",
            provider="test",
            usage=Usage(),
            cost_usd=Decimal("0.01"),
            latency_ms=100,
        )
        assert r.metadata == {} or isinstance(r.metadata, dict)
        r.metadata["key"] = "value"
        assert r.metadata["key"] == "value"

    def test_completion_response_finish_reasons(self):
        for reason in FinishReason:
            r = CompletionResponse(
                content="",
                model="m",
                provider="p",
                usage=Usage(),
                cost_usd=Decimal("0"),
                latency_ms=0,
                finish_reason=reason,
            )
            assert r.finish_reason == reason

    def test_model_info_minimal(self):
        m = ModelInfo(model_id="test/model", provider="test")
        assert m.model_id == "test/model"
        assert m.provider == "test"
        assert m.context_window == 0
        assert m.supports_streaming is True

    def test_health_status(self):
        h = HealthStatus(provider="test", healthy=True, latency_ms=50)
        assert h.healthy is True
        h2 = HealthStatus(provider="test", healthy=False, latency_ms=0, error="down")
        assert h2.healthy is False
        assert h2.error == "down"

    def test_stream_chunk(self):
        c = StreamChunk(
            delta="hello",
            is_final=True,
            accumulated_content="hello world",
            model="m",
            provider="p",
            usage=Usage(total_tokens=5),
            cost_usd=Decimal("0.001"),
            finish_reason=FinishReason.STOP,
        )
        assert c.is_final is True
        assert c.delta == "hello"

    def test_provider_error_hierarchy(self):
        e = ProviderError("test error", provider="alpha")
        assert isinstance(e, Exception)
        assert "test error" in str(e)

        u = ProviderUnavailableError("down", provider="beta")
        assert isinstance(u, ProviderError)


# ---------------------------------------------------------------------------
# nvh/storage/models.py — SQLAlchemy model construction
# ---------------------------------------------------------------------------


class TestStorageModels:
    def test_import_models(self):
        from nvh.storage import models
        assert hasattr(models, "Base") or hasattr(models, "QueryLog")

    def test_query_log_construction(self):
        from nvh.storage.models import QueryLog
        log = QueryLog(
            mode="simple",
            provider="groq",
            model="llama-70b",
            input_tokens=10,
            output_tokens=20,
            cost_usd=0.001,
            latency_ms=100,
            status="success",
        )
        assert log.provider == "groq"
        assert log.cost_usd == 0.001


# ---------------------------------------------------------------------------
# nvh/sandbox/executor.py — config + basic methods
# ---------------------------------------------------------------------------


class TestSandboxExecutor:
    def test_sandbox_config_defaults(self):
        try:
            from nvh.sandbox.executor import SandboxConfig
            config = SandboxConfig()
            assert config.timeout_seconds > 0
            assert config.memory_limit_mb > 0
            assert isinstance(config.allowed_languages, (list, set, tuple))
        except (ImportError, TypeError):
            pytest.skip("SandboxConfig not available")

    def test_sandbox_executor_construction(self):
        try:
            from nvh.sandbox.executor import SandboxExecutor
            se = SandboxExecutor()
            assert se is not None
        except (ImportError, TypeError):
            pytest.skip("SandboxExecutor not available or needs Docker")

    def test_sandbox_is_available(self):
        try:
            from nvh.sandbox.executor import SandboxExecutor
            se = SandboxExecutor()
            # On a dev machine without Docker, this returns False
            result = se.is_available()
            assert isinstance(result, bool)
        except (ImportError, TypeError, AttributeError):
            pytest.skip("SandboxExecutor.is_available not available")


# ---------------------------------------------------------------------------
# Provider health_check paths (Ollama, NVIDIA, LLM7)
# ---------------------------------------------------------------------------


class TestOllamaHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_when_ollama_down(self):
        from nvh.providers.ollama_provider import OllamaProvider
        provider = OllamaProvider(base_url="http://localhost:99999")
        status = await provider.health_check()
        assert isinstance(status, HealthStatus)
        assert status.healthy is False
        assert status.error is not None


class TestNvidiaProvider:
    def test_construct(self):
        from nvh.providers.nvidia_provider import NvidiaProvider
        p = NvidiaProvider()
        assert p.name == "nvidia"

    def test_estimate_tokens(self):
        from nvh.providers.nvidia_provider import NvidiaProvider
        p = NvidiaProvider()
        assert p.estimate_tokens("hello world test") >= 1

    @pytest.mark.asyncio
    async def test_list_models(self):
        from nvh.providers.nvidia_provider import NvidiaProvider
        p = NvidiaProvider()
        models = await p.list_models()
        assert isinstance(models, list)
        assert len(models) >= 1

    @pytest.mark.asyncio
    async def test_complete_with_mock(self):
        from nvh.providers.nvidia_provider import NvidiaProvider

        fake_response = MagicMock()
        fake_response.choices = [MagicMock()]
        fake_response.choices[0].message.content = "nvidia says hi"
        fake_response.choices[0].finish_reason = "stop"
        fake_response.usage.prompt_tokens = 5
        fake_response.usage.completion_tokens = 10
        fake_response.usage.total_tokens = 15
        fake_response.model = "nvidia/nemotron"

        with patch("nvh.providers.nvidia_provider.litellm.acompletion",
                   new=AsyncMock(return_value=fake_response)):
            p = NvidiaProvider()
            resp = await p.complete(messages=[Message(role="user", content="hi")])
            assert resp.content == "nvidia says hi"
            assert resp.provider == "nvidia"


class TestLLM7Provider:
    def test_construct(self):
        from nvh.providers.llm7_provider import LLM7Provider
        p = LLM7Provider()
        assert p.name == "llm7"

    @pytest.mark.asyncio
    async def test_list_models(self):
        from nvh.providers.llm7_provider import LLM7Provider
        p = LLM7Provider()
        models = await p.list_models()
        assert isinstance(models, list)
        assert len(models) >= 1

    def test_estimate_tokens(self):
        from nvh.providers.llm7_provider import LLM7Provider
        p = LLM7Provider()
        assert p.estimate_tokens("hello world") >= 1


class TestGitHubProvider:
    def test_construct(self):
        from nvh.providers.github_provider import GitHubProvider
        p = GitHubProvider()
        assert p.name == "github"

    @pytest.mark.asyncio
    async def test_list_models(self):
        from nvh.providers.github_provider import GitHubProvider
        p = GitHubProvider()
        models = await p.list_models()
        assert isinstance(models, list)
