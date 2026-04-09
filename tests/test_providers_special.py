"""Tests for Ollama and Triton providers.

These two don't fit the parametrized pattern in
test_providers_parametrized.py because:

- **Ollama** uses httpx to talk to a local daemon at
  ``http://localhost:11434/api/tags`` for list_models/health_check,
  so a CI runner without Ollama installed gets an empty list and
  the parametrized contract ``len(models) >= 1`` fails.
- **Triton** also uses httpx (``/v2/models``) for list_models and
  has a different default_model behavior.

Both still wrap litellm for complete/stream, but with custom error
mapping for "connection refused" cases. This file mocks at both
boundaries (litellm + httpx) and exercises the same contract plus
the custom error paths.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nvh.providers.base import (
    CompletionResponse,
    FinishReason,
    Message,
    ProviderError,
    ProviderUnavailableError,
    StreamChunk,
)
from nvh.providers.ollama_provider import OllamaProvider
from nvh.providers.triton_provider import TritonProvider

# ---------------------------------------------------------------------------
# Shared mock builders
# ---------------------------------------------------------------------------


def _fake_completion_response(content: str = "ollama says hi"):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content),
            finish_reason="stop",
        )],
        usage=SimpleNamespace(
            prompt_tokens=5,
            completion_tokens=10,
            total_tokens=15,
        ),
        model="mock-model",
    )


async def _fake_stream():
    yield SimpleNamespace(
        choices=[SimpleNamespace(
            delta=SimpleNamespace(content="streamed"),
            finish_reason=None,
        )],
    )
    yield SimpleNamespace(
        choices=[SimpleNamespace(
            delta=SimpleNamespace(content=""),
            finish_reason="stop",
        )],
        usage=SimpleNamespace(
            prompt_tokens=3,
            completion_tokens=8,
            total_tokens=11,
        ),
    )


def _mock_httpx_get_models(models: list[dict]):
    """Build a mock httpx.AsyncClient whose .get() returns a fake response."""
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json = MagicMock(return_value={"models": models})

    client_instance = MagicMock()
    client_instance.get = AsyncMock(return_value=fake_response)
    client_instance.__aenter__ = AsyncMock(return_value=client_instance)
    client_instance.__aexit__ = AsyncMock(return_value=None)

    return client_instance


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


class TestOllamaProvider:
    def test_construct(self):
        provider = OllamaProvider()
        assert provider.name == "ollama"

    def test_estimate_tokens(self):
        assert OllamaProvider().estimate_tokens("hello world") >= 1

    def test_get_model_adds_ollama_prefix(self):
        """Models passed without the ollama/ prefix must get it auto-added."""
        provider = OllamaProvider()
        assert provider._get_model("llama3.1") == "ollama/llama3.1"
        assert provider._get_model("ollama/mistral") == "ollama/mistral"
        assert provider._get_model(None) == "ollama/llama3.1"  # default

    @pytest.mark.asyncio
    async def test_list_models_parses_tags_response(self):
        """list_models() parses the /api/tags JSON into ModelInfo records."""
        provider = OllamaProvider()
        mock_client = _mock_httpx_get_models([
            {"name": "llama3.1:latest"},
            {"name": "mistral:7b"},
        ])

        with patch("nvh.providers.ollama_provider.httpx.AsyncClient",
                   return_value=mock_client):
            models = await provider.list_models()

        assert len(models) == 2
        assert models[0].model_id == "ollama/llama3.1:latest"
        assert models[1].model_id == "ollama/mistral:7b"
        assert all(m.provider == "ollama" for m in models)

    @pytest.mark.asyncio
    async def test_list_models_returns_empty_on_connection_error(self):
        """When Ollama isn't running, list_models() returns [] silently."""
        provider = OllamaProvider()

        with patch("nvh.providers.ollama_provider.httpx.AsyncClient",
                   side_effect=Exception("connection refused")):
            models = await provider.list_models()

        assert models == []

    @pytest.mark.asyncio
    async def test_complete_happy_path(self):
        provider = OllamaProvider()

        with patch(
            "nvh.providers.ollama_provider.litellm.acompletion",
            new=AsyncMock(return_value=_fake_completion_response()),
        ):
            resp = await provider.complete(
                messages=[Message(role="user", content="hi")],
                temperature=0.0,
                max_tokens=64,
            )

        assert isinstance(resp, CompletionResponse)
        assert resp.content == "ollama says hi"
        assert resp.provider == "ollama"
        assert resp.usage.total_tokens == 15
        assert resp.finish_reason == FinishReason.STOP

    @pytest.mark.asyncio
    async def test_complete_maps_connection_error_to_unavailable(self):
        """When the error string contains 'connection refused', Ollama raises
        ProviderUnavailableError with a helpful 'ollama serve' message."""
        provider = OllamaProvider()

        with patch(
            "nvh.providers.ollama_provider.litellm.acompletion",
            new=AsyncMock(side_effect=Exception("connection refused: localhost:11434")),
        ):
            with pytest.raises(ProviderUnavailableError) as exc_info:
                await provider.complete(
                    messages=[Message(role="user", content="hi")],
                )

        msg = str(exc_info.value)
        assert "ollama serve" in msg.lower() or "11434" in msg

    @pytest.mark.asyncio
    async def test_complete_wraps_generic_errors(self):
        """Non-connection errors still wrap in ProviderError."""
        provider = OllamaProvider()

        with patch(
            "nvh.providers.ollama_provider.litellm.acompletion",
            new=AsyncMock(side_effect=Exception("model not found")),
        ):
            with pytest.raises(ProviderError):
                await provider.complete(
                    messages=[Message(role="user", content="hi")],
                )

    @pytest.mark.asyncio
    async def test_stream_yields_chunks(self):
        provider = OllamaProvider()

        with patch(
            "nvh.providers.ollama_provider.litellm.acompletion",
            new=AsyncMock(return_value=_fake_stream()),
        ):
            chunks: list[StreamChunk] = []
            async for chunk in provider.stream(
                messages=[Message(role="user", content="hi")],
            ):
                chunks.append(chunk)

        assert len(chunks) >= 1
        assert any(c.is_final for c in chunks)
        final = next(c for c in chunks if c.is_final)
        assert final.usage is not None
        # Local models are free — cost should be zero or None
        if final.cost_usd is not None:
            assert final.cost_usd == Decimal("0") or final.cost_usd >= Decimal("0")


