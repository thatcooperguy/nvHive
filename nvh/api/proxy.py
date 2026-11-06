"""OpenAI-compatible API proxy.

Exposes NVHive's multi-provider routing as a standard OpenAI API.
Any tool that supports custom OpenAI base URLs can use NVHive:

  # In .env or tool config (the OpenAI SDK reads OPENAI_BASE_URL; LangChain
  # and LiteLLM also accept the older OPENAI_API_BASE spelling):
  OPENAI_BASE_URL=http://localhost:8000/v1
  OPENAI_API_KEY=anything    # NVHive handles the real keys

  # Or in code (``/v1/proxy`` is the same handler and stays):
  client = OpenAI(base_url="http://localhost:8000/v1")

Streamed completions check ``budget.hard_stop`` before the first provider
call and write a ``QueryLog`` row (``mode="proxy"``) when the stream ends.
The non-streamed calls on the same routes go through ``Engine.query`` in
nvh/api/server.py and have always been logged there as ``mode="simple"``
(the mode ``nvh ask`` writes), so budget totals and ``hard_stop`` see both
while the ledger's ``mode`` column — ``/metrics`` ``task_type``, ``nvh
history`` — tells a streamed proxy row from a non-streamed one.

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
  a spec's default/fallback id, LiteLLM route ("groq/...") or model
  family ("gpt-4o", "claude-*", "gemini-*", "grok-*", "llama-3*")
                              → that provider (nvh.providers.specs)
  anything LiteLLM recognises → the provider LiteLLM names, when it has a spec
  any other model name        → routed by NVHive with the name as a hint

NemoClaw integration:
  Register nvHive as an OpenShell inference provider and all agent
  requests flow through nvHive's smart router, council, or throwdown.

  Headers:
    x-nvhive-privacy: local-only   → forces Ollama routing (no cloud)
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal
from typing import Any

from nvh.core.atohi import ResourcePaused
from nvh.core.engine import BudgetExceededError
from nvh.providers.base import CompletionResponse, FinishReason, Message, StreamChunk, Usage
from nvh.providers.specs import PROVIDER_SPECS

logger = logging.getLogger(__name__)


async def _close_stream(iterator: Any) -> None:
    close = getattr(iterator, "aclose", None)
    if close is not None:
        await close()


def _terminal_resource_pause(*, anthropic: bool = False):
    """Cover setup, iteration and cleanup with one terminal protocol error."""
    def decorate(generate):
        @functools.wraps(generate)
        async def guarded(*args, **kwargs):
            iterator = generate(*args, **kwargs)
            interrupted = False
            try:
                try:
                    async for item in iterator:
                        yield item
                except (asyncio.CancelledError, GeneratorExit):
                    interrupted = True
                    raise
                finally:
                    await _close_stream(iterator)
            except ResourcePaused as exc:
                if interrupted:
                    # A disconnected consumer cannot receive another SSE event.
                    raise
                detail = exc.as_dict()
                error = {"message": detail.pop("error"), "type": "resource_paused", **detail}
                event = {"type": "error", "error": error} if anthropic else {"error": error}
                prefix = "event: error\n" if anthropic else ""
                yield f"{prefix}data: {json.dumps(event)}\n\n".encode()

        return guarded
    return decorate


# ---------------------------------------------------------------------------
# Budget and ledger hooks for the streamed paths
# ---------------------------------------------------------------------------


async def _engine_hook(engine: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    """Call ``engine.<name>(...)`` when the engine has it, awaiting a coroutine.

    Production engines are :class:`nvh.core.engine.Engine`, whose
    ``_check_budget`` and ``_log_query`` are coroutine functions. The stream
    tests drive these generators with bare namespaces and ``MagicMock``
    engines that lack the hook or return a non-awaitable; those keep streaming.
    A ``BudgetExceededError`` raised by the hook propagates to the caller.
    """
    hook = getattr(engine, name, None)
    if hook is None or not callable(hook):
        return None
    result = hook(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _stream_ledger_response(
    chunk: StreamChunk | None, *, provider: str, model: str, latency_ms: int,
) -> CompletionResponse:
    """The ``CompletionResponse`` a finished stream is logged as.

    Usage and cost come from the final chunk, the same fields the Wizard's
    ``_meter_from_chunk`` reads; a stream that ended without them is logged
    with zeros under the provider and model the generator routed to.
    """
    usage = chunk.usage if chunk is not None and chunk.usage is not None else Usage()
    cost = chunk.cost_usd if chunk is not None and chunk.cost_usd is not None else Decimal("0")
    return CompletionResponse(
        content="",
        model=(chunk.model if chunk is not None and chunk.model else model) or "",
        provider=(chunk.provider if chunk is not None and chunk.provider else provider) or "",
        usage=usage,
        cost_usd=cost,
        latency_ms=latency_ms,
        finish_reason=(chunk.finish_reason if chunk is not None and chunk.finish_reason else FinishReason.STOP),
    )


async def _log_stream_spend(
    engine: Any, chunk: StreamChunk | None, *, provider: str, model: str, started: float,
) -> None:
    """Write the ``QueryLog`` row for a stream that finished; never raises."""
    try:
        latency_ms = int((time.monotonic() - started) * 1000)
        response = _stream_ledger_response(chunk, provider=provider, model=model, latency_ms=latency_ms)
        await _engine_hook(engine, "_log_query", response, mode="proxy")
    except Exception:
        logger.debug("proxy stream: ledger write failed", exc_info=True)

# ---------------------------------------------------------------------------
# Model → provider mapping, derived from the spec table
# ---------------------------------------------------------------------------


def _prefix_owners() -> dict[str, list[str]]:
    """``litellm_prefix -> [provider, ...]`` over the spec table."""
    owners: dict[str, list[str]] = {}
    for spec in PROVIDER_SPECS.values():
        if spec.litellm_prefix:
            owners.setdefault(spec.litellm_prefix, []).append(spec.name)
    return owners


def _unique_prefixes() -> dict[str, str]:
    """``litellm_prefix -> provider`` for the routes only one spec uses.

    ``openai/`` is LiteLLM's generic OpenAI-compatible client, shared by
    siliconflow and llm7, so it names no provider.
    """
    return {prefix: names[0] for prefix, names in _prefix_owners().items() if len(names) == 1}


def _shared_prefixes() -> frozenset[str]:
    """The routes more than one spec uses (``openai/``): LiteLLM's answer for them is not a provider."""
    return frozenset(prefix for prefix, names in _prefix_owners().items() if len(names) > 1)


