"""Iterative refinement loop — agents spawn agents until QA passes.

This is the glue that connects:
- recursive_agents.py (agents spawn specialists on demand)
- autonomous.py (pre/post QA on every execution)
- parallel_pipeline.py (parallel dispatch)
- agent_matching.py (best LLM per role)

The loop:
1. Generate initial expert agents for the task
2. Run them (with recursive referral spawning)
3. Post-QA reviews the combined output
4. If PASSED → done
5. If PARTIAL/FAILED → QA feedback identifies gaps
6. Orchestrator spawns new/different agents to address gaps
7. Go to step 2 (max N rounds)

The key insight: any agent at any depth can say "I need a Blender
Color Management Expert" and the system dynamically creates one,
gets the answer, and feeds it back. No more guessing at fixes
outside an agent's expertise. The recursive spawning + iterative
QA loop means the system keeps going until it either finds the
right answer or exhausts its budget.

Usage:
    result = await iterative_solve(
        task="Fix the color banding in the render pipeline",
        engine=engine,
        working_dir=Path("."),
    )
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class IterationRound:
    """One round of the iterative refinement loop."""
    round_number: int
    agents_used: list[str]
    spawned_agents: list[str]
    qa_verdict: str  # PASSED / PARTIAL / FAILED
    qa_feedback: str
    improvements_suggested: list[str]
    duration_ms: int = 0
    cost_usd: Decimal = Decimal("0")


@dataclass
class IterativeResult:
    """Final result of the iterative solve loop."""
    task: str
    rounds: list[IterationRound]
    final_verdict: str
    final_synthesis: str
    total_agents_used: int
    total_agents_spawned: int  # via recursive referral
    total_rounds: int
    converged: bool  # did it reach PASSED?
    final_improvements: list[str]
    total_duration_ms: int = 0
    total_cost_usd: Decimal = Decimal("0")


async def iterative_solve(
    task: str,
    engine,
    working_dir: Path,
    *,
    max_rounds: int = 3,
    max_agents_per_round: int = 5,
    max_referral_depth: int = 2,
    max_total_agents: int = 15,
    budget_usd: float = 1.0,
    on_progress=None,
) -> IterativeResult:
    """Run the iterative refinement loop until QA passes or budget exhausts.

    Each round:
    1. Generate/adjust agents based on previous QA feedback
    2. Run with recursive referral spawning
    3. Synthesize all agent outputs
    4. Post-QA evaluates: PASSED → stop, else → next round with feedback

    The loop also stops early if cumulative cost exceeds *budget_usd*.
    """
    start = time.monotonic()
    rounds: list[IterationRound] = []
    cumulative_context = ""
    total_agents = 0
    total_spawned = 0
    final_synthesis = ""
    cumulative_cost = Decimal("0")
    budget_limit = Decimal(str(budget_usd))

    for round_num in range(1, max_rounds + 1):
        # ── Budget gate ──
        if cumulative_cost >= budget_limit:
            logger.warning(
                "Stopping iterative solve: cumulative cost $%.4f "
                "exceeds budget $%.2f",
                cumulative_cost,
                budget_limit,
            )
            if on_progress:
                on_progress(
                    "budget_exceeded",
                    f"Budget exceeded (${cumulative_cost:.4f} / ${budget_limit:.2f})",
                    1.0,
                )
            break
        round_start = time.monotonic()

        if on_progress:
            on_progress(
                f"round_{round_num}",
                f"Starting round {round_num}/{max_rounds}...",
                round_num / max_rounds,
            )

        # ── Step 1: Generate agents (informed by previous QA feedback) ──
        agent_prompt = f"Task: {task}\n\n"
        if cumulative_context:
            agent_prompt += (
                f"Previous rounds found these gaps:\n{cumulative_context}\n\n"
                f"Generate expert agents that specifically address these gaps. "
                f"Focus on the areas that were marked as incomplete or incorrect."
            )
        else:
            agent_prompt += "Generate the most relevant expert agents for this task."

        try:
            from nvh.core.agent_matching import match_agents_to_providers
            from nvh.core.agents import generate_agents
            from nvh.core.recursive_agents import (
                AgentResponse,
                run_with_referrals,
            )

            personas = generate_agents(agent_prompt, num_agents=min(max_agents_per_round, 5))
            assignments = match_agents_to_providers(personas, engine)
        except Exception as e:
            logger.warning("Agent generation failed in round %d: %s", round_num, e)
            rounds.append(IterationRound(
                round_number=round_num, agents_used=[], spawned_agents=[],
                qa_verdict="FAILED", qa_feedback=f"Agent generation failed: {e}",
                improvements_suggested=[],
            ))
            break

        # ── Step 2: Run agents with recursive referral spawning ──
        initial_responses: list[AgentResponse] = []
        for assignment in assignments:
            try:
                resp = await engine.query(
                    prompt=(
                        f"You are a {assignment.role}. "
                        f"Task: {task}\n\n"
                        f"{'Previous feedback: ' + cumulative_context if cumulative_context else ''}"  # noqa: E501
                        f"\n\n"
                        f"Provide your expert analysis. If you need input from another "
                        f"specialist, include: REFER: Need a [Role] for [specific question]"
                    ),
                    provider=assignment.provider,
                    model=assignment.model if assignment.model else None,
                    stream=False,
                    use_cache=False,
                )
                initial_responses.append(AgentResponse(
                    role=assignment.role,
                    provider=assignment.provider,
                    model=assignment.model,
                    content=resp.content,
                    depth=0,
                    cost_usd=resp.cost_usd if resp.cost_usd else Decimal("0"),
                ))
            except Exception as e:
                initial_responses.append(AgentResponse(
                    role=assignment.role, provider=assignment.provider,
                    model=assignment.model, content=f"(Failed: {e})", depth=0,
                ))

        # Process recursive referrals
        recursive_result = await run_with_referrals(
            task=task,
            engine=engine,
            initial_responses=initial_responses,
            max_depth=max_referral_depth,
            max_total_agents=max_total_agents - total_agents,
        )

        total_agents += recursive_result.total_agents
        total_spawned += recursive_result.spawned_agents

        # ── Step 3: Synthesize all responses ──
        all_content = "\n\n".join(
            f"**{r.role}** ({r.provider})"
            f"{' [referred by ' + r.spawned_from + ']' if r.spawned_from else ''}:\n"
            f"{r.content[:1000]}"
            for r in recursive_result.responses
        )

        try:
            synth_resp = await engine.query(
                prompt=(
                    f"Synthesize these expert responses into a unified answer:\n\n"
                    f"Task: {task}\n\n"
                    f"Expert responses:\n{all_content}\n\n"
                    f"Produce a coherent, actionable response that integrates "
                    f"the best insights from all experts."
                ),
                stream=False, use_cache=False,
            )
            final_synthesis = synth_resp.content
        except Exception as e:
            final_synthesis = f"Synthesis failed: {e}"

        # ── Step 4: Post-QA evaluation ──
        try:
            qa_resp = await engine.query(
                prompt=(
                    f"You are a senior QA reviewer. Evaluate this response:\n\n"
                    f"Task: {task}\n\n"
                    f"Response:\n{final_synthesis[:2000]}\n\n"
                    f"Experts consulted: {', '.join(r.role for r in recursive_result.responses)}\n"
                    f"({recursive_result.spawned_agents} were dynamically spawned via referral)\n\n"
                    f"Evaluate:\n"
                    f"1. Is the task fully addressed?\n"
                    f"2. Are there gaps or incorrect assumptions?\n"
                    f"3. What specific improvements are needed?\n\n"
                    f"VERDICT: PASSED / PARTIAL / FAILED\n"
                    f"GAPS: (list specific gaps if any)\n"
                    f"IMPROVEMENTS: (numbered list)"
                ),
                stream=False, use_cache=False,
            )
            qa_content = qa_resp.content

            from nvh.core.agent_protocol import parse_qa_verdict

            verdict = parse_qa_verdict(qa_content)

            improvements = []
            for line in qa_content.split("\n"):
                line = line.strip()
                if line and line[0] in "0123456789-•" and len(line) > 3:
                    improvements.append(line.lstrip("0123456789.-•) "))

        except Exception as e:
            verdict = "FAILED"
            qa_content = f"QA failed: {e}"
            improvements = []

        round_elapsed = int((time.monotonic() - round_start) * 1000)
        round_cost = sum(r.cost_usd for r in recursive_result.responses)

        rounds.append(IterationRound(
            round_number=round_num,
            agents_used=[r.role for r in recursive_result.responses],
            spawned_agents=[r.role for r in recursive_result.responses if r.spawned_from],
            qa_verdict=verdict,
            qa_feedback=qa_content[:500],
            improvements_suggested=improvements,
            duration_ms=round_elapsed,
            cost_usd=round_cost,
        ))

        cumulative_cost += round_cost

        if on_progress:
            on_progress(
                f"round_{round_num}",
                f"Round {round_num}: {verdict}",
                round_num / max_rounds,
            )

        if verdict == "PASSED":
            logger.info("Iterative solve converged in round %d", round_num)
            break

        # ── Step 5: Feed QA feedback into next round ──
        cumulative_context += f"\nRound {round_num} feedback:\n{qa_content[:500]}\n"

    elapsed = int((time.monotonic() - start) * 1000)
    final_verdict = rounds[-1].qa_verdict if rounds else "FAILED"

    return IterativeResult(
        task=task,
        rounds=rounds,
        final_verdict=final_verdict,
        final_synthesis=final_synthesis,
        total_agents_used=total_agents,
        total_agents_spawned=total_spawned,
        total_rounds=len(rounds),
        converged=final_verdict == "PASSED",
        final_improvements=rounds[-1].improvements_suggested if rounds else [],
        total_duration_ms=elapsed,
        total_cost_usd=sum(r.cost_usd for r in rounds),
    )


def format_iterative_result(result: IterativeResult) -> str:
    """Format the iterative solve result for display."""
    conv = "[green]Yes[/green]" if result.converged else "[yellow]No[/yellow]"
    agents_summary = (
        f"{result.total_agents_used} total "
        f"({result.total_agents_spawned} spawned via referral)"
    )
    lines = [
        "",
        f"[bold]Iterative Solve: {result.task}[/bold]",
        f"[bold]Rounds:[/bold] {result.total_rounds} | "
        f"[bold]Converged:[/bold] {conv}",
        f"[bold]Agents:[/bold] {agents_summary}",
        "",
    ]

    verdict_colors = {
        "PASSED": "green", "PARTIAL": "yellow", "FAILED": "red",
    }
    for r in result.rounds:
        verdict_color = verdict_colors.get(r.qa_verdict, "white")
        lines.append(f"  Round {r.round_number}: [{verdict_color}]{r.qa_verdict}[/{verdict_color}]")
        lines.append(f"    Agents: {', '.join(r.agents_used)}")
        if r.spawned_agents:
            lines.append(f"    Spawned: {', '.join(r.spawned_agents)}")
        if r.improvements_suggested:
            for imp in r.improvements_suggested[:3]:
                lines.append(f"    - {imp}")
        lines.append("")

    if result.final_improvements and not result.converged:
        lines.append("[bold]Remaining improvements:[/bold]")
        for imp in result.final_improvements:
            lines.append(f"  - {imp}")

    dur = result.total_duration_ms
    cost = result.total_cost_usd
    lines.append(f"\n[dim]Duration: {dur}ms | Cost: ${cost:.4f}[/dim]")
    return "\n".join(lines)
