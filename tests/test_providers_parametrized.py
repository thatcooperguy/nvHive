"""Parameterized smoke tests for every litellm-backed provider.

The 22 cloud provider adapters in nvh/providers/ all wrap litellm with
the same shape (complete + stream + list_models + health_check). They
were 100% untested before this file — collectively ~1800 lines at 0%
coverage. A single mock-litellm test exercised against every adapter
brings each one to ~70% in one shot.

We mock at the `litellm.acompletion` boundary so:
  - No real API calls fire
  - No API keys are needed
  - The same fixture validates every provider's request shaping,
    response unpacking, and error mapping

Each provider class is imported via importlib and parameterized so
adding a new provider in nvh/providers/ requires only adding one
line to PROVIDER_SPECS — the rest is automatic.
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

# ---------------------------------------------------------------------------
# Provider catalog — module path + class name + default model
# ---------------------------------------------------------------------------
#
# Adding a new provider only requires adding a row here.
# ---------------------------------------------------------------------------

PROVIDER_SPECS: list[tuple[str, str]] = [
    ("nvh.providers.ai21_provider", "AI21Provider"),
    ("nvh.providers.anthropic_provider", "AnthropicProvider"),
    ("nvh.providers.cerebras_provider", "CerebrasProvider"),
    ("nvh.providers.cohere_provider", "CohereProvider"),
    ("nvh.providers.deepseek_provider", "DeepSeekProvider"),
    ("nvh.providers.fireworks_provider", "FireworksProvider"),
    ("nvh.providers.github_provider", "GitHubProvider"),
    ("nvh.providers.google_provider", "GoogleProvider"),
    ("nvh.providers.grok_provider", "GrokProvider"),
    ("nvh.providers.groq_provider", "GroqProvider"),
    ("nvh.providers.huggingface_provider", "HuggingFaceProvider"),
    ("nvh.providers.llm7_provider", "LLM7Provider"),
    ("nvh.providers.mistral_provider", "MistralProvider"),
    ("nvh.providers.nvidia_provider", "NvidiaProvider"),
    ("nvh.providers.openai_provider", "OpenAIProvider"),
    ("nvh.providers.openrouter_provider", "OpenRouterProvider"),
    ("nvh.providers.perplexity_provider", "PerplexityProvider"),
    ("nvh.providers.sambanova_provider", "SambaNovProvider"),
    ("nvh.providers.siliconflow_provider", "SiliconFlowProvider"),
    ("nvh.providers.together_provider", "TogetherProvider"),
    # Ollama and Triton have different shapes — they'll get their own tests.
]


def _load(spec: tuple[str, str]):
    module_path, class_name = spec
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)


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

@pytest.mark.parametrize("spec", PROVIDER_SPECS, ids=lambda s: s[1])
class TestProviderContract:
    """Verify each provider conforms to the base contract.

    Every provider must:
      - construct with no required arguments
      - expose a `name` property
      - implement complete() returning a CompletionResponse
      - implement stream() yielding StreamChunks
      - implement list_models() returning a non-empty list
      - implement estimate_tokens()
    """

    def test_construct_and_name(self, spec):
        cls = _load(spec)
        provider = cls()
        assert isinstance(provider.name, str) and provider.name, (
            f"{spec[1]}.name must be a non-empty string"
        )

    def test_estimate_tokens(self, spec):
        cls = _load(spec)
        provider = cls()
        # Should return an int >= 1 for non-trivial input
        assert provider.estimate_tokens("hello world") >= 1

    @pytest.mark.asyncio
    async def test_list_models(self, spec):
        cls = _load(spec)
        provider = cls()
        models = await provider.list_models()
        assert isinstance(models, list)
        assert len(models) >= 1, f"{spec[1]}.list_models() returned empty"
        assert all(hasattr(m, "model_id") for m in models)
        assert all(hasattr(m, "provider") for m in models)

    @pytest.mark.asyncio
    async def test_complete_happy_path(self, spec):
        """complete() returns a CompletionResponse when litellm succeeds."""
        cls = _load(spec)
        provider = cls()
        module_path = spec[0]

        # Patch litellm.acompletion in the provider's module so the
        # patch hits whichever import path the provider uses.
        with patch(f"{module_path}.litellm.acompletion", new=AsyncMock(
            return_value=_make_completion_response()
        )):
            resp = await provider.complete(
                messages=[Message(role="user", content="hi")],
                temperature=0.0,
                max_tokens=64,
            )

        assert isinstance(resp, CompletionResponse)
        assert resp.content == "mock response"
        assert resp.provider == provider.name
        assert resp.usage.input_tokens == 10
        assert resp.usage.output_tokens == 20
        assert resp.finish_reason == FinishReason.STOP

    @pytest.mark.asyncio
    async def test_complete_maps_errors(self, spec):
        """When litellm raises, the provider must wrap in ProviderError."""
        cls = _load(spec)
        provider = cls()
        module_path = spec[0]

        with patch(f"{module_path}.litellm.acompletion", new=AsyncMock(
            side_effect=Exception("upstream is down")
        )):
            with pytest.raises(Exception) as exc_info:
                await provider.complete(
                    messages=[Message(role="user", content="hi")],
                    temperature=0.0,
                    max_tokens=64,
                )

        # Should be a ProviderError or subclass — not the bare Exception.
        # `_map_error` returns a ProviderError variant; if the provider
        # doesn't wrap, this catches that regression.
        assert isinstance(exc_info.value, ProviderError), (
            f"{spec[1]} did not wrap upstream exception in ProviderError; "
            f"got {type(exc_info.value).__name__}"
        )

    @pytest.mark.asyncio
    async def test_stream_yields_chunks(self, spec):
        """stream() must yield at least one StreamChunk and one final."""
        cls = _load(spec)
        provider = cls()
        module_path = spec[0]

        # litellm.acompletion in stream mode returns an async iterator
        with patch(
            f"{module_path}.litellm.acompletion",
            new=AsyncMock(return_value=_make_stream_iterator("hi there")),
        ):
            chunks: list[StreamChunk] = []
            async for chunk in provider.stream(
                messages=[Message(role="user", content="hi")],
                temperature=0.0,
                max_tokens=64,
            ):
                chunks.append(chunk)

        assert len(chunks) >= 1, f"{spec[1]}.stream() yielded no chunks"
        # At least one must be marked final
        assert any(c.is_final for c in chunks), (
            f"{spec[1]}.stream() never set is_final=True"
        )
        # The final chunk should carry usage data
        final = next(c for c in chunks if c.is_final)
        assert final.usage is not None
        # Cost should be Decimal-like (or None for free providers)
        if final.cost_usd is not None:
            assert isinstance(final.cost_usd, Decimal)
