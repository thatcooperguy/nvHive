"""LiteLLM adapter shared by every OpenAI-compatible cloud provider.

Per-provider facts live in :mod:`nvh.providers.specs`; this module holds the
behaviour once. The ``nvh.providers.<name>_provider`` modules are one-release
compat shims over :class:`OpenAICompatibleProvider`.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
import litellm

from nvh.providers.base import (
    AuthenticationError,
    CompletionResponse,
    ContentFilterError,
    FinishReason,
    HealthStatus,
    InvalidRequestError,
    Message,
    ModelInfo,
    ModelNotFoundError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    StreamChunk,
    TokenLimitError,
    Usage,
)
from nvh.providers.registry import resolve_provider_key
from nvh.providers.specs import PROVIDER_SPECS, ProviderSpec

__all__ = ["PROVIDER_SPECS", "OpenAICompatibleProvider", "ProviderSpec"]

_HEALTH_TIMEOUT = 15

_FINISH_REASONS = {
    "stop": FinishReason.STOP,
    "length": FinishReason.LENGTH,
    "content_filter": FinishReason.CONTENT_FILTER,
    "tool_calls": FinishReason.TOOL_CALLS,
    "function_call": FinishReason.TOOL_CALLS,
}


def _map_error(e: Exception, provider: str) -> ProviderError:
    """Map LiteLLM/OpenAI exceptions to our error taxonomy with actionable messages."""
    msg = str(e)
    if "AuthenticationError" in type(e).__name__ or "401" in msg:
        from nvh.providers.quota_info import get_quota_info

        info = get_quota_info(provider)
        parts = [
            f"API key invalid or missing permissions for '{provider}'.",
            "  Fix: nvh setup  (reconfigure keys)",
        ]
        if info.upgrade_url:
            parts.append(f"  Get key: {info.upgrade_url}")
        return AuthenticationError(
            "\n".join(parts),
            provider=provider,
            original_error=e,
        )
    if "RateLimitError" in type(e).__name__ or "429" in msg:
        from nvh.providers.quota_info import format_rate_limit_message, parse_retry_after

        retry_after = getattr(e, "retry_after", None) or parse_retry_after(msg)
        friendly = format_rate_limit_message(provider, msg)
        return RateLimitError(
            friendly,
            provider=provider,
            retry_after=retry_after,
            original_error=e,
        )
    if "InvalidRequestError" in type(e).__name__ or "400" in msg:
        if "context_length" in msg.lower() or "max_tokens" in msg.lower():
            return TokenLimitError(
                f"Input too long for '{provider}' model.\n"
                f"Try: nvh config set defaults.max_tokens 2048\n"
                f"Or use a model with a larger context window.\n"
                f"Original error: {msg}",
                provider=provider,
                original_error=e,
            )
        if "content_filter" in msg.lower() or "content_policy" in msg.lower():
            return ContentFilterError(msg, provider=provider, original_error=e)
        return InvalidRequestError(msg, provider=provider, original_error=e)
    if "NotFoundError" in type(e).__name__ or "404" in msg:
        return ModelNotFoundError(
            f"Model not found on '{provider}'.\n"
            f"Run: nvh model list  (to see available models)\n"
            f"Or update your model: nvh config set providers.{provider}.default_model <model>\n"
            f"Original error: {msg}",
            provider=provider,
            original_error=e,
        )
    if "ServiceUnavailableError" in type(e).__name__ or "503" in msg or "500" in msg:
        return ProviderUnavailableError(
            f"'{provider}' is temporarily unavailable (server error).\n"
            f"NVHive will try the next advisor automatically.\n"
            f"Check status: https://status.openai.com  (or provider status page)\n"
            f"Original error: {msg}",
            provider=provider,
            original_error=e,
        )
    return ProviderError(msg, provider=provider, original_error=e)


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
    """Calculate cost using LiteLLM's cost tracking."""
    try:
        prompt_cost, completion_cost = litellm.cost_per_token(
            model=model,
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
        )
        return Decimal(str(round(prompt_cost + completion_cost, 6)))
    except Exception:
        return Decimal("0")


def _resolve_api_key(spec: ProviderSpec, api_key: str, provider_name: str) -> str:
    # An unexpanded "${VAR}" placeholder means the registry found no env value.
    if api_key and not api_key.startswith("${"):
        return api_key
    return resolve_provider_key(provider_name, ptype=spec.name)[0] or ""


def _litellm_api_base(model: str) -> str | None:
    """The endpoint LiteLLM routes ``model`` to, when it publishes one."""
    try:
        return litellm.get_llm_provider(model)[3]
    except Exception:
        return None


