"""NVIDIA NIM provider adapter via LiteLLM.

Uses the OpenAI-compatible NVIDIA inference API:
  https://integrate.api.nvidia.com/v1

Free tier: 1000+ API credits on signup (NVIDIA Developer Program).
Supports 100+ models including Llama 405B, domain-specific models,
and models optimized for NVIDIA hardware.

Default model : meta/llama-3.1-70b-instruct
Fallback model: meta/llama-3.1-8b-instruct

Env vars: NVIDIA_API_KEY or NIM_API_KEY
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import litellm

from nvh.providers.base import (
    CompletionResponse,
    FinishReason,
    HealthStatus,
    Message,
    ModelInfo,
    ProviderError,
    StreamChunk,
    Usage,
)


def _map_error(e: Exception, provider: str) -> ProviderError:
    """Map LiteLLM/NVIDIA exceptions to our error taxonomy with actionable messages."""
    from nvh.providers.openai_provider import _map_error as _openai_map_error
    return _openai_map_error(e, provider)


def _build_messages(
    messages: list[Message],
    system_prompt: str | None = None,
) -> list[dict[str, Any]]:
    """Convert our Message models to LiteLLM format."""
    result: list[dict[str, Any]] = []
    if system_prompt:
        result.append({"role": "system", "content": system_prompt})
    for msg in messages:
        d: dict[str, Any] = {"role": msg.role, "content": msg.content}
        if msg.name:
            d["name"] = msg.name
        result.append(d)
    return result


def _calc_cost(model: str, usage: Usage) -> Decimal:
    """Cost calculation — credits are consumed but tracked separately by NVIDIA."""
    try:
        cost = litellm.completion_cost(
            model=model,
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
        )
        return Decimal(str(round(cost, 6)))
    except Exception:
        return Decimal("0")


class NvidiaProvider:
    """NVIDIA NIM adapter using LiteLLM with OpenAI-compatible API."""

    BASE_URL = "https://integrate.api.nvidia.com/v1"

    def __init__(
        self,
        api_key: str = "",
        default_model: str = "meta/llama-3.1-70b-instruct",
        fallback_model: str = "meta/llama-3.1-8b-instruct",
        base_url: str | None = None,
        provider_name: str = "nvidia",
    ):
        # Resolve API key from env if not provided
        self._api_key = (
            api_key
            or os.environ.get("NVIDIA_API_KEY")
            or os.environ.get("NIM_API_KEY")
            or os.environ.get("HIVE_NVIDIA_API_KEY")
            or ""
        )
        self._default_model = default_model
        self._fallback_model = fallback_model
        self._base_url = base_url or self.BASE_URL
        self._provider_name = provider_name

    @property
    def name(self) -> str:
        return self._provider_name

    def _get_model(self, model: str | None) -> str:
        return model or self._default_model

    def _kwargs(self, model: str) -> dict[str, Any]:
        kw: dict[str, Any] = {"model": model}
        if self._api_key:
            kw["api_key"] = self._api_key
        kw["api_base"] = self._base_url
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
                **self._kwargs(model_name),
                **kwargs,
            )
        except Exception as e:
            raise _map_error(e, self._provider_name) from e

        elapsed = int((time.monotonic() - start) * 1000)
        usage_data = response.usage
        usage = Usage(
            input_tokens=usage_data.prompt_tokens or 0,
            output_tokens=usage_data.completion_tokens or 0,
            total_tokens=usage_data.total_tokens or 0,
        )
        content = response.choices[0].message.content or ""
        finish = response.choices[0].finish_reason or "stop"
        finish_map = {
            "stop": FinishReason.STOP,
            "length": FinishReason.LENGTH,
            "content_filter": FinishReason.CONTENT_FILTER,
            "tool_calls": FinishReason.TOOL_CALLS,
            "function_call": FinishReason.TOOL_CALLS,
        }

        return CompletionResponse(
            content=content,
            model=response.model or model_name,
            provider=self._provider_name,
            usage=usage,
            cost_usd=_calc_cost(model_name, usage),
            latency_ms=elapsed,
            finish_reason=finish_map.get(finish, FinishReason.STOP),
        )

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
                **self._kwargs(model_name),
                **kwargs,
            )
        except Exception as e:
            raise _map_error(e, self._provider_name) from e

        try:
            async for chunk in response:
                delta = ""
                finish_reason = None

                if chunk.choices and chunk.choices[0].delta:
                    delta = chunk.choices[0].delta.content or ""
                if chunk.choices and chunk.choices[0].finish_reason:
                    fr = chunk.choices[0].finish_reason
                    finish_map = {
                        "stop": FinishReason.STOP,
                        "length": FinishReason.LENGTH,
                        "content_filter": FinishReason.CONTENT_FILTER,
                    }
                    finish_reason = finish_map.get(fr, FinishReason.STOP)

                accumulated += delta
                is_final = finish_reason is not None

                usage = None
                cost = None
                if is_final:
                    usage_data = getattr(chunk, "usage", None)
                    if usage_data:
                        usage = Usage(
                            input_tokens=getattr(usage_data, "prompt_tokens", 0) or 0,
                            output_tokens=getattr(usage_data, "completion_tokens", 0) or 0,
                            total_tokens=getattr(usage_data, "total_tokens", 0) or 0,
                        )
                    else:
                        est_out = self.estimate_tokens(accumulated)
                        usage = Usage(
                            input_tokens=0,
                            output_tokens=est_out,
                            total_tokens=est_out,
                        )
                    cost = _calc_cost(model_name, usage)

                yield StreamChunk(
                    delta=delta,
                    is_final=is_final,
                    accumulated_content=accumulated,
                    model=model_name,
                    provider=self._provider_name,
                    usage=usage,
                    cost_usd=cost,
                    finish_reason=finish_reason,
                )
        except Exception as e:
            raise _map_error(e, self._provider_name) from e

    async def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(model_id=self._default_model, provider=self._provider_name),
            ModelInfo(model_id=self._fallback_model, provider=self._provider_name),
        ]

    async def health_check(self) -> HealthStatus:
        start = time.monotonic()
        try:
            await litellm.acompletion(
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                **self._kwargs(self._fallback_model),
            )
            elapsed = int((time.monotonic() - start) * 1000)
            return HealthStatus(
                provider=self._provider_name,
                healthy=True,
                latency_ms=elapsed,
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
