"""NVIDIA Triton Inference Server provider adapter via LiteLLM.

Uses the Triton Inference Server HTTP/gRPC endpoint:
  https://github.com/triton-inference-server/server

Triton serves any model loaded on the server (TensorRT-LLM, vLLM, etc.).
LiteLLM supports Triton via the ``triton/`` model prefix.

Default endpoint: http://localhost:8001
Health check:     GET /v2/health/ready
List models:      GET /v2/models

Env vars: TRITON_URL or TRITON_ENDPOINT
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
    """Map LiteLLM/Triton exceptions to our error taxonomy."""
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


def _get_base_url() -> str:
    """Resolve Triton endpoint from env vars or return the default."""
    return (
        os.environ.get("TRITON_URL")
        or os.environ.get("TRITON_ENDPOINT")
        or "http://localhost:8001"
    )


def _calc_cost(model: str, usage: Usage) -> Decimal:
    """Cost calculation — Triton is self-hosted so cost is effectively zero."""
    try:
        cost = litellm.completion_cost(
            model=model,
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
        )
        return Decimal(str(round(cost, 6)))
    except Exception:
        return Decimal("0")


class TritonProvider:
    """NVIDIA Triton Inference Server adapter using LiteLLM with triton/ prefix."""

    def __init__(
        self,
        api_key: str = "",
        default_model: str = "",
        fallback_model: str = "",
        base_url: str | None = None,
        provider_name: str = "triton",
    ):
        self._api_key = api_key or ""
        self._default_model = default_model or ""
        self._fallback_model = fallback_model or self._default_model
        self._base_url = base_url or _get_base_url()
        self._provider_name = provider_name

    @property
    def name(self) -> str:
        return self._provider_name

    def _get_model(self, model: str | None) -> str:
        """Return the model name, prefixed with triton/ if needed."""
        m = model or self._default_model
        if m and not m.startswith("triton/"):
            m = f"triton/{m}"
        return m

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
        """List models available on the Triton server via /v2/models."""
        import httpx

        url = self._base_url.rstrip("/") + "/v2/models"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
                models_list = data.get("models", [])
                return [
                    ModelInfo(
                        model_id=f"triton/{m.get('name', 'unknown')}",
                        provider=self._provider_name,
                    )
                    for m in models_list
                ]
        except Exception:
            # Fallback: return default model if configured
            if self._default_model:
                return [ModelInfo(model_id=self._get_model(None), provider=self._provider_name)]
            return []

    async def health_check(self) -> HealthStatus:
        """Check Triton readiness via GET /v2/health/ready."""
        import httpx

        url = self._base_url.rstrip("/") + "/v2/health/ready"
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                elapsed = int((time.monotonic() - start) * 1000)
                return HealthStatus(
                    provider=self._provider_name,
                    healthy=resp.status_code == 200,
                    latency_ms=elapsed,
                    error=None if resp.status_code == 200 else f"HTTP {resp.status_code}",
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
