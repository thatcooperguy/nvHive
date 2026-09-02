"""OpenAICompatibleProvider: the spec-driven behaviour the contract suite does not pin."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nvh.config.settings import CouncilConfig, ProviderConfig
from nvh.providers.base import (
    AuthenticationError,
    ContentFilterError,
    FinishReason,
    HealthStatus,
    InvalidRequestError,
    Message,
    ModelNotFoundError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    TokenLimitError,
    Usage,
)
from nvh.providers.lazy_provider import LazyProvider
from nvh.providers.openai_compatible import (
    PROVIDER_SPECS,
    OpenAICompatibleProvider,
    _build_messages,
    _calc_cost,
    _map_error,
)
from nvh.providers.registry import ProviderRegistry, lazy_adapter, resolve_provider_key

_ACOMPLETION = "nvh.providers.openai_compatible.litellm.acompletion"
_ASYNC_CLIENT = "nvh.providers.openai_compatible.httpx.AsyncClient"


def _spec_provider(name: str) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(PROVIDER_SPECS[name])


def _fake_http(status_code: int = 200, payload=None):
    """A stand-in for ``httpx.AsyncClient`` recording every GET."""
    calls: list[tuple[str, dict]] = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            calls.append((url, headers or {}))
            return SimpleNamespace(status_code=status_code, json=lambda: payload)

    return Client, calls


def _response(model: str = "served-model"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        model=model,
    )


async def _stream(finish_reason: str = "stop"):
    yield SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content="hi"), finish_reason=None)],
    )
    yield SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=""), finish_reason=finish_reason)],
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=10, total_tokens=15),
    )


class TestConstruction:
    def test_blank_arguments_fall_back_to_spec(self):
        spec = PROVIDER_SPECS["groq"]
        p = OpenAICompatibleProvider(
            spec, default_model="", fallback_model="", base_url=None, provider_name="",
        )
        assert p._default_model == spec.default_model
        assert p._fallback_model == spec.fallback_model
        assert p.name == "groq"
        assert p._base_url is None  # no spec base_url: litellm's own groq endpoint

    def test_explicit_arguments_win(self):
        p = OpenAICompatibleProvider(
            PROVIDER_SPECS["nvidia"],
            api_key="k",
            default_model="a",
            fallback_model="b",
            base_url="http://nim.local/v1",
            provider_name="nim-local",
        )
        assert p.name == "nim-local"
        assert p._fallback_model == "b"
        assert p._kwargs("a") == {
            "model": "nvidia_nim/a", "api_key": "k", "api_base": "http://nim.local/v1",
        }


def _clear_provider_env(monkeypatch, name: str) -> None:
    """Remove every env var ``resolve_provider_key`` consults for ``name``."""
    monkeypatch.delenv("NVH_USE_KEYRING", raising=False)
    for var in (f"COUNCIL_{name.upper()}_API_KEY", f"{name.upper()}_API_KEY", *PROVIDER_SPECS[name].env_keys):
        monkeypatch.delenv(var, raising=False)


class TestApiKeyResolution:
    def test_spec_env_keys_are_consulted_in_order(self, monkeypatch):
        _clear_provider_env(monkeypatch, "nvidia")
        monkeypatch.setenv("NIM_API_KEY", "nim-key")
        monkeypatch.setenv("HIVE_NVIDIA_API_KEY", "hive-key")
        assert OpenAICompatibleProvider(PROVIDER_SPECS["nvidia"])._api_key == "nim-key"

    def test_explicit_key_beats_env(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "env-key")
        assert OpenAICompatibleProvider(PROVIDER_SPECS["nvidia"], api_key="k")._api_key == "k"

    def test_conventional_env_var_is_resolved_by_the_adapter(self, monkeypatch):
        # The registry, the adapter and `nvh status --deep` share one resolver,
        # so GROQ_API_KEY reaches litellm explicitly rather than by accident.
        _clear_provider_env(monkeypatch, "groq")
        monkeypatch.setenv("GROQ_API_KEY", "env-key")
        p = OpenAICompatibleProvider(PROVIDER_SPECS["groq"])
        assert p._api_key == "env-key"
        assert p._kwargs("groq/openai/gpt-oss-120b")["api_key"] == "env-key"

    @pytest.mark.parametrize("name", sorted(PROVIDER_SPECS))
    def test_historical_hive_spelling_works_for_every_spec(self, name, monkeypatch):
        _clear_provider_env(monkeypatch, name)
        monkeypatch.setenv(f"HIVE_{name.upper()}_API_KEY", "hive-key")
        assert OpenAICompatibleProvider(PROVIDER_SPECS[name])._api_key == "hive-key"

    @pytest.mark.parametrize(
        ("name", "var"), [("nvidia", "NIM_API_KEY"), ("groq", "HIVE_GROQ_API_KEY")],
    )
    def test_check_and_adapter_agree(self, name, var, monkeypatch):
        _clear_provider_env(monkeypatch, name)
        monkeypatch.setenv(var, "the-key")
        assert resolve_provider_key(name) == ("the-key", f"env:{var}")
        assert OpenAICompatibleProvider(PROVIDER_SPECS[name])._api_key == "the-key"

    def test_anonymous_key_when_nothing_configured(self, monkeypatch):
        _clear_provider_env(monkeypatch, "llm7")
        p = OpenAICompatibleProvider(PROVIDER_SPECS["llm7"])
        assert p._kwargs("gpt-oss") == {
            "model": "openai/gpt-oss",
            "api_key": "anonymous",
            "api_base": "https://api.llm7.io/v1",
        }

    def test_unexpanded_placeholder_is_ignored(self, monkeypatch):
        placeholder = "${LLM7_API_KEY:-anonymous}"
        _clear_provider_env(monkeypatch, "llm7")
        _clear_provider_env(monkeypatch, "openai")
        monkeypatch.setenv("LLM7_API_KEY", "real")
        assert OpenAICompatibleProvider(PROVIDER_SPECS["llm7"], api_key=placeholder)._api_key == "real"
        monkeypatch.delenv("LLM7_API_KEY")
        assert OpenAICompatibleProvider(PROVIDER_SPECS["llm7"], api_key=placeholder)._api_key == "anonymous"
        assert OpenAICompatibleProvider(PROVIDER_SPECS["openai"], api_key="${OPENAI_API_KEY}")._api_key == ""


class TestSpecDefaults:
    def test_timeouts_come_from_the_spec_unless_given(self):
        assert OpenAICompatibleProvider(PROVIDER_SPECS["groq"])._timeout == 120
        for name in ("nvidia", "llm7", "perplexity", "siliconflow"):
            assert OpenAICompatibleProvider(PROVIDER_SPECS[name])._timeout == 600, name
        assert OpenAICompatibleProvider(PROVIDER_SPECS["nvidia"], timeout=7)._timeout == 7

    def test_health_model_defaults_to_default_model(self):
        nvidia = OpenAICompatibleProvider(PROVIDER_SPECS["nvidia"])
        assert nvidia._health_model == PROVIDER_SPECS["nvidia"].fallback_model  # the quick 8B
        groq = OpenAICompatibleProvider(PROVIDER_SPECS["groq"], default_model="groq/x")
        assert groq._health_model == "groq/x"


# Provider litellm must resolve for each spec's bare default model once routed.
_LITELLM_PROVIDER = {
    "openai": "openai", "anthropic": "anthropic", "google": "gemini", "groq": "groq",
    "grok": "xai", "mistral": "mistral", "cohere": "cohere_chat", "deepseek": "deepseek",
    "perplexity": "perplexity", "together": "together_ai", "fireworks": "fireworks_ai",
    "openrouter": "openrouter", "cerebras": "cerebras", "sambanova": "sambanova",
    "huggingface": "huggingface", "ai21": "ai21_chat", "nvidia": "nvidia_nim",
    "siliconflow": "openai", "llm7": "openai",
}


class TestLitellmPrefix:
    @pytest.mark.parametrize("name", sorted(PROVIDER_SPECS))
    def test_bare_model_routes_to_the_spec_provider(self, name):
        import litellm

        spec = PROVIDER_SPECS[name]
        bare = spec.default_model.removeprefix(spec.litellm_prefix)
        routed = spec.route(bare)
        # siliconflow/llm7 ship bare IDs and gain "openai/" only at request time.
        assert routed == spec.route(spec.default_model)
        assert routed.startswith(spec.litellm_prefix)
        assert litellm.get_llm_provider(routed)[1] == _LITELLM_PROVIDER[name]

    @pytest.mark.parametrize("name", sorted(PROVIDER_SPECS))
    def test_routing_is_idempotent(self, name):
        spec = PROVIDER_SPECS[name]
        for model in (spec.default_model, spec.fallback_model):
            once = spec.route(model)
            assert spec.route(once) == once
            assert once.count(spec.litellm_prefix) <= 1 or not spec.litellm_prefix

    def test_partial_route_gets_only_the_missing_lead(self):
        fw = PROVIDER_SPECS["fireworks"]
        assert fw.route("accounts/fireworks/models/x") == "fireworks_ai/accounts/fireworks/models/x"
        assert fw.route("x") == "fireworks_ai/accounts/fireworks/models/x"

    @pytest.mark.asyncio
    async def test_bare_model_is_routed_and_priced_under_its_prefix(self):
        p = OpenAICompatibleProvider(PROVIDER_SPECS["groq"], api_key="k")
        with patch(_ACOMPLETION, new=AsyncMock(return_value=_response())) as ac, \
                patch("litellm.cost_per_token", return_value=(0.001, 0.002)) as cpt:
            await p.complete([Message(role="user", content="hi")], model="openai/gpt-oss-20b")
        assert ac.await_args.kwargs["model"] == "groq/openai/gpt-oss-20b"
        assert cpt.call_args.kwargs["model"] == "groq/openai/gpt-oss-20b"


class TestCost:
    @pytest.mark.asyncio
    async def test_zero_cost_spec_never_prices(self):
        p = OpenAICompatibleProvider(PROVIDER_SPECS["llm7"])
        with patch(_ACOMPLETION, new=AsyncMock(return_value=_response())), \
                patch("litellm.cost_per_token", return_value=(1.0, 1.0)):
            resp = await p.complete([Message(role="user", content="hi")])
        assert resp.cost_usd == Decimal("0")

        with patch(_ACOMPLETION, new=AsyncMock(return_value=_stream())), \
                patch("litellm.cost_per_token", return_value=(1.0, 1.0)):
            chunks = [c async for c in p.stream([Message(role="user", content="hi")])]
        assert chunks[-1].cost_usd == Decimal("0")

    @pytest.mark.asyncio
    async def test_priced_spec_uses_litellm_cost_per_token(self):
        p = OpenAICompatibleProvider(PROVIDER_SPECS["groq"])
        with patch(_ACOMPLETION, new=AsyncMock(return_value=_response())), \
                patch("litellm.cost_per_token", return_value=(0.001, 0.002)) as cpt:
            resp = await p.complete([Message(role="user", content="hi")])
        assert resp.cost_usd == Decimal("0.003")
        cpt.assert_called_once_with(
            model="groq/openai/gpt-oss-120b", prompt_tokens=10, completion_tokens=20,
        )


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_prefers_free_models_endpoint_over_a_billed_ping(self):
        p = OpenAICompatibleProvider(PROVIDER_SPECS["nvidia"], api_key="k")
        client, calls = _fake_http(200, {"data": [{"id": "a"}, {"id": "b"}]})
        with patch(_ASYNC_CLIENT, client), patch(_ACOMPLETION, new=AsyncMock()) as ac:
            status = await p.health_check()
        assert status.healthy is True and status.models_available == 2
        assert calls == [("https://integrate.api.nvidia.com/v1/models", {"Authorization": "Bearer k"})]
        ac.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_uses_litellm_known_base_when_spec_has_none(self):
        p = OpenAICompatibleProvider(PROVIDER_SPECS["groq"], api_key="k")
        client, calls = _fake_http(200, {"data": []})
        with patch(_ASYNC_CLIENT, client):
            assert (await p.health_check()).healthy is True
        assert calls[0][0] == "https://api.groq.com/openai/v1/models"

    @pytest.mark.asyncio
    async def test_falls_back_to_ping_with_health_model_when_models_is_missing(self):
        p = OpenAICompatibleProvider(PROVIDER_SPECS["nvidia"], api_key="k")
        client, _ = _fake_http(404)
        with patch(_ASYNC_CLIENT, client), \
                patch(_ACOMPLETION, new=AsyncMock(return_value=_response())) as ac:
            status = await p.health_check()
        assert status.healthy is True
        ac.assert_awaited_once_with(
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            timeout=15,
            model="nvidia_nim/meta/llama-3.1-8b-instruct",  # the 8B, not the 70B default
            api_key="k",
            api_base="https://integrate.api.nvidia.com/v1",
        )

    @pytest.mark.asyncio
    async def test_pings_when_no_models_endpoint_is_known(self):
        # litellm publishes no base for openai; the ping is the only probe left.
        p = OpenAICompatibleProvider(PROVIDER_SPECS["openai"], api_key="k")
        client, calls = _fake_http(200)
        with patch(_ASYNC_CLIENT, client), \
                patch(_ACOMPLETION, new=AsyncMock(return_value=_response())) as ac:
            assert (await p.health_check()).healthy is True
        assert calls == []
        assert ac.await_args.kwargs["model"] == "gpt-5.6-terra"

    @pytest.mark.asyncio
    async def test_auth_failure_on_models_is_unhealthy_without_a_ping(self):
        p = OpenAICompatibleProvider(PROVIDER_SPECS["nvidia"], api_key="bad")
        client, _ = _fake_http(401)
        with patch(_ASYNC_CLIENT, client), patch(_ACOMPLETION, new=AsyncMock()) as ac:
            status = await p.health_check()
        assert status.healthy is False and "HTTP 401" in status.error
        ac.assert_not_awaited()


class TestRequestShaping:

    @pytest.mark.asyncio
    async def test_complete_forwards_timeout_and_prefixed_model(self):
        p = OpenAICompatibleProvider(PROVIDER_SPECS["siliconflow"], api_key="k", timeout=7)
        with patch(_ACOMPLETION, new=AsyncMock(return_value=_response())) as ac:
            resp = await p.complete([Message(role="user", content="hi")], model="Qwen/Qwen3-8B")
        kw = ac.await_args.kwargs
        assert kw["model"] == "openai/Qwen/Qwen3-8B"
        assert kw["timeout"] == 7
        assert kw["api_base"] == "https://api.siliconflow.cn/v1"
        assert resp.model == "served-model"  # litellm's reported model wins

    @pytest.mark.asyncio
    async def test_list_models_dedupes_default_and_fallback(self):
        cerebras = await OpenAICompatibleProvider(PROVIDER_SPECS["cerebras"]).list_models()
        assert [m.model_id for m in cerebras] == ["cerebras/gpt-oss-120b"]
        groq = await OpenAICompatibleProvider(PROVIDER_SPECS["groq"]).list_models()
        assert [m.model_id for m in groq] == ["groq/openai/gpt-oss-120b", "groq/openai/gpt-oss-20b"]

    @pytest.mark.asyncio
    async def test_stream_maps_tool_calls_finish_reason(self):
        p = OpenAICompatibleProvider(PROVIDER_SPECS["openai"])
        with patch(_ACOMPLETION, new=AsyncMock(return_value=_stream("tool_calls"))):
            chunks = [c async for c in p.stream([Message(role="user", content="hi")])]
        assert chunks[-1].finish_reason == FinishReason.TOOL_CALLS
        assert chunks[-1].accumulated_content == "hi"


class TestRegistryWiring:
    def test_lazy_adapter_drops_blank_kwargs(self):
        lazy = lazy_adapter("groq", api_key="k", default_model="", fallback_model="", base_url="")
        assert lazy._module_path == "nvh.providers.openai_compatible"
        assert lazy._kwargs == {"spec": PROVIDER_SPECS["groq"], "provider_name": "groq", "api_key": "k"}

    def test_lazy_adapter_bespoke_keeps_adapter_default_model(self):
        lazy = lazy_adapter("ollama", default_model="", base_url=None)
        assert lazy._module_path == "nvh.providers.ollama_provider"
        assert lazy._kwargs == {"provider_name": "ollama"}

    def test_unknown_type_uses_openai_route_with_custom_base_url(self):
        lazy = lazy_adapter(
            "myproxy", "openai_compatible",
            api_key="k", base_url="http://proxy:8000/v1", default_model="local-model",
        )
        provider = lazy._load()
        assert provider.spec is PROVIDER_SPECS["openai"]
        assert provider.name == "myproxy"
        assert provider._kwargs("local-model") == {
            "model": "local-model", "api_key": "k", "api_base": "http://proxy:8000/v1",
        }

    def test_stanza_without_default_model_gets_shipped_default(self):
        # ProviderConfig.default_model defaults to "" and used to override the adapter's own.
        config = CouncilConfig(
            providers={
                "groq": ProviderConfig(api_key="k", enabled=True),
                "ollama": ProviderConfig(enabled=True),
            }
        )
        registry = ProviderRegistry()
        assert registry.setup_from_config(config) == ["groq", "ollama"]
        groq = registry.get("groq")
        assert isinstance(groq, LazyProvider)
        assert groq._load()._default_model == PROVIDER_SPECS["groq"].default_model
        assert registry.get("ollama")._load()._default_model == "ollama/gemma3:4b"


# ---------------------------------------------------------------------------
# Module helpers: message shaping, cost, error mapping
# ---------------------------------------------------------------------------


class TestBuildMessages:
    def test_without_system_prompt(self) -> None:
        msgs = [Message(role="user", content="hello")]
        result = _build_messages(msgs)
        assert len(result) == 1
        assert result[0] == {"role": "user", "content": "hello"}

    def test_with_system_prompt(self) -> None:
        msgs = [Message(role="user", content="hello")]
        result = _build_messages(msgs, system_prompt="You are helpful.")
        assert len(result) == 2
        assert result[0] == {"role": "system", "content": "You are helpful."}
        assert result[1] == {"role": "user", "content": "hello"}

    def test_with_name_field(self) -> None:
        msgs = [Message(role="user", content="hi", name="alice")]
        result = _build_messages(msgs)
        assert result[0]["name"] == "alice"

    def test_without_name_field(self) -> None:
        msgs = [Message(role="user", content="hi")]
        result = _build_messages(msgs)
        assert "name" not in result[0]

    def test_build_messages_no_system(self):
        msgs = _build_messages([Message(role="user", content="hi")], None)
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    def test_build_messages_with_system(self):
        msgs = _build_messages(
            [Message(role="user", content="hi")],
            "You are helpful",
        )
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == "You are helpful"


class TestCalcCost:
    def test_sums_prompt_and_completion_cost(self) -> None:
        usage = Usage(input_tokens=100, output_tokens=50, total_tokens=150)
        with patch("litellm.cost_per_token", return_value=(0.001, 0.000234)) as cpt:
            cost = _calc_cost("gpt-4o", usage)
        assert isinstance(cost, Decimal)
        assert cost == Decimal("0.001234")
        cpt.assert_called_once_with(model="gpt-4o", prompt_tokens=100, completion_tokens=50)

    def test_returns_zero_on_exception(self) -> None:
        usage = Usage(input_tokens=10, output_tokens=5, total_tokens=15)
        with patch("litellm.cost_per_token", side_effect=Exception("nope")):
            cost = _calc_cost("gpt-4o", usage)
        assert cost == Decimal("0")

    def test_priced_default_is_nonzero(self) -> None:
        # Regression guard: the old completion_cost(prompt_tokens=...) call raised
        # TypeError on every request and silently reported $0 for all providers.
        usage = Usage(input_tokens=1000, output_tokens=1000, total_tokens=2000)
        assert _calc_cost("gpt-5.6-terra", usage) > Decimal("0")

    def test_calc_cost_returns_decimal(self):
        usage = Usage(input_tokens=1000, output_tokens=500, total_tokens=1500)
        cost = _calc_cost("gpt-4o", usage)
        assert isinstance(cost, Decimal)
        assert cost >= 0


class TestMapError:
    @patch("nvh.providers.quota_info.get_quota_info")
    def test_authentication_error(self, mock_qi: MagicMock) -> None:
        mock_qi.return_value = MagicMock(upgrade_url="https://example.com")
        exc = type("AuthenticationError", (Exception,), {})("bad key")
        result = _map_error(exc, "openai")
        assert isinstance(result, AuthenticationError)

    @patch("nvh.providers.quota_info.format_rate_limit_message", return_value="slow down")
    @patch("nvh.providers.quota_info.parse_retry_after", return_value=5)
    def test_rate_limit_error(self, _pr: MagicMock, _fr: MagicMock) -> None:
        exc = type("RateLimitError", (Exception,), {})("429 too many")
        result = _map_error(exc, "openai")
        assert isinstance(result, RateLimitError)

    def test_invalid_request_token_limit(self) -> None:
        exc = type("InvalidRequestError", (Exception,), {})("context_length exceeded")
        result = _map_error(exc, "openai")
        assert isinstance(result, TokenLimitError)

    def test_invalid_request_content_filter(self) -> None:
        exc = type("InvalidRequestError", (Exception,), {})("content_filter triggered")
        result = _map_error(exc, "openai")
        assert isinstance(result, ContentFilterError)

    def test_invalid_request_generic(self) -> None:
        exc = type("InvalidRequestError", (Exception,), {})("something else 400")
        result = _map_error(exc, "openai")
        assert isinstance(result, InvalidRequestError)

    def test_not_found_error(self) -> None:
        exc = type("NotFoundError", (Exception,), {})("404 model gone")
        result = _map_error(exc, "openai")
        assert isinstance(result, ModelNotFoundError)

    def test_service_unavailable_error(self) -> None:
        exc = type("ServiceUnavailableError", (Exception,), {})("503 down")
        result = _map_error(exc, "openai")
        assert isinstance(result, ProviderUnavailableError)

    def test_generic_exception(self) -> None:
        exc = Exception("something unknown happened")
        result = _map_error(exc, "openai")
        assert isinstance(result, ProviderError)
        assert not isinstance(result, AuthenticationError)

    def test_map_error_generic(self):
        err = _map_error(Exception("something broke"), "openai")
        assert isinstance(err, ProviderError)


# ---------------------------------------------------------------------------
# Per-spec providers through the shared adapter
# ---------------------------------------------------------------------------


class TestNvidiaProvider:
    def test_construct(self):
        p = _spec_provider("nvidia")
        assert p.name == "nvidia"

    def test_estimate_tokens(self):
        p = _spec_provider("nvidia")
        assert p.estimate_tokens("hello world test") >= 1

    @pytest.mark.asyncio
    async def test_list_models(self):
        p = _spec_provider("nvidia")
        models = await p.list_models()
        assert isinstance(models, list)
        assert len(models) >= 1

    @pytest.mark.asyncio
    async def test_complete_with_mock(self):
        fake_response = MagicMock()
        fake_response.choices = [MagicMock()]
        fake_response.choices[0].message.content = "nvidia says hi"
        fake_response.choices[0].finish_reason = "stop"
        fake_response.usage.prompt_tokens = 5
        fake_response.usage.completion_tokens = 10
        fake_response.usage.total_tokens = 15
        fake_response.model = "nvidia/nemotron"

        with patch(_ACOMPLETION, new=AsyncMock(return_value=fake_response)):
            p = _spec_provider("nvidia")
            resp = await p.complete(messages=[Message(role="user", content="hi")])
            assert resp.content == "nvidia says hi"
            assert resp.provider == "nvidia"


class TestLLM7Provider:
    def test_construct(self):
        p = _spec_provider("llm7")
        assert p.name == "llm7"

    @pytest.mark.asyncio
    async def test_list_models(self):
        p = _spec_provider("llm7")
        models = await p.list_models()
        assert isinstance(models, list)
        assert len(models) >= 1

    def test_estimate_tokens(self):
        p = _spec_provider("llm7")
        assert p.estimate_tokens("hello world") >= 1


class TestOpenAISpecProvider:
    @pytest.mark.asyncio
    async def test_health_check_success_mock(self):
        fake = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model="gpt-4o",
        )
        with patch(_ACOMPLETION, new=AsyncMock(return_value=fake)):
            p = _spec_provider("openai")
            status = await p.health_check()
            assert status.healthy is True
            assert status.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_health_check_failure_mock(self):
        with patch(_ACOMPLETION, new=AsyncMock(side_effect=Exception("down"))):
            p = _spec_provider("openai")
            status = await p.health_check()
            assert status.healthy is False
            assert status.error is not None


class TestProviderStreamEdgeCases:
    """Test stream() with multi-chunk responses and error mid-stream."""

    @pytest.mark.asyncio
    async def test_groq_stream_multi_chunk(self):
        async def fake_stream():
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="Hello "), finish_reason=None)]
            )
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="world"), finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2, total_tokens=7),
            )

        with patch(_ACOMPLETION, new=AsyncMock(return_value=fake_stream())):
            p = _spec_provider("groq")
            chunks = []
            async for c in p.stream(messages=[Message(role="user", content="hi")]):
                chunks.append(c)
            assert len(chunks) == 2
            assert chunks[0].delta == "Hello "
            assert chunks[1].is_final is True
            assert chunks[1].accumulated_content == "Hello world"

    @pytest.mark.asyncio
    async def test_openai_stream_multi_chunk(self):
        async def fake_stream():
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="A"), finish_reason=None)]
            )
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="B"), finish_reason=None)]
            )
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content=""), finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5),
            )

        with patch(_ACOMPLETION, new=AsyncMock(return_value=fake_stream())):
            p = _spec_provider("openai")
            chunks = []
            async for c in p.stream(messages=[Message(role="user", content="hi")]):
                chunks.append(c)
            assert len(chunks) == 3
            assert chunks[-1].is_final is True

    @pytest.mark.asyncio
    async def test_anthropic_complete_with_mock(self):
        fake = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="claude says hi"), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=10, total_tokens=15),
            model="claude-test",
        )
        with patch(_ACOMPLETION, new=AsyncMock(return_value=fake)):
            p = _spec_provider("anthropic")
            resp = await p.complete(messages=[Message(role="user", content="hi")])
            assert resp.content == "claude says hi"
            assert resp.provider == "anthropic"

    @pytest.mark.asyncio
    async def test_deepseek_complete_with_mock(self):
        fake = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="deepseek ok"), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=8, total_tokens=11),
            model="deepseek-coder",
        )
        with patch(_ACOMPLETION, new=AsyncMock(return_value=fake)):
            p = _spec_provider("deepseek")
            resp = await p.complete(messages=[Message(role="user", content="hi")])
            assert resp.content == "deepseek ok"


class TestSpecProviderCompletions:
    @pytest.mark.asyncio
    async def test_cohere_complete(self):
        fake = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="cohere ok"), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=5, total_tokens=8),
            model="command-r",
        )
        with patch(_ACOMPLETION, new=AsyncMock(return_value=fake)):
            resp = await _spec_provider("cohere").complete(messages=[Message(role="user", content="hi")])
            assert resp.content == "cohere ok"

    @pytest.mark.asyncio
    async def test_mistral_complete(self):
        fake = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="mistral ok"), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=5, total_tokens=8),
            model="mistral-large",
        )
        with patch(_ACOMPLETION, new=AsyncMock(return_value=fake)):
            resp = await _spec_provider("mistral").complete(messages=[Message(role="user", content="hi")])
            assert resp.content == "mistral ok"

    @pytest.mark.asyncio
    async def test_google_complete(self):
        fake = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="gemini ok"), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=5, total_tokens=8),
            model="gemini-pro",
        )
        with patch(_ACOMPLETION, new=AsyncMock(return_value=fake)):
            resp = await _spec_provider("google").complete(messages=[Message(role="user", content="hi")])
            assert resp.content == "gemini ok"

    @pytest.mark.asyncio
    async def test_grok_complete(self):
        fake = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="grok ok"), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=5, total_tokens=8),
            model="grok-1",
        )
        with patch(_ACOMPLETION, new=AsyncMock(return_value=fake)):
            resp = await _spec_provider("grok").complete(messages=[Message(role="user", content="hi")])
            assert resp.content == "grok ok"


def _litellm_resp():
    c = MagicMock(); c.message.content = "pong"; c.finish_reason = "stop"
    u = MagicMock(); u.prompt_tokens = 1; u.completion_tokens = 1; u.total_tokens = 2
    r = MagicMock(); r.choices = [c]; r.usage = u; r.model = "test"
    return r


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(PROVIDER_SPECS))
async def test_provider_health_check_success(name):
    """Every litellm provider returns healthy=True when its probe succeeds."""
    provider = _spec_provider(name)
    client, _ = _fake_http(200, {"data": []})
    with patch(_ASYNC_CLIENT, client), \
            patch("litellm.acompletion", new_callable=AsyncMock, return_value=_litellm_resp()):
        result = await provider.health_check()
    assert isinstance(result, HealthStatus) and result.healthy is True
