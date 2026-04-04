"""NVHive MCP Server — expose nvHive tools to Claude Code, Cursor, OpenClaw, and NemoClaw.

Model Context Protocol server that gives any MCP client access to:
- Smart routing across 23 LLM providers (63 models, 25 free)
- Council consensus (multi-model synthesis)
- Throwdown analysis (two-pass deep analysis)
- Provider status and GPU info

Install:
    pip install "mcp[cli]"

Run standalone:
    python -m nvh.mcp_server
    # or
    mcp dev nvh/mcp_server.py

Register with Claude Code:
    claude mcp add nvhive python -m nvh.mcp_server

Register with OpenClaw (openclaw.json):
    {
      "mcpServers": {
        "nvhive": {
          "command": "python",
          "args": ["-m", "nvh.mcp_server"]
        }
      }
    }
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Lazy imports to keep startup fast ��� MCP servers should launch quickly via stdio.
_engine = None
_engine_lock = asyncio.Lock()

# Default timeout for LLM operations (seconds)
_QUERY_TIMEOUT = 120
_COUNCIL_TIMEOUT = 300


async def _get_engine():
    """Lazy-initialize the nvHive engine (thread-safe)."""
    global _engine
    if _engine is not None:
        return _engine
    async with _engine_lock:
        if _engine is not None:
            return _engine
        try:
            from nvh.core.engine import Engine
            _engine = Engine()
            await _engine.initialize()
        except Exception as e:
            logger.error("Failed to initialize nvHive engine: %s", e)
            raise RuntimeError(
                f"nvHive engine failed to start: {e}\n"
                "Troubleshooting:\n"
                "  1. Check config:  nvh config init\n"
                "  2. Test providers: nvh test --quick\n"
                "  3. Check logs:    ~/.hive/nvhive.log"
            ) from e
    return _engine


def _format_response(resp: Any) -> str:
    """Format a CompletionResponse into readable text."""
    parts = [resp.content]
    if resp.provider:
        parts.append(f"\n\n---\nProvider: {resp.provider}")
    if resp.model:
        parts.append(f" | Model: {resp.model}")
    if resp.usage:
        parts.append(f" | Tokens: {resp.usage.total_tokens}")
    if resp.cost_usd:
        parts.append(f" | Cost: ${resp.cost_usd:.6f}")
    if resp.latency_ms:
        parts.append(f" | Latency: {resp.latency_ms}ms")
    return "".join(parts)


def _format_council_response(result: Any) -> str:
    """Format a CouncilResponse into readable text."""
    parts = []

    # Synthesis first
    if result.synthesis:
        content = (
            result.synthesis.content
            if hasattr(result.synthesis, "content")
            else str(result.synthesis)
        )
        parts.append(f"## Council Synthesis\n\n{content}")

    # Individual member responses
    if result.member_responses:
        parts.append("\n\n## Individual Responses\n")
        for provider, resp in result.member_responses.items():
            content = resp.content if hasattr(resp, "content") else str(resp)
            parts.append(f"\n### {provider}\n{content[:500]}{'...' if len(content) > 500 else ''}")

    # Confidence
    if getattr(result, "confidence_score", None) is not None:
        pct = int(result.confidence_score * 100)
        summary = getattr(result, "agreement_summary", None) or ""
        confidence_text = f"Confidence: {pct}%"
        if summary:
            confidence_text += f" — {summary}"
        parts.append(f"\n\n**{confidence_text}**")

    # Metadata
    meta = []
    if result.strategy:
        meta.append(f"Strategy: {result.strategy}")
    if result.total_cost_usd:
        meta.append(f"Total cost: ${result.total_cost_usd:.6f}")
    if result.total_latency_ms:
        meta.append(f"Total latency: {result.total_latency_ms}ms")
    if result.agents_used:
        meta.append(f"Agents: {', '.join(result.agents_used)}")
    if meta:
        parts.append(f"\n\n---\n{' | '.join(meta)}")

    return "".join(parts)


def create_server():
    """Create and configure the MCP server with nvHive tools."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        raise ImportError(
            'MCP SDK not installed. Install with: pip install "mcp[cli]"\n'
            "Then run: python -m nvh.mcp_server"
        )

    mcp = FastMCP(
        "nvhive",
        description=(
            "NVHive ��� Multi-LLM orchestration with smart routing, "
            "council consensus, and throwdown analysis across 23 providers (63 models, 25 free)."
        ),
    )

    # ------------------------------------------------------------------
    # Input validation helpers
    # ------------------------------------------------------------------

    valid_strategies = {"weighted_consensus", "majority_vote", "best_of"}
    valid_cabinets = {
        "executive", "engineering", "security_review", "code_review",
        "product", "data", "full_board", "homework_help", "code_tutor",
        "essay_review", "study_group", "exam_prep",
    }

    def _validate_prompt(prompt: str) -> str:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("Prompt cannot be empty.")
        if len(prompt) > 500_000:
            raise ValueError(f"Prompt too long ({len(prompt)} chars). Maximum is 500,000.")
        return prompt

    def _validate_temperature(temperature: float) -> float:
        if not (0.0 <= temperature <= 2.0):
            raise ValueError(f"Temperature must be 0.0–2.0, got {temperature}.")
        return temperature

    def _validate_max_tokens(max_tokens: int) -> int:
        if not (1 <= max_tokens <= 200_000):
            raise ValueError(f"max_tokens must be 1–200,000, got {max_tokens}.")
        return max_tokens

    def _error_response(e: Exception, operation: str) -> str:
        """Format an error into a helpful, actionable message."""
        from nvh.providers.base import (
            AuthenticationError,
            ContentFilterError,
            InsufficientQuotaError,
            ProviderUnavailableError,
            RateLimitError,
            TokenLimitError,
        )

        msg = str(e)
        if isinstance(e, AuthenticationError):
            return (
                f"Authentication failed for {operation}: {msg}\n\n"
                "Fix: Run `nvh setup` to reconfigure your API key, "
                "or set the provider's API key as an environment variable."
            )
        if isinstance(e, RateLimitError):
            return (
                f"Rate limit hit during {operation}: {msg}\n\n"
                "nvHive will auto-retry with a different provider. "
                "Or wait a moment and try again."
            )
        if isinstance(e, InsufficientQuotaError):
            return (
                f"Quota exceeded during {operation}: {msg}\n\n"
                "Options:\n"
                "  - Use a free provider: set advisor='groq' or advisor='github'\n"
                "  - Use local inference: use the ask_safe tool instead\n"
                "  - Check budget: use the status tool"
            )
        if isinstance(e, TokenLimitError):
            return (
                f"Input too long for {operation}: {msg}\n\n"
                "Try shortening the prompt or using a model with a larger context window."
            )
        if isinstance(e, ContentFilterError):
            return f"Content filtered during {operation}: {msg}"
        if isinstance(e, ProviderUnavailableError):
            return (
                f"Provider unavailable for {operation}: {msg}\n\n"
                "Check provider status with the status tool, "
                "or try a different advisor."
            )
        if isinstance(e, asyncio.TimeoutError):
            return (
                f"Timed out during {operation}.\n\n"
                "The operation took too long. Try:\n"
                "  - A faster provider (advisor='groq')\n"
                "  - A shorter prompt\n"
                "  - Fewer council members"
            )
        if isinstance(e, (RuntimeError, ValueError)):
            return f"Error in {operation}: {msg}"

        # Unknown error — log full traceback, show summary to client
        logger.exception("Unexpected error in MCP tool %s", operation)
        return (
            f"Unexpected error during {operation}: {msg}\n\n"
            "Check that nvHive is configured correctly: nvh status\n"
            "If this persists, check logs at ~/.hive/nvhive.log"
        )

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    @mcp.tool()
    async def ask(
        prompt: str,
        advisor: str = "",
        model: str = "",
        temperature: float = 1.0,
        max_tokens: int = 4096,
    ) -> str:
        """Ask a question using NVHive's smart router.

        Routes to the best available LLM provider based on query type,
        cost, speed, and reliability. Supports 23 providers and 63 models (25 free).

        Args:
            prompt: The question or task to send to the LLM.
            advisor: Specific advisor/provider to use (e.g. "openai", "anthropic", "groq").
                    Leave empty for smart routing.
            model: Specific model to use (e.g. "gpt-4o", "claude-sonnet-4").
                  Leave empty for auto-selection.
            temperature: Sampling temperature (0.0-2.0). Default 1.0.
            max_tokens: Maximum response tokens (1-200000). Default 4096.
        """
        try:
            prompt = _validate_prompt(prompt)
            temperature = _validate_temperature(temperature)
            max_tokens = _validate_max_tokens(max_tokens)
        except ValueError as e:
            return str(e)

        try:
            engine = await _get_engine()
            resp = await asyncio.wait_for(
                engine.query(
                    prompt=prompt,
                    provider=advisor or None,
                    model=model or None,
                    temperature=temperature,
                    max_tokens=max_tokens,
                ),
                timeout=_QUERY_TIMEOUT,
            )
            return _format_response(resp)
        except Exception as e:
            return _error_response(e, "ask")

    @mcp.tool()
    async def ask_safe(prompt: str, model: str = "") -> str:
        """Ask a question using only local models (Ollama).

        Nothing leaves your machine — all inference is local.
        Requires Ollama running with at least one model installed.

        Args:
            prompt: The question or task.
            model: Local model to use (default: auto-select based on GPU).
        """
        try:
            prompt = _validate_prompt(prompt)
        except ValueError as e:
            return str(e)

        try:
            engine = await _get_engine()
            resp = await asyncio.wait_for(
                engine.query(
                    prompt=prompt,
                    provider="ollama",
                    model=model or None,
                ),
                timeout=_QUERY_TIMEOUT,
            )
            return _format_response(resp)
        except Exception as e:
            return _error_response(e, "ask_safe")

    @mcp.tool()
    async def council(
        prompt: str,
        num_members: int = 3,
        strategy: str = "",
        cabinet: str = "",
    ) -> str:
        """Run a council — get consensus from multiple LLMs.

        Dispatches the prompt to multiple LLM providers in parallel,
        then synthesizes their responses into a unified answer.

        Args:
            prompt: The question to present to the council.
            num_members: Number of council members (2-10). Default 3.
            strategy: Consensus strategy: "weighted_consensus", "majority_vote",
                     "best_of". Leave empty for default.
            cabinet: Agent cabinet preset: "executive", "engineering",
                    "security_review", "code_review", "product", "data",
                    "homework_help", "code_tutor", "essay_review".
                    Leave empty for auto-generated agents.
        """
        try:
            prompt = _validate_prompt(prompt)
        except ValueError as e:
            return str(e)

        # Validate num_members with feedback
        if num_members < 2:
            num_members = 2
            logger.info("num_members clamped to minimum of 2")
        elif num_members > 10:
            num_members = 10
            logger.info("num_members clamped to maximum of 10")

        # Validate strategy
        if strategy and strategy not in valid_strategies:
            return (
                f"Invalid strategy '{strategy}'. "
                f"Valid options: {', '.join(sorted(valid_strategies))}"
            )

        # Validate cabinet
        if cabinet and cabinet not in valid_cabinets:
            return (
                f"Invalid cabinet '{cabinet}'. "
                f"Valid options: {', '.join(sorted(valid_cabinets))}\n"
                "Use the list_cabinets tool to see descriptions."
            )

        try:
            engine = await _get_engine()
            result = await asyncio.wait_for(
                engine.run_council(
                    prompt=prompt,
                    num_agents=num_members,
                    auto_agents=not bool(cabinet),
                    agent_preset=cabinet or None,
                    strategy=strategy or None,
                ),
                timeout=_COUNCIL_TIMEOUT,
            )
            return _format_council_response(result)
        except Exception as e:
            return _error_response(e, "council")

    @mcp.tool()
    async def throwdown(prompt: str, cabinet: str = "") -> str:
        """Run a throwdown — two-pass deep analysis with critique.

        First pass: multiple LLMs analyze the question independently.
        Second pass: LLMs critique and refine each other's answers.
        Final: synthesis of all perspectives.

        Args:
            prompt: The question for deep analysis.
            cabinet: Agent cabinet preset. Leave empty for auto-generated.
        """
        try:
            prompt = _validate_prompt(prompt)
        except ValueError as e:
            return str(e)

        if cabinet and cabinet not in valid_cabinets:
            return (
                f"Invalid cabinet '{cabinet}'. "
                f"Valid options: {', '.join(sorted(valid_cabinets))}\n"
                "Use the list_cabinets tool to see descriptions."
            )

        try:
            engine = await _get_engine()
            result = await asyncio.wait_for(
                engine.run_council(
                    prompt=prompt,
                    auto_agents=not bool(cabinet),
                    agent_preset=cabinet or None,
                    strategy="throwdown",
                ),
                timeout=_COUNCIL_TIMEOUT,
            )
            return _format_council_response(result)
        except Exception as e:
            return _error_response(e, "throwdown")

    @mcp.tool()
    async def status() -> str:
        """Get NVHive system status.

        Returns enabled providers, GPU info, budget status,
        and available models.
        """
        try:
            engine = await _get_engine()
        except Exception as e:
            return _error_response(e, "status")

        parts = ["## NVHive Status\n"]

        # Providers
        enabled = engine.registry.list_enabled() if hasattr(engine.registry, "list_enabled") else []
        parts.append(f"**Providers enabled:** {len(enabled)}")
        if enabled:
            parts.append(f"  {', '.join(sorted(enabled))}")
        else:
            parts.append("  (none — run `nvh setup` to configure providers)")

        # GPU
        try:
            from nvh.utils.gpu import detect_gpus
            gpus = detect_gpus()
            if gpus:
                parts.append(f"\n**GPU:** {gpus[0].name} ({gpus[0].vram_total_mb}MB VRAM)")
            else:
                parts.append("\n**GPU:** None detected")
        except Exception:
            parts.append("\n**GPU:** Detection unavailable")

        # Budget
        if hasattr(engine, "config") and hasattr(engine.config, "budget"):
            budget = engine.config.budget
            if hasattr(budget, "daily_limit_usd") and budget.daily_limit_usd:
                parts.append(f"\n**Budget:** ${budget.daily_limit_usd}/day")

        return "\n".join(parts)

    @mcp.tool()
    async def list_advisors() -> str:
        """List all available LLM advisors/providers and their status.

        Shows which providers are enabled, have API keys configured,
        and their free tier limits.
        """
        try:
            engine = await _get_engine()
        except Exception as e:
            return _error_response(e, "list_advisors")

        enabled = engine.registry.list_enabled() if hasattr(engine.registry, "list_enabled") else []

        if not enabled:
            return (
                "No advisors enabled.\n\n"
                "Get started:\n"
                "  1. Run `nvh setup` to configure providers\n"
                "  2. Or set an API key: export GROQ_API_KEY=gsk_...\n"
                "  3. Then restart the MCP server"
            )

        lines = ["## Available Advisors\n"]
        lines.append("| Advisor | Status | Free Tier |")
        lines.append("|---------|--------|-----------|")

        # Enrich with free tier info from advisor profiles
        try:
            from nvh.core.advisor_profiles import ADVISOR_PROFILES as profiles  # noqa: N811
        except ImportError:
            profiles = {}

        for name in sorted(enabled):
            profile = profiles.get(name)
            free = "Yes" if (profile and profile.has_free_tier) else "—"
            lines.append(f"| {name} | Enabled | {free} |")

        return "\n".join(lines)

    @mcp.tool()
    async def list_cabinets() -> str:
        """List available agent cabinet presets for council mode.

        Cabinets are predefined groups of expert personas
        optimized for specific tasks.
        """
        from nvh.core.agents import list_presets

        presets = list_presets()
        lines = ["## Agent Cabinets\n"]
        lines.append("| Cabinet | Description |")
        lines.append("|---------|-------------|")

        cabinet_descriptions = {
            "executive": "CEO, CTO, CFO, CMO, COO — strategic decisions",
            "engineering": "Architect, Backend, Frontend, DevOps, QA — technical decisions",
            "security_review": "Security analysts — vulnerability assessment",
            "code_review": "Senior engineers — code quality review",
            "product": "Product managers, designers, analysts — product decisions",
            "data": "Data scientists, ML engineers — data analysis",
            "full_board": "All expert personas",
            "homework_help": "Tutors across subjects — student help",
            "code_tutor": "Programming mentors — learn to code",
            "essay_review": "Writing coaches — improve essays",
            "study_group": "Study partners — exam prep",
            "exam_prep": "Test prep specialists — practice questions",
        }

        for preset in presets:
            desc = cabinet_descriptions.get(preset, "Expert persona group")
            lines.append(f"| {preset} | {desc} |")

        return "\n".join(lines)

    return mcp


def main():
    """Run the NVHive MCP server."""
    server = create_server()
    server.run()


if __name__ == "__main__":
    main()