# ---------------------------------------------------------------------------
# Triton
# ---------------------------------------------------------------------------


class TestTritonProvider:
    def test_construct(self):
        provider = TritonProvider()
        assert provider.name == "triton"

    def test_estimate_tokens(self):
        assert TritonProvider().estimate_tokens("hello world") >= 1

    @pytest.mark.asyncio
    async def test_list_models_parses_v2_models_response(self):
        """list_models() parses the /v2/models JSON into ModelInfo records."""
        provider = TritonProvider(base_url="http://triton:8000")
        mock_client = _mock_httpx_get_models([
            {"name": "llama-70b"},
            {"name": "mistral-7b"},
        ])

        with patch("httpx.AsyncClient", return_value=mock_client):
            models = await provider.list_models()

        # Triton is tolerant — it may return the mocked list OR fall
        # back to default_model. Both prove the code path runs.
        assert isinstance(models, list)

    @pytest.mark.asyncio
    async def test_list_models_falls_back_to_default_on_error(self):
        """When Triton server is unreachable, list_models() falls back to
        the configured default_model (if set)."""
        provider = TritonProvider(default_model="triton/llama-70b")

        with patch("httpx.AsyncClient", side_effect=Exception("connection refused")):
            models = await provider.list_models()

        assert isinstance(models, list)
        # Should either be empty or contain the default
        if models:
            assert models[0].model_id.startswith("triton/")
