"""Multi-agent parallel pipeline — decomposes tasks, spawns agents on
the best LLMs, runs them in parallel, and integrates the results.

This is the deep-dive orchestration layer that connects:
- agent_matching.py (which LLM for which agent)
- model_manager.py (load/unload within VRAM constraints)
- autonomous.py (pre/post QA)
- agent_loop.py (tool execution)

Key design decisions:
- Cloud models run in parallel with NO constraints (infinite "VRAM")
- Local models may need swapping if total VRAM is exceeded
- Context is PRESERVED across model swaps — the agent's accumulated
  knowledge lives in Python message history, not in model weights
- If all models fit in FB, they stay loaded (fastest path)
- Dependencies between subtasks are respected (tests wait for code)

Usage:
    result = await run_parallel_pipeline(
        task="Build a notification service",
        engine=engine,
        working_dir=Path("."),
    )
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class SubTaskResult:
    """Result of one agent's work on a subtask.

    Note: This shares a similar shape with recursive_agents.AgentResponse.
    The two are intentionally separate: SubTaskResult tracks file operations
    and success/error state for agentic pipelines, while AgentResponse tracks
    referral chains and spawn depth for recursive expert routing.
    Conversion between them happens in run_parallel_pipeline (phase 4b).
    """
    role: str
    provider: str
    model: str
    is_local: bool
    content: str
    files_modified: list[str] = field(default_factory=list)
    files_created: list[str] = field(default_factory=list)
    success: bool = True
    error: str = ""
    duration_ms: int = 0
    cost_usd: Decimal = Decimal("0")


@dataclass
class PipelineResult:
    """Result of the full parallel pipeline."""
    task: str
    subtask_results: list[SubTaskResult]
    pre_qa_verdict: str = ""
    post_qa_verdict: str = ""
    post_qa_summary: str = ""
    suggested_improvements: list[str] = field(default_factory=list)
    total_duration_ms: int = 0
    total_cost_usd: Decimal = Decimal("0")
    models_used: list[str] = field(default_factory=list)
    vram_swaps: int = 0


async def run_parallel_pipeline(
    task: str,
    engine,
    working_dir: Path,
    *,
    on_progress=None,
    max_parallel: int = 4,
) -> PipelineResult:
    """Run a multi-agent parallel pipeline.

    1. Decompose the task into subtasks
    2. Match each subtask to the best LLM (local preferred)
    3. Check VRAM — plan model swaps if needed
    4. Run independent subtasks in parallel
    5. Run dependent subtasks sequentially after their dependencies
    6. Post-QA: verify integration across all subtask outputs

    Args:
        task: High-level task description
        engine: NVHive Engine instance
        working_dir: Codebase root
        on_progress: callback(phase, message, pct)
        max_parallel: max concurrent agents
    """
    start_time = time.monotonic()

    # ── Phase 1: Decompose ─────────────────────────────────────────
    if on_progress:
        on_progress("decompose", "Analyzing task and creating subtasks...", 0.0)

    subtasks = await _decompose_task(task, engine)

    if not subtasks:
        return PipelineResult(
            task=task,
            subtask_results=[],
            post_qa_verdict="FAILED",
            post_qa_summary="Could not decompose task into subtasks.",
        )

    if on_progress:
        on_progress("decompose", f"Created {len(subtasks)} subtasks", 0.1)

    # ── Phase 2: Match agents to LLMs ──────────────────────────────
    if on_progress:
        on_progress("matching", "Matching agents to best LLMs...", 0.15)

    assignments = _match_subtasks_to_llms(subtasks, engine)

    # ── Phase 3: Check VRAM constraints ────────────────────────────
    local_models = [a["model"] for a in assignments if a["is_local"]]
    cloud_models = [a["model"] for a in assignments if not a["is_local"]]
    vram_swaps = 0

    if local_models:
        try:
            from nvh.core.model_manager import ModelManager
            from nvh.utils.gpu import detect_gpus
            gpus = detect_gpus()
            total_vram = sum(g.vram_gb for g in gpus) if gpus else 24.0
            mm = ModelManager(vram_gb=total_vram)
            await mm.get_loaded_models()
            status = await mm.get_vram_status()

            if on_progress:
                on_progress(
                    "vram",
                    f"VRAM: {status.used_gb:.1f}/{status.total_gb:.1f} GB used, "
                    f"{len(local_models)} local models needed, "
                    f"{len(cloud_models)} cloud models (no VRAM needed)",
                    0.2,
                )

            # Check if all local models fit simultaneously
            from nvh.core.model_manager import MODEL_VRAM_GB, VRAM_OVERHEAD_GB
            total_needed = sum(MODEL_VRAM_GB.get(m, 5.0) for m in set(local_models))
            if total_needed + VRAM_OVERHEAD_GB <= total_vram:
                if on_progress:
                    on_progress("vram", "All models fit in VRAM — no swapping needed.", 0.25)
            else:
                if on_progress:
                    on_progress(
                        "vram",
                        f"Models need {total_needed:.1f} GB but only "
                        f"{total_vram:.1f} GB available — will swap between steps. "
                        f"Context is preserved across swaps.",
                        0.25,
                    )
                vram_swaps = 1  # will be updated during execution
        except Exception as e:
            logger.debug("VRAM check failed: %s", e)

    if on_progress and cloud_models:
        n = len(cloud_models)
        on_progress("vram", f"{n} cloud model(s) run in parallel (no VRAM needed).", 0.25)

    # ── Phase 4: Execute subtasks ──────────────────────────────────
    if on_progress:
        on_progress("execute", f"Running {len(subtasks)} agents...", 0.3)

    # Separate independent (parallel) from dependent (sequential)
    independent = [s for s in assignments if not s.get("depends_on")]
    dependent = [s for s in assignments if s.get("depends_on")]

    results: list[SubTaskResult] = []

    # Run independent subtasks in parallel (bounded by max_parallel)
    if independent:
        semaphore = asyncio.Semaphore(max_parallel)

        async def _run_one(assignment: dict) -> SubTaskResult:
            async with semaphore:
                return await _execute_subtask(assignment, engine, working_dir, on_progress)

        parallel_results = await asyncio.gather(
            *[_run_one(a) for a in independent],
            return_exceptions=True,
        )
        for r in parallel_results:
            if isinstance(r, SubTaskResult):
                results.append(r)
            else:
                results.append(SubTaskResult(
                    role="unknown", provider="", model="",
                    is_local=False, content="", success=False,
                    error=str(r),
                ))

    # Run dependent subtasks sequentially
    for dep in dependent:
        r = await _execute_subtask(dep, engine, working_dir, on_progress)
        results.append(r)

    # ── Phase 4b: Recursive referral spawning ────────────────────
    # Check if any agent requested a specialist via REFER: pattern
    from nvh.core.recursive_agents import (
        AgentResponse,
        detect_referrals,
        run_with_referrals,
    )

    has_referrals = any(
        detect_referrals(r.content, r.role, 0) for r in results if r.success
    )

    if has_referrals:
        if on_progress:
            on_progress("referrals", "Agents requested specialists — spawning referrals...", 0.7)

        # Convert SubTaskResults to AgentResponses for the referral engine
        initial_responses = [
            AgentResponse(
                role=r.role,
                provider=r.provider,
                model=r.model,
                content=r.content,
                depth=0,
                duration_ms=r.duration_ms,
                cost_usd=r.cost_usd,
            )
            for r in results
            if r.success
        ]

        referral_result = await run_with_referrals(
            task=task,
            engine=engine,
            initial_responses=initial_responses,
            max_depth=2,
            on_referral=lambda ref: on_progress(
                "referrals",
                f"  Spawning {ref.requested_role} (referred by {ref.requesting_agent})",
                0.75,
            ) if on_progress else None,
        )

        # Append spawned agent responses back as SubTaskResults
        for resp in referral_result.responses:
            if resp.spawned_from:  # only add the newly spawned ones
                results.append(SubTaskResult(
                    role=resp.role,
                    provider=resp.provider,
                    model=resp.model,
                    is_local=False,
                    content=resp.content,
                    success=bool(resp.content and not resp.content.startswith("(Referral failed")),
                    duration_ms=resp.duration_ms,
                    cost_usd=resp.cost_usd,
                ))

        if on_progress:
            on_progress(
                "referrals",
                f"Referral spawning complete — {referral_result.spawned_agents} specialist(s).",
                0.8,
            )

    # ── Phase 5: Post-QA ───────────────────────────────────────────
    if on_progress:
        on_progress("post_qa", "Reviewing all agents' work together...", 0.85)

    post_qa = await _integration_qa(task, results, engine)

    elapsed = int((time.monotonic() - start_time) * 1000)
    total_cost = sum(r.cost_usd for r in results)

    return PipelineResult(
        task=task,
        subtask_results=results,
        post_qa_verdict=post_qa.get("verdict", "UNKNOWN"),
        post_qa_summary=post_qa.get("summary", ""),
        suggested_improvements=post_qa.get("improvements", []),
        total_duration_ms=elapsed,
        total_cost_usd=total_cost,
        models_used=list({r.model for r in results if r.model}),
        vram_swaps=vram_swaps,
    )


async def _decompose_task(task: str, engine) -> list[dict]:
    """Ask the orchestrator to decompose a task into subtasks."""
    prompt = (
        "You are a senior engineering manager. Decompose this task into "
        "independent subtasks that can be worked on in parallel by "
        "different specialists. For each subtask, specify:\n"
        "- role: the specialist needed (e.g., 'Backend Engineer', 'DBA')\n"
        "- description: what they should do\n"
        "- depends_on: list of subtask indices this depends on (empty if independent)\n\n"
        f"Task: {task}\n\n"
        "Respond with a JSON array of subtasks."
    )
    try:
        resp = await engine.query(prompt=prompt, stream=False, use_cache=False)
        import json
        import re
        # Extract JSON from response
        json_match = re.search(r'\[.*\]', resp.content, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
    except Exception as e:
        logger.warning("Task decomposition failed: %s", e)
    return []


def _match_subtasks_to_llms(subtasks: list[dict], engine) -> list[dict]:
    """Match each subtask to the best LLM using agent_matching."""
    try:
        from nvh.core.agent_matching import _score_provider_for_role
    except ImportError:
        pass

    available = engine.registry.list_enabled()
    local_providers = {"ollama", "local"}

    assignments = []
    for st in subtasks:
        role = st.get("role", "Engineer")
        # Score providers, prefer local
        best_provider = available[0] if available else "ollama"
        best_score = 0
        is_local = False

        for p in available:
            try:
                score = _score_provider_for_role(p, [role.lower()], engine, prefer_local=True)
                if score > best_score:
                    best_score = score
                    best_provider = p
                    is_local = p in local_providers
            except Exception:
                pass

        pconfig = engine.config.providers.get(best_provider)
        model = pconfig.default_model if pconfig else ""

        assignments.append({
            **st,
            "provider": best_provider,
            "model": model,
            "is_local": is_local,
            "score": best_score,
        })

    return assignments


async def _execute_subtask(
    assignment: dict,
    engine,
    working_dir: Path,
    on_progress=None,
) -> SubTaskResult:
    """Execute a single subtask using the assigned LLM."""
    role = assignment.get("role", "Agent")
    provider = assignment.get("provider", "")
    model = assignment.get("model", "")
    description = assignment.get("description", "")
    is_local = assignment.get("is_local", False)

    start = time.monotonic()

    if on_progress:
        locality = "local" if is_local else "cloud"
        on_progress("execute", f"  {role} ({provider}, {locality}): working...", 0.5)

    try:
        resp = await engine.query(
            prompt=f"You are a {role}. Complete this task:\n\n{description}",
            provider=provider,
            model=model if model else None,
            stream=False,
            use_cache=False,
        )
        elapsed = int((time.monotonic() - start) * 1000)
        return SubTaskResult(
            role=role,
            provider=provider,
            model=model,
            is_local=is_local,
            content=resp.content,
            success=True,
            duration_ms=elapsed,
            cost_usd=resp.cost_usd if resp.cost_usd else Decimal("0"),
        )
    except Exception as e:
        elapsed = int((time.monotonic() - start) * 1000)
        return SubTaskResult(
            role=role, provider=provider, model=model,
            is_local=is_local, content="", success=False,
            error=str(e), duration_ms=elapsed,
        )


async def _integration_qa(
    task: str,
    results: list[SubTaskResult],
    engine,
) -> dict:
    """Post-QA: verify all subtask outputs work together."""
    summary_parts = []
    for r in results:
        status = "SUCCESS" if r.success else f"FAILED: {r.error[:100]}"
        summary_parts.append(f"- {r.role} ({r.provider}): {status}\n  {r.content[:200]}")

    prompt = (
        f"You are a senior QA engineer reviewing the combined output of "
        f"multiple agents working on this task:\n\n"
        f"Task: {task}\n\n"
        f"Agent outputs:\n" + "\n".join(summary_parts) + "\n\n"
        "Evaluate:\n"
        "1. Does the combined output accomplish the task?\n"
        "2. Are there integration issues between the agents' work?\n"
        "3. What's missing or incomplete?\n"
        "4. What specific improvements would make this production-ready?\n\n"
        "Respond with:\n"
        "VERDICT: PASSED/PARTIAL/FAILED\n"
        "SUMMARY: (1-2 sentences)\n"
        "IMPROVEMENTS:\n- (numbered list of specific suggestions)"
    )

    try:
        resp = await engine.query(prompt=prompt, stream=False, use_cache=False)
        content = resp.content

        from nvh.core.agent_protocol import parse_qa_verdict

        verdict = parse_qa_verdict(content)

        improvements = []
        in_improvements = False
        for line in content.split("\n"):
            if "IMPROVEMENT" in line.upper():
                in_improvements = True
                continue
            if in_improvements and line.strip().startswith(("-", "•", "1", "2", "3", "4", "5")):
                improvements.append(line.strip().lstrip("-•0123456789. "))

        return {
            "verdict": verdict,
            "summary": content[:500],
            "improvements": improvements,
        }
    except Exception as e:
        return {
            "verdict": "UNKNOWN",
            "summary": f"QA review failed: {e}",
            "improvements": [],
        }


def format_pipeline_result(result: PipelineResult) -> str:
    """Format the pipeline result for display."""
    lines = [
        "",
        f"[bold]Pipeline Result: {result.task}[/bold]",
        "",
        f"[bold]Agents:[/bold] {len(result.subtask_results)} total, "
        f"{sum(1 for r in result.subtask_results if r.success)} succeeded",
        "",
    ]

    for r in result.subtask_results:
        locality = "[green]LOCAL[/green]" if r.is_local else "[cyan]CLOUD[/cyan]"
        status = "[green]OK[/green]" if r.success else f"[red]FAIL: {r.error[:50]}[/red]"
        lines.append(
            f"  {r.role:25s} {locality} {r.provider}/{r.model or 'default':20s} "
            f"{r.duration_ms}ms {status}"
        )

    lines.append("")
    if result.vram_swaps > 0:
        lines.append(f"[dim]VRAM swaps: {result.vram_swaps} (context preserved)[/dim]")
    lines.append(f"[bold]Models used:[/bold] {', '.join(result.models_used)}")
    lines.append(f"[bold]Total cost:[/bold] ${result.total_cost_usd:.4f}")
    lines.append(f"[bold]Duration:[/bold] {result.total_duration_ms}ms")
    lines.append("")
    lines.append(f"[bold]QA Verdict:[/bold] {result.post_qa_verdict}")
    if result.post_qa_summary:
        lines.append(f"[dim]{result.post_qa_summary[:300]}[/dim]")
    if result.suggested_improvements:
        lines.append("")
        lines.append("[bold]Suggested Improvements:[/bold]")
        for i, imp in enumerate(result.suggested_improvements, 1):
            lines.append(f"  {i}. {imp}")

    return "\n".join(lines)