def _derive_model_maps() -> tuple[dict[str, str], dict[str, str]]:
    """``(exact id -> provider, id prefix -> provider)`` from ``PROVIDER_SPECS``.

    Exact: every spec's default and fallback model, routed and bare, when no
    other spec claims the same id (shared open-weight ids such as
    ``openai/gpt-oss-120b`` are left to the router). Prefix: each spec's
    unique LiteLLM route (``groq/``, ``xai/``; a multi-part route such as
    ``fireworks_ai/accounts/fireworks/models/`` also contributes its lead
    segment ``fireworks_ai/``) plus its ``model_prefixes`` families.
    """
    unique = _unique_prefixes()
    claims: dict[str, set[str]] = {}
    for spec in PROVIDER_SPECS.values():
        ids: set[str] = set()
        for model in (spec.default_model, spec.fallback_model):
            ids.add(model.removeprefix(spec.litellm_prefix) if spec.litellm_prefix else model)
            if spec.litellm_prefix in unique:
                ids.add(model)
        for model_id in ids:
            claims.setdefault(model_id, set()).add(spec.name)
    exact = {model_id: next(iter(names)) for model_id, names in claims.items() if len(names) == 1}

    prefixes: dict[str, str] = {}
    for prefix, name in unique.items():
        prefixes[prefix] = name
        lead = prefix.split("/", 1)[0] + "/"
        prefixes.setdefault(lead, name)
    for spec in PROVIDER_SPECS.values():
        for family in spec.model_prefixes:
            prefixes[family] = spec.name
    return exact, prefixes


def _litellm_provider_names() -> dict[str, str]:
    """LiteLLM's ``custom_llm_provider`` names -> nvHive provider names."""
    names = {spec.name: spec.name for spec in PROVIDER_SPECS.values()}
    for prefix, name in _unique_prefixes().items():
        names.setdefault(prefix.split("/", 1)[0], name)
    # LiteLLM's spelling for Cohere's chat surface.
    names.setdefault("cohere_chat", "cohere")
    return names


