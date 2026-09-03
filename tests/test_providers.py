"""Tests for provider base models, the registry, the mock provider, and quota info."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

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
    TaskType,
    Usage,
)
from nvh.providers.mock_provider import MockProvider
from nvh.providers.registry import ProviderRegistry


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


class TestProviderRegistry:
    def test_register_and_get(self) -> None:
        reg = ProviderRegistry()
        mock_prov = MagicMock()
        reg.register("test", mock_prov)
        assert reg.get("test") is mock_prov

    def test_get_nonexistent_raises(self) -> None:
        reg = ProviderRegistry()
        with pytest.raises(KeyError, match="not registered"):
            reg.get("nope")

    def test_register_duplicate_overwrites(self) -> None:
        reg = ProviderRegistry()
        p1, p2 = MagicMock(), MagicMock()
        reg.register("x", p1)
        reg.register("x", p2)
        assert reg.get("x") is p2

    def test_has(self) -> None:
        reg = ProviderRegistry()
        assert not reg.has("foo")
        reg.register("foo", MagicMock())
        assert reg.has("foo")

    def test_list_models_no_filter(self) -> None:
        reg = ProviderRegistry()
        reg._model_catalog["m1"] = ModelInfo(model_id="m1", provider="a")
        reg._model_catalog["m2"] = ModelInfo(model_id="m2", provider="b")
        models = reg.list_models()
        assert len(models) == 2

    def test_list_models_with_provider_filter(self) -> None:
        reg = ProviderRegistry()
        reg._model_catalog["m1"] = ModelInfo(model_id="m1", provider="a")
        reg._model_catalog["m2"] = ModelInfo(model_id="m2", provider="b")
        models = reg.list_models(provider="a")
        assert len(models) == 1
        assert models[0].model_id == "m1"


class TestMockProvider:
    def test_construct(self):
        provider = MockProvider()
        assert provider.name == "mock" or isinstance(provider.name, str)

    @pytest.mark.asyncio
    async def test_complete(self):
        provider = MockProvider()
        resp = await provider.complete(
            messages=[Message(role="user", content="hello")],
        )
        assert isinstance(resp, CompletionResponse)
        assert len(resp.content) > 0

    @pytest.mark.asyncio
    async def test_stream(self):
        provider = MockProvider()
        chunks = []
        async for chunk in provider.stream(
            messages=[Message(role="user", content="hello")],
        ):
            chunks.append(chunk)
        assert len(chunks) >= 1
        assert any(c.is_final for c in chunks)

    @pytest.mark.asyncio
    async def test_list_models(self):
        provider = MockProvider()
        models = await provider.list_models()
        assert isinstance(models, list)

    @pytest.mark.asyncio
    async def test_health_check(self):
        provider = MockProvider()
        status = await provider.health_check()
        assert status.healthy is True


class TestOllamaProviderBaseUrl:
    """The adapter's address goes through ``nvh.utils.ollama.ollama_base_url``: an IPv6 loopback
    literal survives it bracketed and counts as this machine; any other IPv6 host is remote."""

    @pytest.fixture(autouse=True)
    def _no_env(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
        monkeypatch.delenv("OLLAMA_HOST", raising=False)

    def test_ipv6_loopback_is_kept_bracketed_and_local(self):
        from nvh.providers import ollama_provider as op

        provider = op.OllamaProvider(base_url="http://[::1]:11434")
        assert provider._base_url == "http://[::1]:11434"
        assert op._daemon_is_local(provider._base_url)

    def test_other_ipv6_literals_are_remote(self):
        from nvh.providers import ollama_provider as op

        provider = op.OllamaProvider(base_url="http://[fd00::5]:11434")
        assert provider._base_url == "http://[fd00::5]:11434"
        assert not op._daemon_is_local(provider._base_url)

    def test_default_is_ipv4_loopback_and_a_table_model(self):
        from nvh.core import local_models
        from nvh.providers import ollama_provider as op

        provider = op.OllamaProvider()
        assert provider._base_url == "http://127.0.0.1:11434"
        assert local_models.pick_for_tag(provider._default_model.removeprefix("ollama/")) is not None


class TestQuotaInfo:
    def test_import(self):
        from nvh.providers import quota_info
        assert quota_info is not None

    def test_has_quota_data(self):
        from nvh.providers import quota_info
        assert (hasattr(quota_info, "PROVIDER_QUOTAS") or
                hasattr(quota_info, "get_quota") or
                hasattr(quota_info, "QuotaInfo"))

    def test_provider_quotas_structure(self):
        from nvh.providers.quota_info import PROVIDER_QUOTAS, QuotaInfo
        assert len(PROVIDER_QUOTAS) >= 5
        for name, qi in PROVIDER_QUOTAS.items():
            assert isinstance(qi, QuotaInfo)
            assert qi.provider == name

    def test_get_quota_info_known_and_unknown(self):
        from nvh.providers.quota_info import get_quota_info
        info = get_quota_info("groq")
        assert info.provider == "groq"
        assert info.tier == "free"
        unknown = get_quota_info("made_up_provider")
        assert unknown.tier == "unknown"

    def test_parse_retry_after(self):
        from nvh.providers.quota_info import parse_retry_after
        assert parse_retry_after("please retry in 5.2s") == 5.2
        assert parse_retry_after("no info here") is None
