"""Recursive agent spawning — agents that know what they don't know.

When an expert agent identifies a knowledge gap, it can request
additional specialists. The system spawns the requested expert,
gives them the full context, and integrates their response back
into the original agent's work.

This is meta-cognition: agents self-identify when they need help,
rather than the orchestrator guessing upfront.

Example flow:
  1. Orchestrator generates 3 agents: Backend, DevOps, QA
  2. Backend agent responds: "This involves complex sharding —
     REFER: Need a Database Expert for the partitioning strategy"
  3. System detects the referral, spawns a Database Expert
  4. DBA responds with partitioning advice
  5. Backend incorporates the DBA's advice into its final response
  6. Synthesis includes all perspectives (original 3 + spawned DBA)

Safety: max_referral_depth prevents infinite recursion.

Usage:
    result = await run_with_referrals(
        task="Design the data layer",
        engine=engine,
        initial_agents=agents,
        max_depth=2,
    )
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal

logger = logging.getLogger(__name__)

# Pattern that agents use to request a referral
REFERRAL_PATTERN = re.compile(
    r'REFER:\s*(?:Need|Request|Consult|Loop in|Get)\s+(?:a\s+)?(.+?)(?:\s+for\s+(.+?))?(?:\.|$)',
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class ReferralRequest:
    """A request from one agent to spawn another expert."""
    requesting_agent: str
    requested_role: str
    context: str  # why they need this expert
    depth: int  # how many referral levels deep


@dataclass
class AgentResponse:
    """Response from an agent, possibly with referral requests."""
    role: str
    provider: str
    model: str
    content: str
    referrals: list[ReferralRequest] = field(default_factory=list)
    spawned_from: str = ""  # which agent requested this one
    depth: int = 0
    duration_ms: int = 0
    cost_usd: Decimal = Decimal("0")


@dataclass
class RecursiveResult:
    """Result of a recursive agent session."""
    task: str
    responses: list[AgentResponse]
    total_agents: int
    spawned_agents: int  # agents created via referral
    max_depth_reached: int
    synthesis: str = ""
    duration_ms: int = 0
    total_cost_usd: Decimal = Decimal("0")


def detect_referrals(response_text: str, agent_role: str, depth: int) -> list[ReferralRequest]:
    """Parse an agent's response for referral requests.

    Agents include lines like:
      REFER: Need a Database Expert for the sharding strategy
      REFER: Consult a Security Engineer for the auth design
      REFER: Loop in a Frontend Developer for the component architecture
    """
    referrals = []
    for match in REFERRAL_PATTERN.finditer(response_text):
        requested_role = match.group(1).strip()
        context = match.group(2).strip() if match.group(2) else ""
        referrals.append(ReferralRequest(
            requesting_agent=agent_role,
            requested_role=requested_role,
            context=context,
            depth=depth + 1,
        ))
    return referrals


async def run_with_referrals(
    task: str,
    engine,
    initial_responses: list[AgentResponse],
    *,
    max_depth: int = 2,
    max_total_agents: int = 10,
    on_referral=None,
) -> RecursiveResult:
    """Process agent responses and spawn referred experts recursively.

    Args:
        task: The original task
        engine: NVHive Engine
        initial_responses: Responses from the first round of agents
        max_depth: Maximum referral depth (prevents infinite recursion)
        max_total_agents: Hard cap on total agents (initial + spawned)
        on_referral: callback(referral: ReferralRequest) for progress
    """
    start = time.monotonic()
    all_responses = list(initial_responses)
    pending_referrals: list[ReferralRequest] = []
    spawned_count = 0

    # Extract referrals from initial responses
    for resp in initial_responses:
        refs = detect_referrals(resp.content, resp.role, resp.depth)
        pending_referrals.extend(refs)

    # Process referrals level by level
    while pending_referrals:
        current_batch = pending_referrals
        pending_referrals = []

        for referral in current_batch:
            if referral.depth > max_depth:
                logger.info(
                    "Skipping referral at depth %d (max %d): %s requested %s",
                    referral.depth, max_depth,
                    referral.requesting_agent, referral.requested_role,
                )
                continue

            if len(all_responses) >= max_total_agents:
                logger.info("Hit max agent cap (%d), skipping further referrals", max_total_agents)
                break

            if on_referral:
                on_referral(referral)

            logger.info(
                "Spawning referred agent: %s (requested by %s, depth %d)",
                referral.requested_role, referral.requesting_agent, referral.depth,
            )

            # Build context for the referred expert
            # Include the original task + the requesting agent's context
            referral_prompt = (
                f"You are a {referral.requested_role}. You were called in by "
                f"the {referral.requesting_agent} who needs your expertise.\n\n"
                f"Original task: {task}\n\n"
            )
            if referral.context:
                referral_prompt += f"Specific question: {referral.context}\n\n"
            referral_prompt += (
                "Provide your expert opinion on the aspect relevant to your "
                "specialty. Be specific and actionable.\n\n"
                "If YOU also need input from another specialist, include a line:\n"
                "REFER: Need a [Role] for [specific question]"
            )

            # Query the best LLM for this role
            try:
                from nvh.core.agent_matching import _score_provider_for_role
                # Find the best provider for this role
                available = engine.registry.list_enabled()
                best_provider = available[0] if available else None
                best_score = 0
                for p in available:
                    try:
                        score = _score_provider_for_role(
                            p, [referral.requested_role.lower()], engine,
                        )
                        if score > best_score:
                            best_score = score
                            best_provider = p
                    except Exception:
                        pass

                t0 = time.monotonic()
                resp = await engine.query(
                    prompt=referral_prompt,
                    provider=best_provider,
                    stream=False,
                    use_cache=False,
                )
                elapsed = int((time.monotonic() - t0) * 1000)

                agent_resp = AgentResponse(
                    role=referral.requested_role,
                    provider=best_provider or "",
                    model=resp.model,
                    content=resp.content,
                    spawned_from=referral.requesting_agent,
                    depth=referral.depth,
                    duration_ms=elapsed,
                    cost_usd=resp.cost_usd if resp.cost_usd else Decimal("0"),
                )

                # Check if THIS agent also has referrals
                if referral.depth < max_depth:
                    new_refs = detect_referrals(resp.content, referral.requested_role, referral.depth)
                    pending_referrals.extend(new_refs)

                all_responses.append(agent_resp)
                spawned_count += 1

            except Exception as e:
                logger.warning("Failed to spawn referred agent %s: %s", referral.requested_role, e)
                all_responses.append(AgentResponse(
                    role=referral.requested_role,
                    provider="",
                    model="",
                    content=f"(Referral failed: {e})",
                    spawned_from=referral.requesting_agent,
                    depth=referral.depth,
                ))

    elapsed = int((time.monotonic() - start) * 1000)
    max_depth_used = max((r.depth for r in all_responses), default=0)

    return RecursiveResult(
        task=task,
        responses=all_responses,
        total_agents=len(all_responses),
        spawned_agents=spawned_count,
        max_depth_reached=max_depth_used,
        duration_ms=elapsed,
        total_cost_usd=sum(r.cost_usd for r in all_responses),
    )


def format_recursive_result(result: RecursiveResult) -> str:
    """Format the recursive agent session for display."""
    lines = [
        "\n[bold]Recursive Agent Session[/bold]",
        f"[bold]Task:[/bold] {result.task}",
        f"[bold]Agents:[/bold] {result.total_agents} total ({result.spawned_agents} spawned via referral)",
        f"[bold]Max depth:[/bold] {result.max_depth_reached}",
        "",
    ]

    for r in result.responses:
        indent = "  " * r.depth
        spawned = f" [dim](referred by {r.spawned_from})[/dim]" if r.spawned_from else ""
        lines.append(f"{indent}[bold]{r.role}[/bold] ({r.provider}){spawned}")
        # Show first 200 chars of content
        preview = r.content[:200].replace("\n", " ")
        lines.append(f"{indent}  {preview}{'...' if len(r.content) > 200 else ''}")
        lines.append("")

    lines.append(f"[dim]Duration: {result.duration_ms}ms | Cost: ${result.total_cost_usd:.4f}[/dim]")
    return "\n".join(lines)