# Exact model ids and id prefixes the specs claim; ``resolve_provider_from_model``
# checks exact first, then prefixes in this order, then asks LiteLLM.
_MODEL_TO_PROVIDER, _MODEL_PREFIX_TO_PROVIDER = _derive_model_maps()
_LITELLM_TO_NVH: dict[str, str] = _litellm_provider_names()
_SHARED_PREFIXES: frozenset[str] = _shared_prefixes()


@functools.lru_cache(maxsize=512)
def _litellm_provider(model: str) -> str | None:
    """The nvHive provider LiteLLM infers for ``model``.

    ``None`` when LiteLLM does not know the id, names a provider nvHive has
    no spec for (vertex_ai, bedrock, ...), or the id carries a route several
    specs share (``openai/gpt-oss-120b`` is a generic-client id, not OpenAI's).
    LiteLLM is imported lazily (it is heavy) and the answer cached per id;
    ``_litellm_provider.cache_clear()`` resets it.
    """
    if model.startswith(tuple(_SHARED_PREFIXES)):
        return None
    try:
        from litellm import get_llm_provider

        _, provider, _, _ = get_llm_provider(model)
    except Exception:
        return None
    return _LITELLM_TO_NVH.get(provider or "")

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

    # Exact match first: a spec's default or fallback id, routed or bare
    if model in _MODEL_TO_PROVIDER:
        return _MODEL_TO_PROVIDER[model], model

    # Prefix match: a LiteLLM route ("groq/...") or a model family
    # (e.g. "gpt-4o-2024-11-20" → "openai")
    for prefix, provider in _MODEL_PREFIX_TO_PROVIDER.items():
        if model.startswith(prefix):
            return provider, model

    # LiteLLM may still know the id (a served model the specs do not name)
    provider = _litellm_provider(model)
    if provider:
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


def _text_of(content: Any) -> str:
    """The text of an OpenAI message ``content``: a string as is, a parts list's text blocks joined."""
    if isinstance(content, list):
        return " ".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return "" if content is None else str(content)


def openai_messages_to_nvhive_history(
    messages: list[dict[str, Any]],
) -> tuple[str, str | None, list[Any]]:
    """Split an OpenAI messages list into ``(prompt, system_prompt, history)`` for ``Engine.query``.

    The forwarding form of :func:`openai_messages_to_nvhive` (0.46, O12): the
    **last user message** is the prompt (its text, parts flattened), every
    ``system`` message joins the system prompt, and every other message —
    earlier user turns, assistant turns with their ``tool_calls``, ``tool``
    results with their ``tool_call_id``, ``name`` — becomes a
    :class:`~nvh.providers.base.Message` in order, the shape ``Engine.query``'s
    ``history`` takes (``nvh.sdk._messages_to_internal`` keeps those fields;
    the flattener dropped them). ``history`` is ``[]`` for a single-turn
    request, so the engine builds exactly the messages it did before.

    A transcript that ends in an assistant or ``tool`` message (an agent loop
    posting tool results back) has no new user turn: the prompt is ``""`` and
    the whole transcript is the history, in order. The engine appends the
    prompt as a user message, so the caller decides what an empty prompt means.
    """
    from nvh.sdk import _messages_to_internal

    system_parts: list[str] = []
    rest: list[dict[str, Any]] = []
    for msg in messages:
        role = str(msg.get("role") or "user")
        if role == "system":
            text = _text_of(msg.get("content", ""))
            if text:
                system_parts.append(text)
            continue
        rest.append({**msg, "role": role})

    prompt = ""
    if rest and rest[-1]["role"] == "user":
        prompt = _text_of(rest.pop().get("content", ""))

    history = _messages_to_internal(rest) if rest else []
    system_prompt = "\n\n".join(system_parts) if system_parts else None
    return prompt, system_prompt, history


