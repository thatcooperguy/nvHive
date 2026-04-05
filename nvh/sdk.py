"""NVHive Python SDK — use NVHive from your Python code.

User-facing API:
    from nvh import ask, convene, poll, safe

    # Simple query
    response = await ask("What is machine learning?")
    print(response.content)

    # Ask a specific advisor
    response = await ask("Debug this code", advisor="anthropic")

    # Convene a council
    result = await convene("Should we use Rust?", agents=True)
    print(result.synthesis.content)

    # Poll all advisors
    results = await poll("Write a sort function")
    for name, resp in results.items():
        print(f"{name}: {resp.content[:100]}")

    # Safe mode (local only)
    response = await safe("Analyze my salary data")

    # Synchronous versions
    from nvh import ask_sync, convene_sync
    response = ask_sync("Quick question")

Infrastructure API (for tool builders):
    import nvh

    # Drop-in replacement for OpenAI — 3 lines of code
    response = await nvh.complete([
        {"role": "user", "content": "Explain quicksort"}
    ])
    print(response.content)

    # See where nvHive would route a query
    decision = await nvh.route("Explain quicksort")
    print(decision)  # {'provider': 'groq', 'model': 'llama-3.3-70b', 'reason': '...'}

    # Check provider health on startup
    status = await nvh.health()
    print(status)  # {'groq': {'healthy': True, ...}, ...}

    # Stream responses
    async for chunk in nvh.stream([{"role": "user", "content": "Hello"}]):
        print(chunk.delta, end="")
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from nvh.config.settings import load_config
from nvh.core.engine import Engine
from nvh.providers.base import CompletionResponse, HealthStatus, Message, StreamChunk

# Module-level engine (lazy init)
_engine: Engine | None = None


async def _get_engine() -> Engine:
    global _engine
    if _engine is None:
        config = load_config()
        _engine = Engine(config=config)
        await _engine.initialize()
    return _engine


async def ask(
    prompt: str,
    advisor: str | None = None,
    model: str | None = None,
    system: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> CompletionResponse:
    """Ask a single advisor a question.

    Args:
        prompt: The question
        advisor: Specific advisor (e.g., "openai", "groq"). None = auto-route.
        model: Specific model. None = advisor's default.
        system: System prompt
        temperature: 0.0-2.0
        max_tokens: Max response length

    Returns:
        CompletionResponse with .content, .provider, .model, .usage, .cost_usd
    """
    engine = await _get_engine()
    return await engine.query(
        prompt=prompt,
        provider=advisor,
        model=model,
        system_prompt=system,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=False,
    )


async def convene(
    prompt: str,
    agents: bool = True,
    cabinet: str | None = None,
    strategy: str | None = None,
    num_agents: int | None = None,
    system: str | None = None,
):
    """Convene a council of AI agents.

    Args:
        prompt: The question for the council
        agents: Auto-generate expert agents (default True)
        cabinet: Named cabinet ("engineering", "executive", etc.)
        strategy: Consensus strategy ("weighted_consensus", "majority_vote", "best_of")
        num_agents: Number of agents to generate
        system: System prompt

    Returns:
        CouncilResponse with .synthesis, .member_responses, .agents_used
    """
    engine = await _get_engine()
    return await engine.run_council(
        prompt=prompt,
        auto_agents=agents,
        agent_preset=cabinet,
        strategy=strategy,
        num_agents=num_agents,
        system_prompt=system,
        synthesize=True,
    )


async def poll(
    prompt: str,
    advisors: list[str] | None = None,
    system: str | None = None,
) -> dict[str, CompletionResponse]:
    """Poll multiple advisors and compare responses.

    Args:
        prompt: The question
        advisors: List of advisor names. None = all enabled.
        system: System prompt

    Returns:
        Dict of {advisor_name: CompletionResponse}
    """
    engine = await _get_engine()
    return await engine.compare(
        prompt=prompt,
        providers=advisors,
        system_prompt=system,
    )


async def safe(
    prompt: str,
    model: str | None = None,
    system: str | None = None,
) -> CompletionResponse:
    """Safe mode — local only, no data leaves your machine.

    Args:
        prompt: The question (processed locally only)
        model: Local model to use
        system: System prompt

    Returns:
        CompletionResponse from local Ollama
    """
    engine = await _get_engine()
    return await engine.query(
        prompt=prompt,
        provider="ollama",
        model=model,
        system_prompt=system,
        stream=False,
        privacy=True,
    )


async def quick(prompt: str) -> CompletionResponse:
    """Quick answer from the fastest/cheapest advisor."""
    engine = await _get_engine()
    return await engine.query(
        prompt=prompt,
        strategy="cheapest",
        stream=False,
    )


# Synchronous wrappers for non-async code
def ask_sync(prompt: str, **kwargs) -> CompletionResponse:
    """Synchronous version of ask()."""
    return asyncio.run(ask(prompt, **kwargs))

def convene_sync(prompt: str, **kwargs):
    """Synchronous version of convene()."""
    return asyncio.run(convene(prompt, **kwargs))

def poll_sync(prompt: str, **kwargs):
    """Synchronous version of poll()."""
    return asyncio.run(poll(prompt, **kwargs))

def safe_sync(prompt: str, **kwargs) -> CompletionResponse:
    """Synchronous version of safe()."""
    return asyncio.run(safe(prompt, **kwargs))

def quick_sync(prompt: str) -> CompletionResponse:
    """Synchronous version of quick()."""
    return asyncio.run(quick(prompt))


# =========================================================================
# Infrastructure API — for tool builders embedding nvHive
# =========================================================================


def _messages_to_internal(messages: list[dict[str, Any]]) -> list[Message]:
    """Convert OpenAI-format message dicts to internal Message objects.

    Args:
        messages: List of dicts with ``role`` and ``content`` keys,
            matching the OpenAI chat completions format.

    Returns:
        List of :class:`Message` instances.
    """
    return [
        Message(
            role=m["role"],
            content=m.get("content", ""),
            name=m.get("name"),
            tool_calls=m.get("tool_calls"),
            tool_call_id=m.get("tool_call_id"),
        )
        for m in messages
    ]


async def route(
    prompt: str,
    strategy: str = "best",
    **kwargs: Any,
) -> dict[str, str]:
    """Return the routing decision without executing a query.

    Inspect where nvHive *would* send a prompt so tool builders can
    display routing info, log decisions, or override before calling
    :func:`complete`.

    Args:
        prompt: The user query to route.
        strategy: Routing strategy (``"best"``, ``"cheapest"``, etc.).
        **kwargs: Extra arguments forwarded to the routing engine.

    Returns:
        Dict with ``provider``, ``model``, and ``reason`` keys.

    Example::

        import nvh

        decision = await nvh.route("Explain quicksort")
        print(decision)
        # {'provider': 'groq', 'model': 'llama-3.3-70b-versatile', 'reason': '...'}
    """
    engine = await _get_engine()
    decision = engine.router.route(query=prompt, strategy=strategy, **kwargs)
    return {
        "provider": decision.provider,
        "model": decision.model,
        "reason": decision.reason,
    }


async def complete(
    messages: list[dict[str, Any]],
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    **kwargs: Any,
) -> CompletionResponse:
    """Send a chat completion using OpenAI-compatible message format.

    Drop-in replacement: swap ``openai.chat.completions.create()`` for
    ``nvh.complete()`` and get multi-provider routing, failover, and
    budget enforcement for free.

    Args:
        messages: List of message dicts (``{"role": "...", "content": "..."}``).
        provider: Pin to a specific provider. ``None`` = auto-route.
        model: Pin to a specific model. ``None`` = provider default.
        temperature: Sampling temperature (0.0-2.0).
        max_tokens: Maximum response tokens.
        **kwargs: Extra arguments forwarded to :meth:`Engine.query`.

    Returns:
        :class:`CompletionResponse` with ``.content``, ``.provider``,
        ``.model``, ``.usage``, and ``.cost_usd``.

    Example::

        import nvh

        response = await nvh.complete([
            {"role": "user", "content": "Explain quicksort"}
        ])
        print(response.content)
    """
    engine = await _get_engine()
    internal_msgs = _messages_to_internal(messages)

    # Extract the user prompt (last user message) for routing
    user_prompt = ""
    system_prompt = None
    for msg in reversed(internal_msgs):
        if msg.role == "user" and not user_prompt:
            user_prompt = msg.content
        if msg.role == "system" and system_prompt is None:
            system_prompt = msg.content

    return await engine.query(
        prompt=user_prompt,
        provider=provider,
        model=model,
        system_prompt=system_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=False,
        **kwargs,
    )


def complete_sync(
    messages: list[dict[str, Any]],
    **kwargs: Any,
) -> CompletionResponse:
    """Synchronous version of :func:`complete`.

    Example::

        import nvh

        response = nvh.complete_sync([
            {"role": "user", "content": "Explain quicksort"}
        ])
        print(response.content)
    """
    return asyncio.run(complete(messages, **kwargs))


async def stream(
    messages: list[dict[str, Any]],
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    **kwargs: Any,
) -> AsyncIterator[StreamChunk]:
    """Stream a chat completion, yielding chunks as they arrive.

    Async generator that yields :class:`StreamChunk` objects with
    ``.delta`` (new text), ``.accumulated_content``, and ``.is_final``.

    Args:
        messages: List of message dicts (``{"role": "...", "content": "..."}``).
        provider: Pin to a specific provider. ``None`` = auto-route.
        model: Pin to a specific model. ``None`` = provider default.
        temperature: Sampling temperature (0.0-2.0).
        max_tokens: Maximum response tokens.
        **kwargs: Extra arguments forwarded to the provider.

    Yields:
        :class:`StreamChunk` with ``.delta``, ``.accumulated_content``,
        ``.is_final``, ``.provider``, and ``.model``.

    Example::

        import nvh

        async for chunk in nvh.stream([
            {"role": "user", "content": "Write a poem"}
        ]):
            print(chunk.delta, end="", flush=True)
    """
    engine = await _get_engine()
    internal_msgs = _messages_to_internal(messages)

    # Extract user prompt and system prompt for routing
    user_prompt = ""
    system_prompt = None
    for msg in reversed(internal_msgs):
        if msg.role == "user" and not user_prompt:
            user_prompt = msg.content
        if msg.role == "system" and system_prompt is None:
            system_prompt = msg.content

    temp = temperature if temperature is not None else engine.config.defaults.temperature
    max_tok = max_tokens or engine.config.defaults.max_tokens

    # Route to pick provider/model
    decision = engine.router.route(
        query=user_prompt,
        provider_override=provider,
        model_override=model,
    )

    provider_instance = engine.registry.get(decision.provider)
    async for chunk in provider_instance.stream(
        messages=internal_msgs,
        model=decision.model or None,
        temperature=temp,
        max_tokens=max_tok,
        system_prompt=system_prompt,
        **kwargs,
    ):
        yield chunk


async def health() -> dict[str, dict[str, Any]]:
    """Check the health of all enabled providers.

    Returns a dict keyed by provider name, each containing health
    information (``healthy``, ``latency_ms``, ``error``,
    ``models_available``).  Tool builders typically call this on startup
    to verify connectivity.

    Returns:
        Dict mapping provider names to health status dicts.

    Example::

        import nvh

        status = await nvh.health()
        for name, info in status.items():
            icon = "ok" if info["healthy"] else "FAIL"
            print(f"  {icon}  {name}: {info.get('latency_ms', '?')}ms")
    """
    engine = await _get_engine()
    enabled = engine.registry.list_enabled()
    results: dict[str, dict[str, Any]] = {}

    async def _check(name: str) -> tuple[str, dict[str, Any]]:
        try:
            prov = engine.registry.get(name)
            hs: HealthStatus = await prov.health_check()
            return name, {
                "healthy": hs.healthy,
                "latency_ms": hs.latency_ms,
                "error": hs.error,
                "models_available": hs.models_available,
            }
        except Exception as exc:
            return name, {
                "healthy": False,
                "latency_ms": None,
                "error": str(exc),
                "models_available": 0,
            }

    checks = await asyncio.gather(*[_check(n) for n in enabled])
    for name, info in checks:
        results[name] = info
    return results


def health_sync() -> dict[str, dict[str, Any]]:
    """Synchronous version of :func:`health`.

    Example::

        import nvh

        status = nvh.health_sync()
        print(status)
    """
    return asyncio.run(health())
