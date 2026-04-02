"""Provider protocol and shared data models."""

from __future__ import annotations

import enum
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class FinishReason(enum.StrEnum):
    STOP = "stop"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    TOOL_CALLS = "tool_calls"
    ERROR = "error"


class TaskType(enum.StrEnum):
    CODE_GENERATION = "code_generation"
    CODE_REVIEW = "code_review"
    CODE_DEBUG = "code_debug"
    REASONING = "reasoning"
    MATH = "math"
    CREATIVE_WRITING = "creative_writing"
    SUMMARIZATION = "summarization"
    TRANSLATION = "translation"
    CONVERSATION = "conversation"
    QUESTION_ANSWERING = "question_answering"
    STRUCTURED_EXTRACTION = "structured_extraction"
    MULTIMODAL = "multimodal"
    LONG_CONTEXT_ANALYSIS = "long_context_analysis"


class CircuitState(enum.StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class StreamChunk(BaseModel):
    delta: str = ""
    is_final: bool = False
    accumulated_content: str = ""
    model: str = ""
    provider: str = ""
    usage: Usage | None = None
    cost_usd: Decimal | None = None
    finish_reason: FinishReason | None = None


class CompletionResponse(BaseModel):
    content: str
    model: str
    provider: str
    usage: Usage
    cost_usd: Decimal = Decimal("0")
    latency_ms: int = 0
    finish_reason: FinishReason = FinishReason.STOP
    cache_hit: bool = False
    fallback_from: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelInfo(BaseModel):
    model_id: str
    provider: str
    display_name: str = ""
    context_window: int = 0
    max_output_tokens: int = 0
    supports_streaming: bool = True
    supports_tools: bool = False
    supports_vision: bool = False
    supports_json_mode: bool = False
    supports_system_prompt: bool = True
    input_cost_per_1m_tokens: Decimal = Decimal("0")
    output_cost_per_1m_tokens: Decimal = Decimal("0")
    typical_latency_ms: int = 1000
    capability_scores: dict[str, float] = Field(default_factory=dict)
    status: str = "active"
    successor: str | None = None


class HealthStatus(BaseModel):
    provider: str
    healthy: bool
    latency_ms: int | None = None
    error: str | None = None
    models_available: int = 0


class Message(BaseModel):
    role: str  # system, user, assistant, tool
    content: str
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


# ---------------------------------------------------------------------------
# Error Taxonomy
# ---------------------------------------------------------------------------

class ProviderError(Exception):
    """Base error for all provider-related failures."""

    def __init__(self, message: str, provider: str = "", original_error: Exception | None = None):
        self.provider = provider
        self.original_error = original_error
        super().__init__(message)


class AuthenticationError(ProviderError):
    pass


class RateLimitError(ProviderError):
    def __init__(
        self,
        message: str,
        provider: str = "",
        retry_after: float | None = None,
        original_error: Exception | None = None,
    ):
        self.retry_after = retry_after
        super().__init__(message, provider, original_error)


class TokenLimitError(ProviderError):
    pass


class ContentFilterError(ProviderError):
    pass


class ModelNotFoundError(ProviderError):
    pass


class ProviderUnavailableError(ProviderError):
    pass


class InsufficientQuotaError(ProviderError):
    pass


class InvalidRequestError(ProviderError):
    pass


# ---------------------------------------------------------------------------
# Provider Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class Provider(Protocol):
    """Interface that every LLM provider adapter must implement."""

    @property
    def name(self) -> str:
        """Unique provider identifier (e.g. 'openai', 'anthropic')."""
        ...

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 1.0,
        max_tokens: int = 4096,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> CompletionResponse:
        """Send a completion request and return the full response."""
        ...

    async def stream(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 1.0,
        max_tokens: int = 4096,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """Send a completion request and yield streaming chunks."""
        ...

    async def list_models(self) -> list[ModelInfo]:
        """List available models for this provider."""
        ...

    async def health_check(self) -> HealthStatus:
        """Check if the provider is reachable and the API key is valid."""
        ...

    def estimate_tokens(self, text: str) -> int:
        """Estimate token count for a given text."""
        ...
