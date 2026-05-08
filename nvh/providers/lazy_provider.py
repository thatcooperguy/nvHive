"""Lazy provider wrapper for slow optional adapters."""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from typing import Any

from nvh.providers.base import CompletionResponse, HealthStatus, Message, ModelInfo, StreamChunk


class LazyProvider:
    """Import and construct a provider only when it is actually used."""

    def __init__(self, name: str, module_path: str, class_name: str, **kwargs: Any) -> None:
        self._name = name
        self._module_path = module_path
        self._class_name = class_name
        self._kwargs = kwargs
        self._provider: Any | None = None

    @property
    def name(self) -> str:
        return self._name

    def _load(self) -> Any:
        if self._provider is None:
            module = importlib.import_module(self._module_path)
            provider_cls = getattr(module, self._class_name)
            self._provider = provider_cls(**self._kwargs)
        return self._provider

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 1.0,
        max_tokens: int = 4096,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> CompletionResponse:
        return await self._load().complete(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
            **kwargs,
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
        async for chunk in self._load().stream(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
            **kwargs,
        ):
            yield chunk

    async def list_models(self) -> list[ModelInfo]:
        return await self._load().list_models()

    async def health_check(self) -> HealthStatus:
        return await self._load().health_check()

    def estimate_tokens(self, text: str) -> int:
        return self._load().estimate_tokens(text)
