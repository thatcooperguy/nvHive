"""Ollama (local) provider adapter via LiteLLM."""

from __future__ import annotations

import time
import json
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

_AUTO_MODEL_CHOICES = {
    "auto",
    "auto-pick",
    "auto pick",
    "auto-pick best available",
    "recommended",
    "recommended model",
    "best available",
    "default",
    "none",
}

_FALLBACK_MODEL_PREFERENCE = (
    "llama3.1:8b",
    "llama3.1",
    "gemma3:4b",
    "qwen3:8b",
    "qwen3-8b",
    "nemotron-mini",
)


def _rootless_ollama_unavailable_message(base_url: str) -> str:
    return (
        f"Ollama is not responding at {base_url}. "
        "Open nvWizard Setup and press Install Runtime or Fix My Setup. "
        "nvHive repairs the rootless Ollama runtime under NVH_HOME; no sudo, apt, "
        "or system install should be needed. Advanced override: nvh studio --install rootless-ollama -y"
    )


def _ollama_daemon_reachable(base_url: str) -> bool:
    """Return True iff Ollama responds to /api/tags within 2s.

    We use this as a ground-truth check BEFORE raising "Ollama is not
    running" based on an error message substring. Many litellm errors
    contain substrings like "connect", "connection", or "HTTPConnectionPool"
    even when the daemon is up and the real issue is model-not-found,
    timeout, or auth. Actually probing the daemon eliminates false
    positives that confuse users.
    """
    try:
        resp = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=2)
        return resp.status_code == 200
    except Exception:
        return False


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
        m = (model or "").strip()
        if not m or m.lower() in _AUTO_MODEL_CHOICES:
            m = self._default_model
        # LiteLLM requires the ollama/ prefix for routing
        if m and not m.startswith("ollama/"):
            m = f"ollama/{m}"
        return m

    def _kwargs(self, model: str) -> dict[str, Any]:
        kw: dict[str, Any] = {"model": model, "api_base": self._base_url}
        return kw

    @staticmethod
    def _looks_like_missing_model(exc: Exception) -> bool:
        text = str(exc).lower()
        return "404" in text or ("model" in text and "not found" in text)

    async def _installed_model_fallback(self, attempted_model: str) -> str | None:
        """Return a usable installed model when the configured default is stale."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self._base_url.rstrip('/')}/api/tags", timeout=3)
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            return None

        names = [
            str(item.get("name", "")).strip()
            for item in data.get("models", [])
            if item.get("name")
        ]
        if not names:
            return None

        attempted_raw = attempted_model.removeprefix("ollama/")
        for preferred in _FALLBACK_MODEL_PREFERENCE:
            for name in names:
                raw = name.split(":")[0]
                if name == preferred or raw == preferred or name.startswith(f"{preferred}:"):
                    candidate = f"ollama/{name}"
                    if candidate != attempted_model and name != attempted_raw:
                        return candidate

        first = f"ollama/{names[0]}"
        return first if first != attempted_model else None

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
            if self._looks_like_missing_model(e):
                fallback_model = await self._installed_model_fallback(model_name)
                if fallback_model:
                    try:
                        response = await litellm.acompletion(
                            messages=msgs,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            timeout=self._timeout,
                            **self._kwargs(fallback_model),
                            **kwargs,
                        )
                        model_name = fallback_model
                    except Exception as retry_error:
                        e = retry_error
                    else:
                        elapsed = int((time.monotonic() - start) * 1000)
                        usage_data = response.usage
                        usage = Usage(
                            input_tokens=getattr(usage_data, "prompt_tokens", 0) or 0,
                            output_tokens=getattr(usage_data, "completion_tokens", 0) or 0,
                            total_tokens=getattr(usage_data, "total_tokens", 0) or 0,
                        )
                        content = response.choices[0].message.content or ""
                        return CompletionResponse(
                            content=content,
                            model=response.model or model_name,
                            provider=self._provider_name,
                            usage=usage,
                            cost_usd=Decimal("0"),
                            latency_ms=elapsed,
                            finish_reason=FinishReason.STOP,
                            metadata={"fallback_model": model_name},
                        )
            err_str = str(e).lower()
            looks_like_conn = (
                "connection" in err_str
                or "refused" in err_str
                or "connect" in err_str
            )
            # Only declare "not running" if the daemon actually isn't
            # answering — otherwise the real cause is model-not-found,
            # timeout, or some other transient issue and the user needs
            # the underlying error, not a misleading "start Ollama" hint.
            if looks_like_conn and not _ollama_daemon_reachable(self._base_url):
                raise ProviderUnavailableError(
                    _rootless_ollama_unavailable_message(self._base_url),
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

        try:
            async for chunk in self._direct_stream(msgs, model_name, temperature, max_tokens):
                yield chunk
            return
        except Exception as e:
            recovered = False
            if self._looks_like_missing_model(e):
                fallback_model = await self._installed_model_fallback(model_name)
                if fallback_model:
                    try:
                        async for chunk in self._direct_stream(msgs, fallback_model, temperature, max_tokens):
                            yield chunk
                        model_name = fallback_model
                        recovered = True
                    except Exception as retry_error:
                        e = retry_error
            if not recovered:
                err_str = str(e).lower()
                looks_like_conn = (
                    "connection" in err_str
                    or "refused" in err_str
                    or "connect" in err_str
                )
                if looks_like_conn and not _ollama_daemon_reachable(self._base_url):
                    raise ProviderUnavailableError(
                        _rootless_ollama_unavailable_message(self._base_url),
                        provider=self._provider_name,
                        original_error=e,
                    ) from e
                raise _map_error(e, self._provider_name) from e

    async def _direct_stream(
        self,
        messages: list[dict],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[StreamChunk]:
        """Stream directly from Ollama's native API.

        LiteLLM is useful for cloud providers, but local Ollama is more reliable
        when we keep the chat stream close to the daemon. This also makes the
        first local-model test less fragile on fresh VMs where the model is
        still loading into VRAM.
        """
        raw_model = model.removeprefix("ollama/")
        accumulated = ""
        timeout = httpx.Timeout(self._timeout, connect=5.0, read=self._timeout, write=30.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{self._base_url.rstrip('/')}/api/chat",
                json={
                    "model": raw_model,
                    "messages": messages,
                    "stream": True,
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_tokens,
                    },
                },
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("error"):
                        raise RuntimeError(str(data["error"]))
                    delta = str((data.get("message") or {}).get("content") or "")
                    accumulated += delta
                    is_final = bool(data.get("done"))
                    usage = None
                    if is_final:
                        prompt_tokens = int(data.get("prompt_eval_count") or 0)
                        output_tokens = int(data.get("eval_count") or self.estimate_tokens(accumulated))
                        usage = Usage(
                            input_tokens=prompt_tokens,
                            output_tokens=output_tokens,
                            total_tokens=prompt_tokens + output_tokens,
                        )
                    yield StreamChunk(
                        delta=delta,
                        is_final=is_final,
                        accumulated_content=accumulated,
                        model=model,
                        provider=self._provider_name,
                        usage=usage,
                        cost_usd=Decimal("0") if is_final else None,
                        finish_reason=FinishReason.STOP if is_final else None,
                    )
                    if is_final:
                        return

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
