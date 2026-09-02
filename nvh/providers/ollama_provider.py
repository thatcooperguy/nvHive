"""Ollama (local) provider adapter via LiteLLM."""

from __future__ import annotations

import json
import re
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
from nvh.providers.openai_compatible import _build_messages, _map_error
from nvh.utils.ollama import ollama_base_url

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
    "nemotron-3-nano-omni",
    "nemotron-omni",
    "nemotron",
    "llama3.3:70b",
    "qwen2.5-coder:32b",
    "llama3.2-vision",
    "qwen3:8b",
    "deepseek-r1:8b",
    "qwen2.5-coder:7b",
    "llama3.1:8b",
    "llama3.1",
    "gemma3:4b",
    "llava:7b",
    "minicpm-v",
    "moondream",
    "nemotron-mini",
)

_VISION_MODEL_PREFERENCE = (
    "nemotron-3-nano-omni",
    "nemotron-omni",
    "llama3.2-vision",
    "llava:7b",
    "llava",
    "minicpm-v",
    "moondream",
    "bakllava",
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
        default_model: str = "ollama/gemma3:4b",
        fallback_model: str = "",
        base_url: str | None = None,
        provider_name: str = "ollama",
        timeout: int = 300,
    ):
        self._default_model = default_model
        self._fallback_model = fallback_model
        self._base_url = ollama_base_url(base_url)
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

    @staticmethod
    def _is_auto_model_selection(model: str | None) -> bool:
        m = (model or "").strip().lower()
        return not m or m in _AUTO_MODEL_CHOICES

    def _kwargs(self, model: str) -> dict[str, Any]:
        kw: dict[str, Any] = {"model": model, "api_base": self._base_url}
        return kw

    @staticmethod
    def _looks_like_missing_model(exc: Exception) -> bool:
        text = str(exc).lower()
        return "404" in text or ("model" in text and "not found" in text)

    @staticmethod
    def _should_try_installed_fallback(exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            OllamaProvider._looks_like_missing_model(exc)
            or "timeout" in text
            or "timed out" in text
            or "stalled" in text
            or "no tokens" in text
            or "no text" in text
            or "empty" in text
        )

    async def _installed_model_fallback(
        self,
        attempted_model: str,
        *,
        prefer_vision: bool = False,
    ) -> str | None:
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
        preference = _VISION_MODEL_PREFERENCE if prefer_vision else _FALLBACK_MODEL_PREFERENCE
        for preferred in preference:
            preferred_base = preferred.split(":")[0]
            for name in names:
                raw = name.split(":")[0]
                if (
                    name == preferred
                    or raw == preferred
                    or raw == preferred_base
                    or name.startswith(f"{preferred}:")
                ):
                    candidate = f"ollama/{name}"
                    if attempted_model == "__auto__" or (
                        candidate != attempted_model and name != attempted_raw
                    ):
                        return candidate

        first = f"ollama/{names[0]}"
        return first if first != attempted_model else None

    @staticmethod
    def _messages_for_ollama(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert OpenAI-style multimodal messages to Ollama native messages."""
        converted: list[dict[str, Any]] = []
        for msg in messages:
            content = msg.get("content", "")
            if not isinstance(content, list):
                converted.append(dict(msg))
                continue

            text_parts: list[str] = []
            images: list[str] = []
            for part in content:
                if isinstance(part, str):
                    text_parts.append(part)
                    continue
                if not isinstance(part, dict):
                    text_parts.append(str(part))
                    continue
                kind = str(part.get("type", "")).lower()
                if kind == "text":
                    text_parts.append(str(part.get("text", "")))
                    continue
                if kind == "image_url":
                    image_url = part.get("image_url")
                    url = ""
                    if isinstance(image_url, dict):
                        url = str(image_url.get("url", ""))
                    elif image_url:
                        url = str(image_url)
                    if url.startswith("data:image/") and "," in url:
                        images.append(url.split(",", 1)[1])
                    elif url and re.fullmatch(r"[A-Za-z0-9+/=\s]+", url):
                        images.append(url.strip())
                    elif url:
                        text_parts.append(f"[Image URL attached: {url}]")

            out = {k: v for k, v in msg.items() if k != "content"}
            out["content"] = "\n".join(part for part in text_parts if part).strip()
            if images:
                out["images"] = images
            converted.append(out)
        return converted

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 1.0,
        max_tokens: int = 4096,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> CompletionResponse:
        prefer_vision = bool(kwargs.pop("prefer_vision", False))
        auto_model = self._is_auto_model_selection(model)
        model_name = self._get_model(model)
        initial_model_name = model_name
        msgs = _build_messages(messages, system_prompt)
        start = time.monotonic()

        if auto_model:
            installed = await self._installed_model_fallback(
                "__auto__",
                prefer_vision=prefer_vision,
            )
            if installed:
                model_name = installed

        # Prefer Ollama's native API for local desktop installs. It avoids a
        # class of LiteLLM edge cases where a freshly loaded local model reports
        # usage but returns no text, which made first-run quick tests feel broken.
        direct_error: Exception | None = None
        try:
            content = await self._direct_complete(
                msgs, model_name, temperature, max_tokens,
            )
            if content.strip():
                elapsed = int((time.monotonic() - start) * 1000)
                output_tokens = max(1, self.estimate_tokens(content))
                prompt_tokens = sum(
                    self.estimate_tokens(str(message.get("content", "")))
                    for message in msgs
                )
                return CompletionResponse(
                    content=content,
                    model=model_name,
                    provider=self._provider_name,
                    usage=Usage(
                        input_tokens=prompt_tokens,
                        output_tokens=output_tokens,
                        total_tokens=prompt_tokens + output_tokens,
                    ),
                    cost_usd=Decimal("0"),
                    latency_ms=elapsed,
                    finish_reason=FinishReason.STOP,
                    metadata={"transport": "ollama-api"},
                )
            if auto_model:
                fallback_model = await self._installed_model_fallback(
                    model_name,
                    prefer_vision=prefer_vision,
                )
                if fallback_model:
                    content = await self._direct_complete(
                        msgs, fallback_model, temperature, max_tokens,
                    )
                    if content.strip():
                        elapsed = int((time.monotonic() - start) * 1000)
                        output_tokens = max(1, self.estimate_tokens(content))
                        prompt_tokens = sum(
                            self.estimate_tokens(str(message.get("content", "")))
                            for message in msgs
                        )
                        return CompletionResponse(
                            content=content,
                            model=fallback_model,
                            provider=self._provider_name,
                            usage=Usage(
                                input_tokens=prompt_tokens,
                                output_tokens=output_tokens,
                                total_tokens=prompt_tokens + output_tokens,
                            ),
                            cost_usd=Decimal("0"),
                            latency_ms=elapsed,
                            finish_reason=FinishReason.STOP,
                            metadata={
                                "transport": "ollama-api",
                                "fallback_model": fallback_model,
                                "fallback_reason": "empty response",
                            },
                        )
            direct_error = RuntimeError(f"Provider '{self._provider_name}' returned no text from {model_name}")
        except Exception as exc:
            direct_error = exc
            if self._should_try_installed_fallback(exc):
                fallback_model = await self._installed_model_fallback(
                    model_name,
                    prefer_vision=prefer_vision,
                )
                if fallback_model:
                    try:
                        content = await self._direct_complete(
                            msgs, fallback_model, temperature, max_tokens,
                        )
                        if content.strip():
                            elapsed = int((time.monotonic() - start) * 1000)
                            output_tokens = max(1, self.estimate_tokens(content))
                            prompt_tokens = sum(
                                self.estimate_tokens(str(message.get("content", "")))
                                for message in msgs
                            )
                            return CompletionResponse(
                                content=content,
                                model=fallback_model,
                                provider=self._provider_name,
                                usage=Usage(
                                    input_tokens=prompt_tokens,
                                    output_tokens=output_tokens,
                                    total_tokens=prompt_tokens + output_tokens,
                                ),
                                cost_usd=Decimal("0"),
                                latency_ms=elapsed,
                                finish_reason=FinishReason.STOP,
                                metadata={
                                    "transport": "ollama-api",
                                    "fallback_model": fallback_model,
                                },
                            )
                    except Exception as retry_error:
                        direct_error = retry_error
        if direct_error is not None:
            err_str = str(direct_error).lower()
            looks_like_conn = (
                "connection" in err_str
                or "refused" in err_str
                or "connect" in err_str
            )
            if looks_like_conn and not _ollama_daemon_reachable(self._base_url):
                raise ProviderUnavailableError(
                    _rootless_ollama_unavailable_message(self._base_url),
                    provider=self._provider_name,
                    original_error=direct_error,
                ) from direct_error

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
            if self._should_try_installed_fallback(e):
                fallback_model = await self._installed_model_fallback(
                    model_name,
                    prefer_vision=prefer_vision,
                )
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

        metadata: dict[str, Any] = {}
        if model_name != initial_model_name:
            metadata["fallback_model"] = model_name

        return CompletionResponse(
            content=content,
            model=response.model or model_name,
            provider=self._provider_name,
            usage=usage,
            cost_usd=Decimal("0"),  # Local models are free
            latency_ms=elapsed,
            finish_reason=FinishReason.STOP,
            metadata=metadata,
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
                    "messages": self._messages_for_ollama(messages),
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
        prefer_vision = bool(kwargs.pop("prefer_vision", False))
        auto_model = self._is_auto_model_selection(model)
        model_name = self._get_model(model)
        msgs = _build_messages(messages, system_prompt)

        if auto_model:
            installed = await self._installed_model_fallback(
                "__auto__",
                prefer_vision=prefer_vision,
            )
            if installed:
                model_name = installed

        try:
            async for chunk in self._direct_stream(msgs, model_name, temperature, max_tokens):
                yield chunk
            return
        except Exception as e:
            recovered = False
            if self._should_try_installed_fallback(e):
                fallback_model = await self._installed_model_fallback(
                    model_name,
                    prefer_vision=prefer_vision,
                )
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
                    "messages": self._messages_for_ollama(messages),
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
                    base_name = str(name).split(":", 1)[0]
                    models.append(ModelInfo(
                        model_id=f"ollama/{name}",
                        provider=self._provider_name,
                        display_name=name,
                        supports_vision=base_name in {
                            "nemotron-3-nano-omni",
                            "nemotron-omni",
                            "llama3.2-vision",
                            "llava",
                            "minicpm-v",
                            "moondream",
                            "bakllava",
                        },
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
