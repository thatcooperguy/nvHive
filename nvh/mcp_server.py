"""NVHive MCP Server — expose nvHive tools to Claude Code, Cursor, OpenClaw, and NemoClaw.

Model Context Protocol server that gives any MCP client access to:
- Smart routing across 22 LLM providers
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

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Lazy imports to keep startup fast — MCP servers should launch quickly via stdio.
_engine = None


async def _get_engine():
    """Lazy-initialize the nvHive engine."""
    global _engine
    if _engine is None:
        from nvh.core.engine import Engine
        _engine = Engine()
        await _engine.initialize()
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
            "NVHive — Multi-LLM orchestration with smart routing, "
            "council consensus, and throwdown analysis across 22 providers."
        ),
    )

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
        cost, speed, and reliability. Supports 22 providers and 63 models.

        Args:
            prompt: The question or task to send to the LLM.
            advisor: Specific advisor/provider to use (e.g. "openai", "anthropic", "groq").
                    Leave empty for smart routing.
            model: Specific model to use (e.g. "gpt-4o", "claude-sonnet-4").
                  Leave empty for auto-selection.
            temperature: Sampling temperature (0.0-2.0). Default 1.0.
            max_tokens: Maximum response tokens. Default 4096.
        """
        engine = await _get_engine()
        try:
            resp = await engine.query(
                prompt=prompt,
                provider=advisor or None,
                model=model or None,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return _format_response(resp)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    async def ask_safe(prompt: str, model: str = "") -> str:
        """Ask a question using only local models (Ollama).

        Nothing leaves your machine — all inference is local.
        Requires Ollama running with at least one model installed.

        Args:
            prompt: The question or task.
            model: Local model to use (default: auto-select based on GPU).
        """
        engine = await _get_engine()
        try:
            resp = await engine.query(
                prompt=prompt,
                provider="ollama",
                model=model or None,
            )
            return _format_response(resp)
        except Exception as e:
            return f"Error: {e}"

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
        engine = await _get_engine()
        try:
            result = await engine.run_council(
                prompt=prompt,
                num_agents=max(2, min(num_members, 10)),
                auto_agents=not bool(cabinet),
                agent_preset=cabinet or None,
                strategy=strategy or None,
            )
            return _format_council_response(result)
        except Exception as e:
            return f"Error: {e}"

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
        engine = await _get_engine()
        try:
            result = await engine.run_council(
                prompt=prompt,
                auto_agents=not bool(cabinet),
                agent_preset=cabinet or None,
                strategy="throwdown",
            )
            return _format_council_response(result)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    async def status() -> str:
        """Get NVHive system status.

        Returns enabled providers, GPU info, budget status,
        and available models.
        """
        engine = await _get_engine()

        parts = ["## NVHive Status\n"]

        # Providers
        enabled = engine.registry.list_enabled() if hasattr(engine.registry, "list_enabled") else []
        parts.append(f"**Providers enabled:** {len(enabled)}")
        if enabled:
            parts.append(f"  {', '.join(enabled)}")

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
        engine = await _get_engine()
        enabled = engine.registry.list_enabled() if hasattr(engine.registry, "list_enabled") else []

        if not enabled:
            return "No advisors enabled. Run `nvh setup` to configure providers."

        lines = ["## Available Advisors\n"]
        lines.append("| Advisor | Status |")
        lines.append("|---------|--------|")
        for name in sorted(enabled):
            lines.append(f"| {name} | Enabled |")

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