def format_openai_response(
    content: str,
    model: str,
    provider: str,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    finish_reason: str = "stop",
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build an OpenAI-format chat completion response dict.

    ``tool_calls`` (the wire shape ``CompletionResponse.tool_calls`` carries:
    ``{id, type: "function", function: {name, arguments}}``) is written to
    ``message.tool_calls`` and the choice finishes with ``"tool_calls"``, as
    an OpenAI client expects before it runs the tools; ``content`` is then
    ``null`` when the model said nothing beside the calls.
    """
    now = int(time.time())
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = list(tool_calls)
        message["content"] = content or None
        finish_reason = "tool_calls"
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": now,
        "model": model,
        "system_fingerprint": f"nvhive-{provider}",
        "choices": [
            {
                "index": 0,
                "message": message,
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


@_terminal_resource_pause()
async def openai_stream_generator(
    engine: Any,
    prompt: str,
    provider_override: str | None,
    model_override: str | None,
    system_prompt: str | None,
    temperature: float | None,
    max_tokens: int | None,
    requested_model: str,
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    include_usage: bool = False,
    history: list[Message] | None = None,
) -> AsyncGenerator[bytes, None]:
    """Yield SSE bytes in OpenAI streaming format.

    Produces ``data: {...}`` lines followed by ``data: [DONE]``.

    ``tools`` / ``tool_choice`` (the OpenAI function-calling shape) are handed
    to the provider's ``stream``; when its final chunk carries native
    ``tool_calls`` (the way the Wizard's ``_filtered_token_stream`` receives
    them) one ``delta.tool_calls`` chunk goes out before a finish chunk with
    ``finish_reason: "tool_calls"``. ``include_usage`` (``stream_options``)
    adds the usage-only chunk (``choices: []``) after the finish chunk and
    before ``[DONE]``. ``history`` (earlier turns as ``Message`` objects, the
    shape ``Engine.query(history=)`` takes) goes to the provider between the
    system prompt and the final user message, so a streamed multi-turn
    request sees the same messages the non-stream path builds (0.46 §5,
    O12). All three default to today's behaviour.
    """
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

    # The hard stop the non-stream path applies, before any provider is touched.
    try:
        await _engine_hook(engine, "_check_budget")
    except BudgetExceededError as exc:
        error_chunk = {
            "error": {
                "message": str(exc),
                "type": "budget_exceeded",
                "code": "budget_exceeded",
            }
        }
        yield f"data: {json.dumps(error_chunk)}\n\ndata: [DONE]\n\n".encode()
        return

    provider = engine.registry.get(decision.provider)
    messages: list[Message] = []
    if sys_prompt:
        messages.append(Message(role="system", content=sys_prompt))
    if history:
        messages.extend(history)
    if prompt or not history:
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

    aiter = None
    finish_chunk = None
    last_chunk: StreamChunk | None = None
    failed = False
    started = time.monotonic()
    stream_kwargs: dict[str, Any] = {}
    if tools:
        stream_kwargs["tools"] = tools
        if tool_choice is not None:
            stream_kwargs["tool_choice"] = tool_choice
    try:
        aiter = provider.stream(
            messages=messages,
            model=decision.model or None,
            temperature=temp,
            max_tokens=max_tok,
            system_prompt=sys_prompt,
            **stream_kwargs,
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

            last_chunk = chunk
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
                finish_reason = chunk.finish_reason.value if chunk.finish_reason else "stop"
                native_calls = getattr(chunk, "tool_calls", None)
                if isinstance(native_calls, list) and native_calls:
                    tool_call_chunk = {
                        "id": chat_id,
                        "object": "chat.completion.chunk",
                        "created": now,
                        "model": effective_model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"tool_calls": _stream_tool_call_deltas(native_calls)},
                                "logprobs": None,
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(tool_call_chunk)}\n\n".encode()
                    finish_reason = "tool_calls"
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
                            "finish_reason": finish_reason,
                        }
                    ],
                }
                break

    except Exception as exc:
        failed = True
        error_chunk = {
            "error": {
                "message": str(exc),
                "type": "provider_error",
                "code": "stream_error",
            }
        }
        yield f"data: {json.dumps(error_chunk)}\n\n".encode()
    finally:
        await _close_stream(aiter)

    if not failed:
        # Spend is recorded before the finish chunk goes out, so a consumer
        # that leaves on the last token still leaves a ledger row behind.
        await _log_stream_spend(
            engine, last_chunk, provider=decision.provider or "",
            model=decision.model or requested_model or "", started=started,
        )

    if finish_chunk is not None:
        yield f"data: {json.dumps(finish_chunk)}\n\n".encode()
    if include_usage and not failed:
        usage_chunk = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": now,
            "model": effective_model,
            "choices": [],
            "usage": _stream_usage_dict(last_chunk),
        }
        yield f"data: {json.dumps(usage_chunk)}\n\n".encode()
    yield b"data: [DONE]\n\n"


def _stream_tool_call_deltas(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Wire-shape tool calls as the ``delta.tool_calls`` entries of one streaming chunk.

    Each entry gains its ``index``; ``function.arguments`` is the JSON string
    the wire shape already carries (a dict is serialised), and a missing
    ``id`` becomes ``call_<n>``, the fallback the adapters use.
    """
    deltas: list[dict[str, Any]] = []
    for index, call in enumerate(calls):
        call = call if isinstance(call, dict) else {}
        function = call.get("function")
        function = function if isinstance(function, dict) else {}
        arguments = function.get("arguments", "")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, default=str)
        deltas.append({
            "index": index,
            "id": str(call.get("id") or f"call_{index}"),
            "type": "function",
            "function": {"name": str(function.get("name") or ""), "arguments": arguments},
        })
    return deltas


