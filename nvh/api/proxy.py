"""OpenAI-compatible API proxy.

Exposes NVHive's multi-provider routing as a standard OpenAI API.
Any tool that supports custom OpenAI base URLs can use NVHive:

  # In .env or tool config:
  OPENAI_API_BASE=http://localhost:8000/v1/proxy
  OPENAI_API_KEY=anything    # NVHive handles the real keys

  # Or in code:
  client = OpenAI(base_url="http://localhost:8000/v1/proxy")

This means:
- VSCode Copilot alternatives can use NVHive
- LangChain apps route through NVHive's smart router
- Any OpenAI SDK client gets multi-provider fallback for free
- Budget limits and advisor profiles apply automatically
- NemoClaw agents get multi-model consensus via council/throwdown

Model routing:
  "auto" or "nvhive"          → smart routing (best available)
  "safe"                      → local Ollama only
  "council" or "council:N"    → N-model consensus (default 3)
  "throwdown"                 → two-pass deep analysis
  "gpt-4o", "gpt-4o-mini"    → OpenAI provider
  "claude-*"                  → Anthropic provider
  "gemini-*"                  → Google provider
  "llama-*", "mixtral-*"     → Groq/local provider
  any other known model name  → routed by NVHive

NemoClaw integration:
  Register nvHive as an OpenShell inference provider and all agent
  requests flow through nvHive's smart router, council, or throwdown.

  Headers:
    x-nvhive-privacy: local-only   → forces Ollama routing (no cloud)
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

# ---------------------------------------------------------------------------
# Model → provider mapping for well-known model IDs
# ---------------------------------------------------------------------------

# Maps model name prefixes/exact matches to NVHive provider names
_MODEL_TO_PROVIDER: dict[str, str] = {
    "gpt-4o": "openai",
    "gpt-4o-mini": "openai",
    "gpt-4-turbo": "openai",
    "gpt-4": "openai",
    "gpt-3.5-turbo": "openai",
    "o1": "openai",
    "o1-mini": "openai",
    "o1-preview": "openai",
    "o3": "openai",
    "o3-mini": "openai",
    "claude-3-5-sonnet": "anthropic",
    "claude-3-5-haiku": "anthropic",
    "claude-3-opus": "anthropic",
    "claude-3-sonnet": "anthropic",
    "claude-3-haiku": "anthropic",
    "claude-sonnet-4": "anthropic",
    "claude-opus-4": "anthropic",
    "gemini-2.0": "google",
    "gemini-1.5-pro": "google",
    "gemini-1.5-flash": "google",
    "gemini-pro": "google",
    "llama-3.3": "groq",
    "llama-3.1": "groq",
    "llama-3": "groq",
    "mixtral-8x7b": "groq",
    "mixtral-8x22b": "groq",
    "mistral-large": "mistral",
    "mistral-medium": "mistral",
    "mistral-small": "mistral",
    "deepseek-chat": "deepseek",
    "deepseek-coder": "deepseek",
}

# Virtual model names handled by NVHive routing logic
_NVHIVE_VIRTUAL_MODELS = {
    "auto", "nvhive", "nvhive-auto", "safe", "local",
    "council", "throwdown",
}


def parse_council_model(model: str) -> int | None:
    """Parse council model spec like 'council', 'council:3', 'council:5'.

    Returns the member count, or None if this is not a council model.
    """
    if not model:
        return None
    if model == "council":
        return 3  # default council size
    if model.startswith("council:"):
        try:
            count = int(model.split(":", 1)[1])
            return max(2, min(count, 10))  # clamp to 2-10
        except ValueError:
            return 3
    return None


def is_throwdown_model(model: str | None) -> bool:
    """Check if the model spec requests throwdown mode."""
    return model == "throwdown"


def resolve_provider_from_model(model: str | None) -> tuple[str | None, str | None]:
    """Return (provider_override, model_override) for a given OpenAI model name.

    Returns (None, None) for smart routing, ("ollama", None) for safe/local,
    or (provider_name, model_name) for a known mapping.

    Council and throwdown models return (None, None) — the caller should
    check parse_council_model() / is_throwdown_model() first.
    """
    if not model or model in ("auto", "nvhive", "nvhive-auto"):
        return None, None  # smart routing

    if model in ("safe", "local"):
        return "ollama", None

    # Council and throwdown are handled by the caller, not the router
    if parse_council_model(model) is not None or is_throwdown_model(model):
        return None, None

    # Exact match first
    if model in _MODEL_TO_PROVIDER:
        return _MODEL_TO_PROVIDER[model], model

    # Prefix match (e.g. "gpt-4o-2024-11-20" → "openai")
    for prefix, provider in _MODEL_TO_PROVIDER.items():
        if model.startswith(prefix):
            return provider, model

    # Unknown model — let NVHive route it with the model as a hint
    return None, model


def openai_messages_to_nvhive(messages: list[dict[str, Any]]) -> tuple[str, str | None]:
    """Extract the user prompt and optional system prompt from an OpenAI messages list.

    For multi-turn conversations we concatenate them into a single prompt string
    with role prefixes so NVHive's single-turn engine can process them.
    Returns (prompt, system_prompt).
    """
    system_prompt: str | None = None
    conversation_parts: list[str] = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # Handle structured content (vision / tool messages)
        if isinstance(content, list):
            text_parts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            content = " ".join(text_parts)

        if role == "system":
            system_prompt = content
        elif role == "assistant":
            conversation_parts.append(f"Assistant: {content}")
        else:
            conversation_parts.append(f"User: {content}")

    prompt = "\n".join(conversation_parts) if len(conversation_parts) > 1 else (
        conversation_parts[0].removeprefix("User: ") if conversation_parts else ""
    )
    return prompt, system_prompt


def format_openai_response(
    content: str,
    model: str,
    provider: str,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    """Build an OpenAI-format chat completion response dict."""
    now = int(time.time())
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": now,
        "model": model,
        "system_fingerprint": f"nvhive-{provider}",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "logprobs": None,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "x_nvhive_provider": provider,
    }


async def openai_stream_generator(
    engine: Any,
    prompt: str,
    provider_override: str | None,
    model_override: str | None,
    system_prompt: str | None,
    temperature: float | None,
    max_tokens: int | None,
    requested_model: str,
) -> AsyncGenerator[bytes, None]:
    """Yield SSE bytes in OpenAI streaming format.

    Produces ``data: {...}`` lines followed by ``data: [DONE]``.
    """
    from nvh.providers.base import Message

    config = engine.config
    temp = temperature if temperature is not None else config.defaults.temperature
    max_tok = max_tokens or config.defaults.max_tokens
    sys_prompt = system_prompt or config.defaults.system_prompt or None

    chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    now = int(time.time())

    decision = engine.router.route(
        query=prompt,
        provider_override=provider_override,
        model_override=model_override,
    )

    if not engine.registry.has(decision.provider):
        error_chunk = {
            "error": {
                "message": f"Provider '{decision.provider}' is not available.",
                "type": "provider_unavailable",
                "code": "provider_not_found",
            }
        }
        yield f"data: {json.dumps(error_chunk)}\n\ndata: [DONE]\n\n".encode()
        return

    provider = engine.registry.get(decision.provider)
    messages: list[Message] = []
    if sys_prompt:
        messages.append(Message(role="system", content=sys_prompt))
    messages.append(Message(role="user", content=prompt))

    effective_model = decision.model or requested_model or "nvhive"

    # Opening chunk with role
    role_chunk = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": now,
        "model": effective_model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": ""},
                "logprobs": None,
                "finish_reason": None,
            }
        ],
    }
    yield f"data: {json.dumps(role_chunk)}\n\n".encode()

    # Per-chunk stall timeout — prevents a silent provider from hanging
    # OpenAI-compatible clients like Continue / Cursor indefinitely.
    CHUNK_STALL_TIMEOUT = 45.0  # noqa: N806 — local constant, intentional uppercase

    try:
        aiter = provider.stream(
            messages=messages,
            model=decision.model or None,
            temperature=temp,
            max_tokens=max_tok,
            system_prompt=sys_prompt,
        ).__aiter__()

        while True:
            try:
                chunk = await asyncio.wait_for(
                    aiter.__anext__(),
                    timeout=CHUNK_STALL_TIMEOUT,
                )
            except StopAsyncIteration:
                break
            except TimeoutError as exc:
                raise TimeoutError(
                    f"Provider '{decision.provider}' stalled — no tokens for "
                    f"{CHUNK_STALL_TIMEOUT:.0f}s"
                ) from exc

            if chunk.delta:
                content_chunk = {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": now,
                    "model": effective_model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": chunk.delta},
                            "logprobs": None,
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(content_chunk)}\n\n".encode()

            if chunk.is_final:
                finish_chunk = {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": now,
                    "model": effective_model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "logprobs": None,
                            "finish_reason": (
                                chunk.finish_reason.value if chunk.finish_reason else "stop"
                            ),
                        }
                    ],
                }
                yield f"data: {json.dumps(finish_chunk)}\n\n".encode()
                break

    except Exception as exc:
        error_chunk = {
            "error": {
                "message": str(exc),
                "type": "provider_error",
                "code": "stream_error",
            }
        }
        yield f"data: {json.dumps(error_chunk)}\n\n".encode()

    yield b"data: [DONE]\n\n"


def _sse_choices(
    delta: dict[str, Any],
    finish_reason: str | None = None,
) -> list[dict[str, Any]]:
    """Build the choices array for an SSE streaming chunk."""
    return [{
        "index": 0,
        "delta": delta,
        "logprobs": None,
        "finish_reason": finish_reason,
    }]


def _extract_content(result: Any) -> str:
    """Extract text content from a CouncilResponse."""
    if result.synthesis:
        if hasattr(result.synthesis, "content"):
            return result.synthesis.content
        return str(result.synthesis)
    if result.member_responses:
        first = next(iter(result.member_responses.values()))
        if hasattr(first, "content"):
            return first.content
        return str(first)
    return ""


async def council_stream_generator(
    engine: Any,
    prompt: str,
    system_prompt: str | None,
    temperature: float | None,
    max_tokens: int | None,
    council_size: int,
    requested_model: str,
) -> AsyncGenerator[bytes, None]:
    """Stream a council result as OpenAI SSE chunks.

    Runs the council (parallel dispatch + synthesis), then streams the
    synthesized content word-by-word so the client sees progressive output.
    """
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    now = int(time.time())
    effective_model = requested_model or f"council:{council_size}"

    # Opening chunk
    role_chunk = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": now,
        "model": effective_model,
        "choices": _sse_choices(
            {"role": "assistant", "content": ""}
        ),
    }
    yield f"data: {json.dumps(role_chunk)}\n\n".encode()

    try:
        result = await engine.run_council(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            num_agents=council_size,
            auto_agents=True,
        )
    except Exception as exc:
        error_chunk = {
            "error": {
                "message": str(exc),
                "type": "council_error",
                "code": "council_failed",
            }
        }
        yield f"data: {json.dumps(error_chunk)}\n\n".encode()
        yield b"data: [DONE]\n\n"
        return

    content = _extract_content(result)

    # Stream content in word-sized chunks for progressive rendering
    words = content.split(" ")
    for i, word in enumerate(words):
        delta = word if i == 0 else f" {word}"
        chunk = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": now,
            "model": effective_model,
            "choices": _sse_choices({"content": delta}),
        }
        yield f"data: {json.dumps(chunk)}\n\n".encode()

    # Final chunk
    finish_chunk = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": now,
        "model": effective_model,
        "choices": _sse_choices({}, finish_reason="stop"),
    }
    yield f"data: {json.dumps(finish_chunk)}\n\n".encode()
    yield b"data: [DONE]\n\n"


async def throwdown_stream_generator(
    engine: Any,
    prompt: str,
    system_prompt: str | None,
    temperature: float | None,
    max_tokens: int | None,
) -> AsyncGenerator[bytes, None]:
    """Stream a throwdown result as OpenAI SSE chunks."""
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    now = int(time.time())

    role_chunk = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": now,
        "model": "throwdown",
        "choices": _sse_choices(
            {"role": "assistant", "content": ""}
        ),
    }
    yield f"data: {json.dumps(role_chunk)}\n\n".encode()

    try:
        result = await engine.run_council(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            auto_agents=True,
            strategy="throwdown",
        )
    except Exception as exc:
        error_chunk = {
            "error": {
                "message": str(exc),
                "type": "throwdown_error",
                "code": "throwdown_failed",
            }
        }
        yield f"data: {json.dumps(error_chunk)}\n\n".encode()
        yield b"data: [DONE]\n\n"
        return

    content = _extract_content(result)

    words = content.split(" ")
    for i, word in enumerate(words):
        delta = word if i == 0 else f" {word}"
        chunk = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": now,
            "model": "throwdown",
            "choices": _sse_choices({"content": delta}),
        }
        yield f"data: {json.dumps(chunk)}\n\n".encode()

    finish_chunk = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": now,
        "model": "throwdown",
        "choices": _sse_choices({}, finish_reason="stop"),
    }
    yield f"data: {json.dumps(finish_chunk)}\n\n".encode()
    yield b"data: [DONE]\n\n"


def build_models_list(registry: Any) -> dict[str, Any]:
    """Return an OpenAI-format /models response from the NVHive registry."""
    now = int(time.time())
    model_objects: list[dict[str, Any]] = []

    # Add virtual NVHive routing models
    for virtual_id, description in [
        ("nvhive", "NVHive smart router — auto-selects the best available provider"),
        ("auto", "Alias for nvhive smart routing"),
        ("safe", "Routes to local Ollama only — no data leaves your machine"),
        ("council", "Multi-LLM consensus — dispatches to 3 models and synthesizes"),
        ("council:3", "Council with 3 members"),
        ("council:5", "Council with 5 members"),
        ("throwdown", "Two-pass deep analysis with critique and refinement"),
    ]:
        model_objects.append({
            "id": virtual_id,
            "object": "model",
            "created": now,
            "owned_by": "nvhive",
            "description": description,
        })

    # Add real provider models from the registry
    for provider_name in (registry.list_enabled() if hasattr(registry, "list_enabled") else []):
        try:
            provider = registry.get(provider_name)
            models = getattr(provider, "available_models", None)
            if callable(models):
                models = models()
            if not models and hasattr(provider, "default_model"):
                models = [provider.default_model]
            for model_id in (models or []):
                model_objects.append({
                    "id": model_id,
                    "object": "model",
                    "created": now,
                    "owned_by": provider_name,
                })
        except Exception:
            continue

    return {
        "object": "list",
        "data": model_objects,
    }


# ------------------------------------------------------------------
# Anthropic/Claude API format proxy
# ------------------------------------------------------------------
# Accepts requests in Anthropic Messages API format and routes them
# through nvHive's engine. This lets OpenClaw users and any tool
# that talks to the Anthropic API point at nvHive instead:
#
#   ANTHROPIC_API_URL=http://localhost:8000/v1/anthropic
#   ANTHROPIC_API_KEY=anything
#
# nvHive handles routing, fallback, and multi-provider orchestration.
# ------------------------------------------------------------------

_CLAUDE_STOP_TO_NVHIVE = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
}


def anthropic_messages_to_nvhive(
    messages: list[dict[str, Any]],
    system: str | None = None,
) -> tuple[str, str | None]:
    """Convert Anthropic Messages format to nvHive (prompt, system).

    Anthropic format uses top-level ``system`` param and messages
    with ``role`` + ``content`` (where content can be text or list
    of content blocks).
    """
    parts: list[str] = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # Content can be a string or list of content blocks
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        text_parts.append(
                            str(block.get("content", "")),
                        )
                elif isinstance(block, str):
                    text_parts.append(block)
            content = " ".join(text_parts)

        if role == "assistant":
            parts.append(f"Assistant: {content}")
        else:
            parts.append(f"User: {content}")

    prompt = (
        "\n".join(parts) if len(parts) > 1
        else (
            parts[0].removeprefix("User: ") if parts else ""
        )
    )
    return prompt, system


def format_anthropic_response(
    content: str,
    model: str,
    provider: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    stop_reason: str = "end_turn",
) -> dict[str, Any]:
    """Build an Anthropic Messages API response dict."""
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [
            {
                "type": "text",
                "text": content,
            }
        ],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
        "x_nvhive_provider": provider,
    }


async def anthropic_stream_generator(
    engine: Any,
    prompt: str,
    provider_override: str | None,
    model_override: str | None,
    system_prompt: str | None,
    temperature: float | None,
    max_tokens: int | None,
    requested_model: str,
) -> AsyncGenerator[bytes, None]:
    """Yield SSE events in Anthropic streaming format.

    Produces message_start, content_block_start, content_block_delta,
    content_block_stop, and message_stop events.
    """
    from nvh.providers.base import Message

    config = engine.config
    temp = (
        temperature if temperature is not None
        else config.defaults.temperature
    )
    max_tok = max_tokens or config.defaults.max_tokens
    sys = system_prompt or config.defaults.system_prompt or None

    msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    decision = engine.router.route(
        query=prompt,
        provider_override=provider_override,
        model_override=model_override,
    )

    if not engine.registry.has(decision.provider):
        error_event = {
            "type": "error",
            "error": {
                "type": "provider_unavailable",
                "message": (
                    f"Provider '{decision.provider}' is not"
                    " available. Check nvh status."
                ),
            },
        }
        yield (
            f"event: error\ndata: {json.dumps(error_event)}\n\n"
        ).encode()
        return

    provider = engine.registry.get(decision.provider)
    messages: list[Message] = []
    if sys:
        messages.append(Message(role="system", content=sys))
    messages.append(Message(role="user", content=prompt))

    effective_model = (
        decision.model or requested_model or "nvhive"
    )

    # message_start event
    start_event = {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": effective_model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }
    yield (
        f"event: message_start\n"
        f"data: {json.dumps(start_event)}\n\n"
    ).encode()

    # content_block_start
    block_start = {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    }
    yield (
        f"event: content_block_start\n"
        f"data: {json.dumps(block_start)}\n\n"
    ).encode()

    total_output_tokens = 0
    # Per-chunk stall timeout — keeps Anthropic-compatible clients from
    # hanging when a backing provider stalls mid-stream.
    CHUNK_STALL_TIMEOUT = 45.0  # noqa: N806 — local constant, intentional uppercase
    try:
        aiter = provider.stream(
            messages=messages,
            model=decision.model or None,
            temperature=temp,
            max_tokens=max_tok,
            system_prompt=sys,
        ).__aiter__()

        while True:
            try:
                chunk = await asyncio.wait_for(
                    aiter.__anext__(),
                    timeout=CHUNK_STALL_TIMEOUT,
                )
            except StopAsyncIteration:
                break
            except TimeoutError as exc:
                raise TimeoutError(
                    f"Provider '{decision.provider}' stalled — no tokens for "
                    f"{CHUNK_STALL_TIMEOUT:.0f}s"
                ) from exc

            if chunk.delta:
                delta_event = {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "text_delta",
                        "text": chunk.delta,
                    },
                }
                yield (
                    f"event: content_block_delta\n"
                    f"data: {json.dumps(delta_event)}\n\n"
                ).encode()

            if chunk.usage:
                total_output_tokens = chunk.usage.output_tokens

    except Exception as exc:
        error_event = {
            "type": "error",
            "error": {
                "type": "api_error",
                "message": f"Stream error: {type(exc).__name__}",
            },
        }
        yield (
            f"event: error\n"
            f"data: {json.dumps(error_event)}\n\n"
        ).encode()
        return

    # content_block_stop
    block_stop = {"type": "content_block_stop", "index": 0}
    yield (
        f"event: content_block_stop\n"
        f"data: {json.dumps(block_stop)}\n\n"
    ).encode()

    # message_delta (final usage + stop reason)
    msg_delta = {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": total_output_tokens},
    }
    yield (
        f"event: message_delta\n"
        f"data: {json.dumps(msg_delta)}\n\n"
    ).encode()

    # message_stop
    yield b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n"
