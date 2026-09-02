"""Parameterized smoke tests for every LiteLLM-backed cloud provider.

The cloud providers are one ``OpenAICompatibleProvider`` bound to a
``PROVIDER_SPECS`` row, so a single mock-litellm suite covers request shaping,
response unpacking and error mapping for each of them. Adding a provider is
adding a spec row; it is picked up here automatically. Ollama and Triton have
bespoke adapters and their own tests (test_providers_special.py).

We mock at the ``litellm.acompletion`` boundary so no real API calls fire and
no API keys are needed.
"""

from __future__ import annotations

import importlib
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from nvh.providers.base import (
    CompletionResponse,
    FinishReason,
    Message,
    ProviderError,
    StreamChunk,
)
from nvh.providers.openai_compatible import PROVIDER_SPECS, OpenAICompatibleProvider
from nvh.providers.registry import BESPOKE_ADAPTERS, ProviderRegistry, lazy_adapter

PROVIDERS = sorted(PROVIDER_SPECS)

_ACOMPLETION = "nvh.providers.openai_compatible.litellm.acompletion"

# One-release compat shims (removed in 0.43): ``nvh.providers.<name>_provider``.
COMPAT_SHIM_CLASSES = {
    "ai21": "AI21Provider",
    "anthropic": "AnthropicProvider",
    "cerebras": "CerebrasProvider",
    "cohere": "CohereProvider",
    "deepseek": "DeepSeekProvider",
    "fireworks": "FireworksProvider",
    "google": "GoogleProvider",
    "grok": "GrokProvider",
    "groq": "GroqProvider",
    "huggingface": "HuggingFaceProvider",
    "llm7": "LLM7Provider",
    "mistral": "MistralProvider",
    "nvidia": "NvidiaProvider",
    "openai": "OpenAIProvider",
    "openrouter": "OpenRouterProvider",
    "perplexity": "PerplexityProvider",
    "sambanova": "SambaNovProvider",
    "siliconflow": "SiliconFlowProvider",
    "together": "TogetherProvider",
}


def _provider(name: str) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(PROVIDER_SPECS[name])


# ---------------------------------------------------------------------------
# Mock litellm responses
# ---------------------------------------------------------------------------

def _make_completion_response(
    content: str = "mock response",
    model: str = "mock-model",
    finish_reason: str = "stop",
    prompt_tokens: int = 10,
    completion_tokens: int = 20,
):
    """Build a fake litellm completion response."""
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content),
            finish_reason=finish_reason,
        )],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        model=model,
    )


async def _make_stream_iterator(text: str = "hello world"):
    """Build an async iterator that yields one chunk then a final chunk."""
    chunks = [
        SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content=text),
                finish_reason=None,
            )],
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content=""),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(
                prompt_tokens=5,
                completion_tokens=10,
                total_tokens=15,
            ),
        ),
    ]
    for chunk in chunks:
        yield chunk


# ---------------------------------------------------------------------------
# Parameterized tests — one set per provider
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", PROVIDERS)
class TestProviderContract:
    """Verify each provider conforms to the base contract.

    Every provider must:
      - construct from its spec alone
      - expose a `name` property
      - implement complete() returning a CompletionResponse
      - implement stream() yielding StreamChunks
      - implement list_models() returning a non-empty list
      - implement estimate_tokens()
    """

    def test_construct_and_name(self, name):
        provider = _provider(name)
        assert provider.name == name
        assert provider.spec is PROVIDER_SPECS[name]

    def test_estimate_tokens(self, name):
        # Should return an int >= 1 for non-trivial input
        assert _provider(name).estimate_tokens("hello world") >= 1

    @pytest.mark.asyncio
    async def test_list_models(self, name):
        models = await _provider(name).list_models()
        assert isinstance(models, list)
        assert len(models) >= 1, f"{name}.list_models() returned empty"
        assert all(m.provider == name for m in models)
        assert models[0].model_id == PROVIDER_SPECS[name].default_model

    @pytest.mark.asyncio
    async def test_complete_happy_path(self, name):
        """complete() returns a CompletionResponse when litellm succeeds."""
        provider = _provider(name)

        with patch(_ACOMPLETION, new=AsyncMock(return_value=_make_completion_response())):
            resp = await provider.complete(
                messages=[Message(role="user", content="hi")],
                temperature=0.0,
                max_tokens=64,
            )

        assert isinstance(resp, CompletionResponse)
        assert resp.content == "mock response"
        assert resp.provider == name
        assert resp.usage.input_tokens == 10
        assert resp.usage.output_tokens == 20
        assert resp.finish_reason == FinishReason.STOP

    @pytest.mark.asyncio
    async def test_complete_maps_errors(self, name):
        """When litellm raises, the provider must wrap in ProviderError."""
        provider = _provider(name)

        with patch(_ACOMPLETION, new=AsyncMock(side_effect=Exception("upstream is down"))):
            with pytest.raises(ProviderError) as exc_info:
                await provider.complete(
                    messages=[Message(role="user", content="hi")],
                    temperature=0.0,
                    max_tokens=64,
                )

        assert exc_info.value.provider == name

    @pytest.mark.asyncio
    async def test_stream_yields_chunks(self, name):
        """stream() must yield at least one StreamChunk and one final."""
        provider = _provider(name)

        # litellm.acompletion in stream mode returns an async iterator
        with patch(_ACOMPLETION, new=AsyncMock(return_value=_make_stream_iterator("hi there"))):
            chunks: list[StreamChunk] = []
            async for chunk in provider.stream(
                messages=[Message(role="user", content="hi")],
                temperature=0.0,
                max_tokens=64,
            ):
                chunks.append(chunk)

        assert len(chunks) >= 1, f"{name}.stream() yielded no chunks"
        assert any(c.is_final for c in chunks), f"{name}.stream() never set is_final=True"
        # The final chunk should carry usage data and a Decimal cost
        final = next(c for c in chunks if c.is_final)
        assert final.usage is not None
        assert isinstance(final.cost_usd, Decimal)
        if PROVIDER_SPECS[name].zero_cost:
            assert final.cost_usd == Decimal("0")