def _model_count(resp: httpx.Response) -> int:
    try:
        data = resp.json()
    except ValueError:
        return 0
    if isinstance(data, dict):
        data = data.get("data", [])
    return len(data) if isinstance(data, list) else 0


class OpenAICompatibleProvider:
    """LiteLLM adapter parameterised by a :class:`ProviderSpec`.

    Blank ``default_model``/``fallback_model``/``base_url``/``provider_name``/
    ``timeout`` fall back to the spec, so a config stanza that omits them gets
    the shipped defaults.
    """

    def __init__(
        self,
        spec: ProviderSpec,
        api_key: str = "",
        default_model: str = "",
        fallback_model: str = "",
        base_url: str | None = None,
        provider_name: str = "",
        timeout: int | None = None,
    ):
        self._spec = spec
        self._provider_name = provider_name or spec.name
        self._api_key = _resolve_api_key(spec, api_key, self._provider_name)
        self._default_model = default_model or spec.default_model
        self._fallback_model = fallback_model or spec.fallback_model
        self._health_model = spec.health_model or self._default_model
        self._base_url = base_url or spec.base_url
        self._timeout = timeout or spec.timeout

    @property
    def name(self) -> str:
        return self._provider_name

    @property
    def spec(self) -> ProviderSpec:
        return self._spec

    def _get_model(self, model: str | None) -> str:
        return self._spec.route(model or self._default_model)

    def _kwargs(self, model: str) -> dict[str, Any]:
        kw: dict[str, Any] = {"model": self._spec.route(model)}
        if self._api_key:
            kw["api_key"] = self._api_key
        if self._base_url:
            kw["api_base"] = self._base_url
        return kw

    def _cost(self, model: str, usage: Usage) -> Decimal:
        return Decimal("0") if self._spec.zero_cost else _calc_cost(model, usage)

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

        return CompletionResponse(
            content=content,
            model=response.model or model_name,
            provider=self._provider_name,
            usage=usage,
            cost_usd=self._cost(model_name, usage),
            latency_ms=elapsed,
            finish_reason=_FINISH_REASONS.get(finish, FinishReason.STOP),
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
                timeout=self._timeout,
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
                    finish_reason = _FINISH_REASONS.get(fr, FinishReason.STOP)

                accumulated += delta
                is_final = finish_reason is not None

                usage = None
                cost = None
                if is_final:
                    # Estimate usage for streaming (not all providers send it)
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
                    cost = self._cost(model_name, usage)

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
        model_ids = dict.fromkeys((self._default_model, self._fallback_model))
        return [
            ModelInfo(model_id=model_id, provider=self._provider_name)
            for model_id in model_ids
            if model_id
        ]

    def _models_url(self) -> str | None:
        """The free ``GET /models`` endpoint, when one is known and we hold a key."""
        base = self._base_url or _litellm_api_base(self._get_model(None))
        if not base or not self._api_key:
            return None
        return f"{base.rstrip('/')}/models"

    def _health(
        self, start: float, healthy: bool, error: str = "", models: int = 0
    ) -> HealthStatus:
        return HealthStatus(
            provider=self._provider_name,
            healthy=healthy,
            latency_ms=int((time.monotonic() - start) * 1000),
            error=error[:200] or None,
            models_available=models,
        )

    async def health_check(self) -> HealthStatus:
        """Free ``GET /models`` when the endpoint is known; otherwise a one-token ping."""
        start = time.monotonic()
        models_url = self._models_url()
        if models_url:
            try:
                async with httpx.AsyncClient(timeout=_HEALTH_TIMEOUT) as client:
                    resp = await client.get(
                        models_url,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                    )
            except Exception as e:
                return self._health(start, False, str(e))
            if resp.status_code < 400:
                return self._health(start, True, models=_model_count(resp))
            if resp.status_code not in (404, 405):
                return self._health(start, False, f"HTTP {resp.status_code} from {models_url}")
            # No /models on this host: fall through to the billed ping.
        try:
            await litellm.acompletion(
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                timeout=_HEALTH_TIMEOUT,
                **self._kwargs(self._health_model),
            )
        except Exception as e:
            return self._health(start, False, str(e))
        return self._health(start, True)

    def estimate_tokens(self, text: str) -> int:
        try:
            import tiktoken

            enc = tiktoken.encoding_for_model(self._default_model)
            return len(enc.encode(text))
        except Exception:
            return len(text) // 4
