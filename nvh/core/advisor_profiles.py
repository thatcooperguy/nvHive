"""Advisor profiles: strengths, weaknesses, weights, and routing intelligence.

Each advisor has a profile that tells the router:
- WHEN to use this advisor (strengths, best-for scenarios)
- WHEN NOT to use this advisor (weaknesses, negative prompts)
- HOW to weight this advisor in council mode
- COST tier for budget-aware routing

This data drives smart routing — the system picks the right advisor
for each question without the user needing to specify.

The profiles ARE the :data:`nvh.providers.specs.PROVIDER_FACTS` rows: an
advisor card is a :class:`~nvh.providers.specs.ProviderSpec` (weights, prose
tuples, cost tier, free-tier note, capability flags), read by attribute in
``nvh advisor info``, the dashboard's free-providers endpoint, the router's
profile bonus and the MCP server. The two card-side spellings the consumers
use — ``has_free_tier`` and ``free_tier_limits`` — are properties on the
spec, so a fact added to a spec row reaches every card without a second
dataclass to keep in step. ``AdvisorProfile`` is the spec class under its
old name, for importers.
"""

from __future__ import annotations

from nvh.providers.specs import PROVIDER_FACTS, ProviderSpec

#: The card type — a spec row. Kept under the pre-0.44 name for importers.
AdvisorProfile = ProviderSpec


def profile_for(spec: ProviderSpec) -> ProviderSpec:
    """The advisor card a spec row describes — the row itself."""
    return spec


# ---------------------------------------------------------------------------
# Advisor Profiles Database — one card per spec row that carries card prose.
# Triton and Mock carry none and have no card (and no router bonus).
# ---------------------------------------------------------------------------

ADVISOR_PROFILES: dict[str, ProviderSpec] = {
    name: spec for name, spec in PROVIDER_FACTS.items() if spec.strengths
}


def get_advisor_profile(name: str) -> ProviderSpec | None:
    """Get the profile for an advisor by name."""
    return ADVISOR_PROFILES.get(name)


def format_advisor_card(name: str) -> str:
    """Format an advisor profile as a rich text card for display."""
    profile = ADVISOR_PROFILES.get(name)
    if not profile:
        return f"Unknown advisor: {name}"

    lines = [
        f"[bold]{profile.display_name}[/bold]",
        f"Cost: {profile.cost_tier} | Quality: {profile.quality_weight:.0%} | Speed: {profile.speed_weight:.0%}",
        "",
        "[green]Best for:[/green]",
    ]
    for item in profile.best_for[:3]:
        lines.append(f"  + {item}")
    lines.append("")
    lines.append("[red]Avoid for:[/red]")
    for item in profile.avoid_for[:3]:
        lines.append(f"  - {item}")

    if profile.has_free_tier:
        lines.append(f"\n[cyan]Free: {profile.free_tier_limits}[/cyan]")

    return "\n".join(lines)
