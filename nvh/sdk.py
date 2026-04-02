"""NVHive Python SDK — use NVHive from your Python code.

Usage:
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
"""

from __future__ import annotations

import asyncio

from nvh.config.settings import load_config
from nvh.core.engine import Engine
from nvh.providers.base import CompletionResponse

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
