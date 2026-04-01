"""Perplexity provider adapter via LiteLLM.

Perplexity is known for search-augmented responses with real-time citations.
LiteLLM prefix: perplexity/
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import litellm

from nvh.providers.base import (
    CompletionResponse,
    FinishReason,
    HealthStatus,
    Message,
    ModelInfo,
    StreamChunk,
    Usage,
)
from nvh.providers.openai_provider import _build_messages, _calc_cost, _map_error


class PerplexityProvider:
    """Perplexity adapter using LiteLLM.

    Perplexity offers search-augmented LLMs that return responses with
    real-time web citations. Set PERPLEXITY_API_KEY to authenticate.
    """

    def __init__(
        self,
        api_key: str = "",
        default_model: str = "perplexity/llama-3.1-sonar-large-128k-online",
        fallback_model: str = "perplexity/llama-3.1-sonar-small-128k-online",
        base_url: str | None = None,
        provider_name: str = "perplexity",
    ):
        self._api_key = api_key
        self._default_model = default_model
        self._fallback_model = fallback_model
        self._base_url = base_url
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
        if self._base_url:
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
                **self._kwargs(self._default_model),
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