# ---------------------------------------------------------------------------
# Spec table / registry / catalog sync
# ---------------------------------------------------------------------------
#
# The router resolves capability scores by looking the adapter's model ID up
# in capabilities.yaml, so every shipped default must be a catalog key. mock,
# ollama and triton pick models dynamically and have no spec row.
# ---------------------------------------------------------------------------

def test_spec_table_is_keyed_by_name_and_disjoint_from_bespoke_adapters():
    assert all(spec.name == name for name, spec in PROVIDER_SPECS.items())
    assert not set(PROVIDER_SPECS) & set(BESPOKE_ADAPTERS)


@pytest.mark.parametrize("name", PROVIDERS)
def test_registry_builds_spec_adapters_lazily(name):
    lazy = lazy_adapter(name, api_key="k")
    assert lazy._module_path == "nvh.providers.openai_compatible"
    provider = lazy._load()
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.spec is PROVIDER_SPECS[name]
    assert provider.name == name


@pytest.mark.parametrize("name", PROVIDERS)
def test_default_and_fallback_models_are_catalog_keys(name):
    registry = ProviderRegistry()
    registry.load_capabilities()
    spec = PROVIDER_SPECS[name]
    for model_id in {spec.default_model, spec.fallback_model}:
        info = registry.get_model_info(model_id)
        assert info is not None, f"{name}: '{model_id}' is not a capabilities.yaml key"
        assert info.provider == name, (
            f"{name}: '{model_id}' row is keyed to provider '{info.provider}'"
        )


@pytest.mark.parametrize(
    "name,model,expected",
    [
        ("siliconflow", "Qwen/Qwen2.5-7B-Instruct", "openai/Qwen/Qwen2.5-7B-Instruct"),
        ("siliconflow", "openai/Qwen/Qwen3-8B", "openai/Qwen/Qwen3-8B"),
        ("llm7", "gpt-oss", "openai/gpt-oss"),
        ("nvidia", "meta/llama-3.1-405b-instruct", "nvidia_nim/meta/llama-3.1-405b-instruct"),
        ("nvidia", "nvidia_nim/meta/llama-3.1-8b-instruct", "nvidia_nim/meta/llama-3.1-8b-instruct"),
        ("groq", "groq/openai/gpt-oss-120b", "groq/openai/gpt-oss-120b"),
    ],
    ids=[
        "siliconflow-bare", "siliconflow-prefixed", "llm7-bare",
        "nvidia-bare", "nvidia-prefixed", "groq-untouched",
    ],
)
def test_openai_compatible_endpoints_prefix_model_for_litellm(name, model, expected):
    kw = _provider(name)._kwargs(model)
    assert kw["model"] == expected
    assert kw.get("api_base") == PROVIDER_SPECS[name].base_url


def test_nvidia_defaults_carry_litellm_prefix():
    """Unprefixed 'meta/...' IDs are parsed by litellm as its Llama-API provider."""
    spec = PROVIDER_SPECS["nvidia"]
    assert spec.default_model.startswith(spec.litellm_prefix)
    assert spec.fallback_model.startswith(spec.litellm_prefix)


# ---------------------------------------------------------------------------
# Compat shims (one release)
# ---------------------------------------------------------------------------

def test_every_spec_has_a_compat_shim():
    assert set(COMPAT_SHIM_CLASSES) == set(PROVIDER_SPECS)


@pytest.mark.parametrize("name", PROVIDERS)
def test_compat_shim_binds_its_spec(name):
    module = importlib.import_module(f"nvh.providers.{name}_provider")
    cls = getattr(module, COMPAT_SHIM_CLASSES[name])
    assert issubclass(cls, OpenAICompatibleProvider)
    assert cls().name == name
    provider = cls(api_key="k", default_model="custom-model")
    assert provider.spec is PROVIDER_SPECS[name]
    assert provider._default_model == "custom-model"
    assert provider._fallback_model == PROVIDER_SPECS[name].fallback_model
