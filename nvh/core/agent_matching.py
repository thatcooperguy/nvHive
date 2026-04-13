"""Smart agent-to-LLM matching — connects each expert persona to the
best available LLM for that persona's specialty.

When nvhive generates expert agents (e.g., "Database Expert",
"Security Engineer", "Frontend Developer"), this module matches each
agent to the LLM provider that scores highest for that domain based
on the learning engine's real per-provider per-task-type quality data.

If no learning data exists yet, falls back to a static capability
map based on known provider strengths.

Usage:
    from nvh.core.agent_matching import match_agents_to_providers
    assignments = match_agents_to_providers(agents, engine)
    # → {"Database Expert": ("openai", "gpt-4o"),
    #    "Security Engineer": ("anthropic", "claude-sonnet"),
    #    "Frontend Developer": ("groq", "llama-3.3-70b")}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class AgentAssignment:
    """An agent matched to its best LLM."""
    role: str
    provider: str
    model: str
    reason: str  # why this LLM was chosen for this role
    score: float  # confidence in the match (0-1)


# ---------------------------------------------------------------------------
# Role → task type mapping
#
# Maps expert persona roles to the task classifier's task types so we
# can look up the learning engine's per-provider scores. The task
# classifier uses 13 types; this maps the 22 persona roles to the
# most relevant ones.
# ---------------------------------------------------------------------------

_ROLE_TO_TASK_TYPES: dict[str, list[str]] = {
    "CTO": ["architecture", "reasoning", "analysis"],
    "Software Architect": ["architecture", "code_generation", "reasoning"],
    "Senior Backend Engineer": ["code_generation", "debugging", "api"],
    "Senior Frontend Engineer": ["code_generation", "frontend", "debugging"],
    "DevOps/SRE Engineer": ["devops", "debugging", "infrastructure"],
    "Security Engineer": ["security", "analysis", "code_review"],
    "Database Administrator": ["database", "optimization", "analysis"],
    "Data Engineer": ["data", "code_generation", "analysis"],
    "ML/AI Engineer": ["ml", "code_generation", "reasoning"],
    "Mobile Developer": ["code_generation", "frontend", "debugging"],
    "CEO / Business Strategist": ["reasoning", "analysis", "creative"],
    "CFO / Financial Analyst": ["analysis", "reasoning", "math"],
    "Product Manager": ["analysis", "reasoning", "creative"],
    "UX Designer": ["creative", "analysis", "frontend"],
    "Engineering Manager": ["reasoning", "analysis", "management"],
    "QA/Test Engineer": ["testing", "code_generation", "debugging"],
    "Technical Writer": ["creative", "analysis", "documentation"],
    "Legal/Compliance Advisor": ["analysis", "reasoning", "compliance"],
    "Performance Engineer": ["optimization", "debugging", "analysis"],
    "Open Source Maintainer": ["code_review", "analysis", "creative"],
    "Blockchain/Web3 Engineer": ["code_generation", "security", "reasoning"],
    "Game Developer": ["code_generation", "optimization", "creative"],
}

# ---------------------------------------------------------------------------
# Static fallback strengths (used when no learning data exists)
# ---------------------------------------------------------------------------

_STATIC_PROVIDER_STRENGTHS: dict[str, list[str]] = {
    "openai": ["reasoning", "analysis", "creative", "code_generation"],
    "anthropic": ["reasoning", "analysis", "code_review", "security"],
    "groq": ["code_generation", "debugging", "general"],
    "google": ["reasoning", "analysis", "creative", "math"],
    "ollama": ["code_generation", "debugging", "general"],
    "github": ["code_generation", "debugging", "general"],
    "deepseek": ["code_generation", "math", "reasoning"],
    "mistral": ["code_generation", "reasoning", "creative"],
    "nvidia": ["code_generation", "ml", "optimization"],
    "llm7": ["general", "code_generation", "debugging"],
}


def match_agents_to_providers(
    agents: list,  # list of AgentPersona from agents.py
    engine,
    *,
    exclude_providers: set[str] | None = None,
    prefer_local: bool = False,
) -> list[AgentAssignment]:
    """Match each agent persona to the best available LLM.

    Strategy:
    1. For each agent, determine its primary task types
    2. Query the learning engine for per-provider scores on those types
    3. If learning data exists, pick the highest-scoring provider
    4. If no data, fall back to static capability map
    5. Ensure diversity — don't assign all agents to the same provider
       unless there's only one available

    Args:
        agents: List of AgentPersona from generate_agents()
        engine: NVHive Engine instance (has registry, learning, rate_manager)
        exclude_providers: Providers to skip (e.g., already used as synthesis)
        prefer_local: Boost local providers (ollama) for cost savings
    """
    exclude = exclude_providers or set()
    available = [
        p for p in engine.registry.list_enabled()
        if p not in exclude
    ]

    if not available:
        logger.warning("No providers available for agent matching")
        return []

    # Filter by health if rate_manager is available
    if hasattr(engine, "rate_manager"):
        healthy = [
            p for p in available
            if engine.rate_manager.get_health_score(p) >= 0.2
        ]
        if healthy:
            available = healthy

    assignments: list[AgentAssignment] = []
    used_providers: dict[str, int] = {}

    for agent in agents:
        role = agent.role
        task_types = _ROLE_TO_TASK_TYPES.get(role, ["general"])

        # Score each provider for this role
        provider_scores: list[tuple[str, float, str]] = []

        for provider_name in available:
            score = _score_provider_for_role(
                provider_name, task_types, engine,
                prefer_local=prefer_local,
            )

            # Diversity penalty — slight downrank for overused providers
            use_count = used_providers.get(provider_name, 0)
            diversity_penalty = use_count * 0.1
            adjusted_score = max(0, score - diversity_penalty)

            reason = _explain_match(provider_name, task_types, score)
            provider_scores.append((provider_name, adjusted_score, reason))

        # Pick the best
        provider_scores.sort(key=lambda x: x[1], reverse=True)
        best_provider, best_score, best_reason = provider_scores[0]

        # Get the best model for this provider
        model = _get_model_for_provider(best_provider, engine)

        assignments.append(AgentAssignment(
            role=role,
            provider=best_provider,
            model=model,
            reason=best_reason,
            score=best_score,
        ))

        used_providers[best_provider] = used_providers.get(best_provider, 0) + 1

    return assignments


def _score_provider_for_role(
    provider_name: str,
    task_types: list[str],
    engine,
    prefer_local: bool = False,
) -> float:
    """Score a provider for a set of task types.

    Uses the learning engine's real data if available, falls back
    to static capability map.
    """
    scores: list[float] = []

    # Try learning engine first
    if hasattr(engine, "learning") and engine.learning is not None:
        for task_type in task_types:
            try:
                learned = engine.learning.get_score(provider_name, task_type)
                if learned is not None and learned > 0:
                    scores.append(learned)
            except Exception:
                pass

    # If no learning data, use static strengths
    if not scores:
        strengths = _STATIC_PROVIDER_STRENGTHS.get(provider_name, [])
        for task_type in task_types:
            if task_type in strengths:
                # Position in the strengths list matters — first = strongest
                position = strengths.index(task_type)
                scores.append(1.0 - (position * 0.15))
            else:
                scores.append(0.3)  # baseline for unknown capability

    # Local provider boost
    if prefer_local and provider_name in ("ollama", "local"):
        scores = [s * 1.2 for s in scores]

    return sum(scores) / len(scores) if scores else 0.5


def _get_model_for_provider(provider_name: str, engine) -> str:
    """Get the best model name for a provider."""
    pconfig = engine.config.providers.get(provider_name)
    if pconfig and pconfig.default_model:
        return pconfig.default_model
    return ""


def _explain_match(provider_name: str, task_types: list[str], score: float) -> str:
    """Generate a human-readable explanation of why this provider was chosen."""
    strengths = _STATIC_PROVIDER_STRENGTHS.get(provider_name, [])
    matching = [t for t in task_types if t in strengths]
    if matching:
        return f"Strong at {', '.join(matching)} (score: {score:.2f})"
    return f"Best available (score: {score:.2f})"


def format_team_report(assignments: list[AgentAssignment]) -> str:
    """Format the team assignments for display."""
    lines = ["[bold]Agent Team Assignments:[/bold]"]
    for a in assignments:
        lines.append(
            f"  {a.role:30s} → {a.provider}/{a.model or 'default'} "
            f"({a.reason})"
        )
    return "\n".join(lines)
