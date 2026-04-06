"""Ollama (local) provider adapter via LiteLLM."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
import litellm

from nvh.providers.base import (
    CompletionResponse,
    FinishReason,
    HealthStatus,
    Message,
    ModelInfo,
    ProviderUnavailableError,  # noqa: F401 — also used directly for connection errors
    StreamChunk,
    Usage,
)
from nvh.providers.openai_provider import _build_messages, _map_error


class OllamaProvider:
    """Ollama local model adapter using LiteLLM."""

    def __init__(
        self,
        api_key: str = "",
        default_model: str = "ollama/llama3.1",
        fallback_model: str = "",
        base_url: str | None = None,
        provider_name: str = "ollama",
        timeout: int = 300,
    ):
        self._default_model = default_model
        self._fallback_model = fallback_model
        self._base_url = base_url or "http://localhost:11434"
        self._provider_name = provider_name
        self._timeout = timeout

    @property
    def name(self) -> str:
        return self._provider_name

    def _get_model(self, model: str | None) -> str:
        m = model or self._default_model
        # LiteLLM requires the ollama/ prefix for routing
        if m and not m.startswith("ollama/"):
            m = f"ollama/{m}"
        return m

    def _kwargs(self, model: str) -> dict[str, Any]:
        kw: dict[str, Any] = {"model": model, "api_base": self._base_url}
        return kw

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 1.0,
        max_tokens: int = 4096,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> CompletionResponse:
        model_name = self._get_model(model)
        msgs = _build_messages(messages, system_prompt)
        start = time.monotonic()

        try:
            response = await litellm.acompletion(
                messages=msgs,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=self._timeout,
                **self._kwargs(model_name),
                **kwargs,
            )
        except Exception as e:
            err_str = str(e).lower()
            if "connection" in err_str or "refused" in err_str or "connect" in err_str:
                raise ProviderUnavailableError(
                    f"Ollama is not running at {self._base_url}.\n"
                    f"Start with:   ollama serve\n"
                    f"Install:      curl -fsSL https://ollama.com/install.sh | sh\n"
                    f"Pull a model: ollama pull llama3.1",
                    provider=self._provider_name,
                    original_error=e,
                ) from e
            raise _map_error(e, self._provider_name) from e

        elapsed = int((time.monotonic() - start) * 1000)
        usage_data = response.usage
        usage = Usage(
            input_tokens=getattr(usage_data, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage_data, "completion_tokens", 0) or 0,
            total_tokens=getattr(usage_data, "total_tokens", 0) or 0,
        )
        content = response.choices[0].message.content or ""

        # Fallback: some models (e.g. Gemma 4) return empty
        # content through LiteLLM. Call Ollama API directly.
        if not content and usage.output_tokens > 0:
            try:
                content = await self._direct_complete(
                    msgs, model_name, temperature, max_tokens,
                )
            except Exception:
                pass  # keep empty, don't crash

        return CompletionResponse(
            content=content,
            model=response.model or model_name,
            provider=self._provider_name,
            usage=usage,
            cost_usd=Decimal("0"),  # Local models are free
            latency_ms=elapsed,
            finish_reason=FinishReason.STOP,
        )

    async def _direct_complete(
        self,
        messages: list[dict],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call Ollama API directly, bypassing LiteLLM.

        Fallback for models where LiteLLM returns empty content
        (e.g. Gemma 4 with code/structured responses).
        """
        import httpx

        # Strip ollama/ prefix for direct API call
        raw_model = model.removeprefix("ollama/")

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/api/chat",
                json={
                    "model": raw_model,
                    "messages": messages,
                    "stream": False,
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_tokens,
                    },
                },
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("message", {}).get("content", "")

    async def stream(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 1.0,
        max_tokens: int = 4096,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        model_name = self._get_model(model)
        msgs = _build_messages(messages, system_prompt)
        accumulated = ""

        try:
            response = await litellm.acompletion(
                messages=msgs,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                timeout=self._timeout,
                **self._kwargs(model_name),
                **kwargs,
            )
        except Exception as e:
            err_str = str(e).lower()
            if "connection" in err_str or "refused" in err_str or "connect" in err_str:
                raise ProviderUnavailableError(
                    f"Ollama is not running at {self._base_url}.\n"
                    f"Start with:   ollama serve\n"
                    f"Install:      curl -fsSL https://ollama.com/install.sh | sh\n"
                    f"Pull a model: ollama pull llama3.1",
                    provider=self._provider_name,
                    original_error=e,
                ) from e
            raise _map_error(e, self._provider_name) from e

        try:
            async for chunk in response:
                delta = ""
                finish_reason = None

                if chunk.choices and chunk.choices[0].delta:
                    delta = chunk.choices[0].delta.content or ""
                if chunk.choices and chunk.choices[0].finish_reason:
                    finish_reason = FinishReason.STOP

                accumulated += delta
                is_final = finish_reason is not None

                usage = None
                if is_final:
                    est = self.estimate_tokens(accumulated)
                    usage = Usage(output_tokens=est, total_tokens=est)

                yield StreamChunk(
                    delta=delta,
                    is_final=is_final,
                    accumulated_content=accumulated,
                    model=model_name,
                    provider=self._provider_name,
                    usage=usage,
                    cost_usd=Decimal("0") if is_final else None,
                    finish_reason=finish_reason,
                )
        except Exception as e:
            raise _map_error(e, self._provider_name) from e

    async def list_models(self) -> list[ModelInfo]:
        """Discover models from the Ollama API."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self._base_url}/api/tags", timeout=5.0)
                resp.raise_for_status()
                data = resp.json()
                models = []
                for m in data.get("models", []):
                    name = m.get("name", "")
                    models.append(ModelInfo(
                        model_id=f"ollama/{name}",
                        provider=self._provider_name,
                        display_name=name,
                    ))
                return models
        except Exception:
            return []

    async def health_check(self) -> HealthStatus:
        """Check if Ollama is running by hitting /api/tags."""
        start = time.monotonic()
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self._base_url}/api/tags", timeout=5.0)
                resp.raise_for_status()
                data = resp.json()
                elapsed = int((time.monotonic() - start) * 1000)
                model_count = len(data.get("models", []))
                return HealthStatus(
                    provider=self._provider_name,
                    healthy=True,
                    latency_ms=elapsed,
                    models_available=model_count,
                )
        except Exception as e:
            elapsed = int((time.monotonic() - start) * 1000)
            return HealthStatus(
                provider=self._provider_name,
                healthy=False,
                latency_ms=elapsed,
                error=str(e)[:200],
            )

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4