def _stream_usage_dict(chunk: StreamChunk | None) -> dict[str, int]:
    """The OpenAI ``usage`` object for a finished stream: the final chunk's counts, zeros if none."""
    usage = chunk.usage if chunk is not None and chunk.usage is not None else None
    prompt_tokens = int(usage.input_tokens or 0) if usage is not None else 0
    completion_tokens = int(usage.output_tokens or 0) if usage is not None else 0
    total = int(usage.total_tokens or 0) if usage is not None else 0
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total or prompt_tokens + completion_tokens,
    }


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


@_terminal_resource_pause()
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
        "usage": {
            "prompt_tokens": result.total_usage.input_tokens,
            "completion_tokens": result.total_usage.output_tokens,
            "total_tokens": result.total_usage.total_tokens,
        },
    }
    yield f"data: {json.dumps(finish_chunk)}\n\n".encode()
    yield b"data: [DONE]\n\n"


@_terminal_resource_pause()
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
        "usage": {
            "prompt_tokens": result.total_usage.input_tokens,
            "completion_tokens": result.total_usage.output_tokens,
            "total_tokens": result.total_usage.total_tokens,
        },
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

    # The real models: the registry's catalog rows for each enabled provider
    # (capabilities.yaml merged with LiteLLM's table), retired rows hidden.
    # The adapters themselves carry no model attributes, and a live probe
    # has no place in this synchronous list.
    for provider_name in (registry.list_enabled() if hasattr(registry, "list_enabled") else []):
        try:
            rows = registry.list_models(provider=provider_name) if hasattr(registry, "list_models") else []
        except Exception:
            continue
        for row in rows:
            if getattr(row, "status", "active") == "retired":
                continue
            model_objects.append({
                "id": row.model_id,
                "object": "model",
                "created": now,
                "owned_by": provider_name,
            })

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
#   ANTHROPIC_BASE_URL=http://localhost:8000/v1/anthropic
#   ANTHROPIC_AUTH_TOKEN=<HIVE_API_KEY>   # any value in open mode
#
# The SDKs send ANTHROPIC_API_KEY as ``x-api-key``, which ``require_auth``
# does not read; ``ANTHROPIC_AUTH_TOKEN`` is the bearer it checks. nvHive
# handles routing, fallback, and multi-provider orchestration.
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


@_terminal_resource_pause(anthropic=True)
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

    # The hard stop the non-stream path applies, before any provider is
    # touched. An Anthropic stream that fails before message_start ends on
    # the error event alone, as the provider_unavailable path above does.
    try:
        await _engine_hook(engine, "_check_budget")
    except BudgetExceededError as exc:
        error_event = {
            "type": "error",
            "error": {
                "type": "budget_exceeded",
                "message": str(exc),
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
    last_chunk: StreamChunk | None = None
    failed = False
    started = time.monotonic()
    # Per-chunk stall timeout — keeps Anthropic-compatible clients from
    # hanging when a backing provider stalls mid-stream.
    CHUNK_STALL_TIMEOUT = 45.0  # noqa: N806 — local constant, intentional uppercase
    aiter = None
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

            last_chunk = chunk
            if chunk.usage:
                total_output_tokens = chunk.usage.output_tokens

    except Exception as exc:
        failed = True
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
    finally:
        await _close_stream(aiter)

    # Spend is recorded before the closing events go out, so a consumer that
    # leaves on the last token still leaves a ledger row behind. A stream
    # that ended on the error event above never reaches this line; the flag
    # keeps that true even if the ``return`` moves.
    if not failed:
        await _log_stream_spend(
            engine, last_chunk, provider=decision.provider or "",
            model=decision.model or requested_model or "", started=started,
        )

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
