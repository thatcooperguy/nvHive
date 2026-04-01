"""Streaming utilities for normalizing provider output."""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

from nvh.providers.base import CompletionResponse, StreamChunk, Usage


async def collect_stream(
    stream: AsyncIterator[StreamChunk],
) -> CompletionResponse:
    """Consume a stream and collect it into a CompletionResponse."""
    content = ""
    model = ""
    provider = ""
    usage = Usage()
    cost = Decimal("0")
    finish_reason = None

    async for chunk in stream:
        content = chunk.accumulated_content or (content + chunk.delta)
        model = chunk.model or model
        provider = chunk.provider or provider
        if chunk.usage:
            usage = chunk.usage
        if chunk.cost_usd is not None:
            cost = chunk.cost_usd
        if chunk.finish_reason:
            finish_reason = chunk.finish_reason

    from nvh.providers.base import FinishReason
    return CompletionResponse(
        content=content,
        model=model,
        provider=provider,
        usage=usage,
        cost_usd=cost,
        finish_reason=finish_reason or FinishReason.STOP,
    )


async def stream_to_callback(
    stream: AsyncIterator[StreamChunk],
    on_token: callable | None = None,
) -> CompletionResponse:
    """Consume a stream, calling on_token for each chunk, then return the full response."""
    content = ""
    model = ""
    provider = ""
    usage = Usage()
    cost = Decimal("0")
    finish_reason = None

    async for chunk in stream:
        if chunk.delta and on_token:
            on_token(chunk.delta)
        content = chunk.accumulated_content or (content + chunk.delta)
        model = chunk.model or model
        provider = chunk.provider or provider
        if chunk.usage:
            usage = chunk.usage
        if chunk.cost_usd is not None:
            cost = chunk.cost_usd
        if chunk.finish_reason:
            finish_reason = chunk.finish_reason

    from nvh.providers.base import FinishReason
    return CompletionResponse(
        content=content,
        model=model,
        provider=provider,
        usage=usage,
        cost_usd=cost,
        finish_reason=finish_reason or FinishReason.STOP,
    )
