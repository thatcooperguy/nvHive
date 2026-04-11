"""Agentic coding — tier-aware multi-model coding agent (beta).

A hierarchical agent loop that:
1. **Plans** using the strongest available model (cloud or local 70B)
2. **Executes** using local models (sized by GPU tier)
3. **Verifies** using the orchestrator model

Scales automatically based on detected GPU VRAM:

  Tier 3 (128 GB+, DGX Spark):  70B local orchestrator + workers, minimal cloud
  Tier 2 (48 GB, RTX 6000 Pro): cloud orchestrator, 32B local workers
  Tier 1 (24 GB, RTX 3090):     cloud orchestrator, 14B local worker
  Tier 0 (<24 GB / no GPU):     fully cloud

Usage (CLI):
    nvh agent "Fix the streaming hang bug in council.py"
    nvh agent "Add tests for the auth middleware" --tier 3
    nvh agent "Refactor the router to use health scores" --dir /d/GitHub/project

Usage (SDK):
    from nvh.core.agentic import run_coding_agent, auto_detect_config
    config = auto_detect_config(engine)
    result = await run_coding_agent("Add retry logic", engine, config, Path("."))
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from nvh.core.agent_loop import AgentResult, run_agent_loop
from nvh.core.tools import ToolRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GPU tier detection
# ---------------------------------------------------------------------------


class AgentTier(enum.StrEnum):
    """GPU tier, determines which models and parallelism level to use."""
    TIER_0 = "tier_0"  # no GPU or <24 GB — fully cloud
    TIER_1 = "tier_1"  # 24-47 GB (RTX 3090, RTX 4090) — cloud orchestrator, small local worker
    TIER_2 = "tier_2"  # 48-127 GB (RTX 6000 Pro BSE 96GB, A100 80GB) — cloud orchestrator, 70B worker
    TIER_3 = "tier_3"  # 128+ GB (DGX Spark, multi-GPU) — fully local, multiple 70B workers


def detect_agent_tier(total_vram_gb: float) -> AgentTier:
    """Map total available VRAM to a tier.

    Uses total across all GPUs so multi-GPU setups get a higher tier
    even if no single card hits the threshold alone.
    """
    if total_vram_gb >= 128:
        return AgentTier.TIER_3
    if total_vram_gb >= 48:
        return AgentTier.TIER_2
    if total_vram_gb >= 24:
        return AgentTier.TIER_1
    return AgentTier.TIER_0


# ---------------------------------------------------------------------------
# Agent configuration
# ---------------------------------------------------------------------------


# Model recommendations per tier. Each tuple is (provider, model).
# None means "use whatever the engine routes to by default" (usually
# the user's configured default or the smart router's pick).
#
# Model selection priority:
#   1. Nemotron (NVIDIA-optimized, best on NVIDIA hardware)
#   2. Llama 3.3 70B (strong general + coding, 128K context)
#   3. Gemma 2 27B (strong coding, smaller footprint)
#   4. Qwen 2.5 Coder (specialized for code generation)
#
# The build_agent_config() function validates these against the
# registry and falls back to engine defaults when not available.
_TIER_MODELS: dict[AgentTier, dict[str, tuple[str | None, str | None]]] = {
    AgentTier.TIER_3: {
        # DGX Spark (128 GB+): both orchestrator and worker fully local.
        # Nemotron 70B for orchestration (NVIDIA-optimized), Llama 70B
        # as the worker coder. Both fit comfortably with room for
        # parallel inference.
        "orchestrator": ("ollama", "ollama/nemotron:70b"),
        "worker": ("ollama", "ollama/llama3.3:70b"),
    },
    AgentTier.TIER_2: {
        # RTX 6000 Pro BSE (96 GB): cloud orchestrator, local 70B worker.
        # 96 GB is enough for a 70B model at Q4/Q5 quantization (~40 GB)
        # with headroom for KV cache. Llama 3.3 70B is the best local
        # coder at this size.
        "orchestrator": (None, None),  # engine default (cloud)
        "worker": ("ollama", "ollama/llama3.3:70b"),
    },
    AgentTier.TIER_1: {
        # RTX 3090 (24 GB): cloud orchestrator, local 14B-27B worker.
        # Gemma 2 27B at Q4 (~16 GB) or Qwen 2.5 Coder 14B at Q8
        # (~15 GB). Both fit in 24 GB with room for context.
        "orchestrator": (None, None),
        "worker": ("ollama", "ollama/gemma2:27b"),
    },
    AgentTier.TIER_0: {
        # No GPU: everything goes to cloud
        "orchestrator": (None, None),
        "worker": (None, None),
    },
}


@dataclass
class AgentConfig:
    """Configuration for a coding agent session."""
    tier: AgentTier
    orchestrator_provider: str | None = None
    orchestrator_model: str | None = None
    worker_provider: str | None = None
    worker_model: str | None = None
    max_parallel_workers: int = 1  # reserved for future parallel dispatch
    max_iterations: int = 10
    verify_results: bool = True


def build_agent_config(
    tier: AgentTier,
    registry=None,
) -> AgentConfig:
    """Build a concrete config for the given tier.

    If a registry is provided, validates that the recommended providers
    are actually available and falls back to engine defaults (cloud)
    when they're not. This means a Tier 1 user without Ollama installed
    still gets a working agent — just fully cloud.
    """
    tier_models = _TIER_MODELS[tier]
    orch = tier_models["orchestrator"]
    work = tier_models["worker"]

    # Validate providers exist in registry
    if registry is not None:
        if orch[0] and not registry.has(orch[0]):
            logger.info(
                "Tier %s orchestrator %s not in registry — falling back to engine default",
                tier, orch[0],
            )
            orch = (None, None)
        if work[0] and not registry.has(work[0]):
            logger.info(
                "Tier %s worker %s not in registry — falling back to engine default",
                tier, work[0],
            )
            work = (None, None)

    return AgentConfig(
        tier=tier,
        orchestrator_provider=orch[0],
        orchestrator_model=orch[1],
        worker_provider=work[0],
        worker_model=work[1],
        max_parallel_workers=_parallel_workers(tier),
    )


def _parallel_workers(tier: AgentTier) -> int:
    """Max concurrent workers per tier (reserved for future use)."""
    return {
        AgentTier.TIER_0: 1,
        AgentTier.TIER_1: 1,
        AgentTier.TIER_2: 2,
        AgentTier.TIER_3: 4,
    }[tier]


def auto_detect_config(engine) -> AgentConfig:
    """Detect GPU tier and build a config automatically.

    Uses the engine's registry to validate provider availability.
    Falls back to TIER_0 (fully cloud) if GPU detection fails.
    """
    try:
        from nvh.utils.gpu import detect_gpus
        gpus = detect_gpus()
        total_vram = sum(g.vram_gb for g in gpus) if gpus else 0
    except Exception:
        total_vram = 0

    tier = detect_agent_tier(total_vram)
    logger.info("Agent tier: %s (%.0f GB VRAM detected)", tier, total_vram)

    return build_agent_config(tier, registry=engine.registry)


# ---------------------------------------------------------------------------
# Coding-specific system prompt
# ---------------------------------------------------------------------------

CODING_SYSTEM_PROMPT = """You are an expert coding agent. You receive a task and use tools to read, understand, and modify code in a real codebase.

APPROACH:
1. START by understanding the task. Think about what files you need to read.
2. Use list_files and search_files to find relevant code.
3. Use read_file to read and understand existing code BEFORE modifying anything.
4. Make surgical, targeted edits — do NOT rewrite entire files.
5. After making changes, verify your work by reading the modified files.
6. When the task is complete, provide a clear summary of what you changed and why.

RULES:
- Always read a file before modifying it.
- Make the minimum change needed — don't add features that weren't asked for.
- Don't add unnecessary comments, docstrings, or type annotations to code you didn't change.
- If you're unsure about something, explain your uncertainty instead of guessing.
- If you can't complete the task, explain what you tried and what blocked you.

When you need to use a tool, respond with a JSON tool call block:

```tool_call
{{"tool": "tool_name", "args": {{"param1": "value1"}}}}
```

Available tools:
{tool_descriptions}

When your work is complete, respond with your final summary WITHOUT any tool calls.
"""


# ---------------------------------------------------------------------------
# Coding agent result
# ---------------------------------------------------------------------------


@dataclass
class CodingResult:
    """Result of a coding agent session."""
    task: str
    plan: str
    final_summary: str
    files_modified: list[str] = field(default_factory=list)
    files_created: list[str] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    commands_run: list[str] = field(default_factory=list)
    total_iterations: int = 0
    total_tool_calls: int = 0
    completed: bool = False
    verification: str = ""
    total_cost_usd: Decimal = Decimal("0")
    duration_ms: int = 0
    tier: AgentTier = AgentTier.TIER_0
    worker_model: str = ""
    orchestrator_model: str = ""
    error: str = ""


def _extract_file_operations(result: AgentResult) -> tuple[list[str], list[str], list[str]]:
    """Parse files_modified, files_created, and files_read from tool results."""
    modified: list[str] = []
    created: list[str] = []
    read: list[str] = []

    for step in result.steps:
        for call, res in zip(step.tool_calls, step.tool_results):
            tool = call.get("tool", "")
            path = call.get("args", {}).get("path", "")
            if not path:
                continue

            if tool == "read_file" and res.success:
                if path not in read:
                    read.append(path)
            elif tool == "write_file" and res.success:
                if path in read:
                    if path not in modified:
                        modified.append(path)
                else:
                    if path not in created:
                        created.append(path)

    return modified, created, read


def _extract_commands(result: AgentResult) -> list[str]:
    """Parse shell commands from tool results."""
    cmds: list[str] = []
    for step in result.steps:
        for call in step.tool_calls:
            if call.get("tool") in ("shell", "run_code"):
                cmd = call.get("args", {}).get("command", "")
                if cmd and cmd not in cmds:
                    cmds.append(cmd)
    return cmds


# ---------------------------------------------------------------------------
# Main agent loop
# ---------------------------------------------------------------------------


async def run_coding_agent(
    task: str,
    engine,
    config: AgentConfig,
    working_dir: Path,
    on_step: Any = None,
    confirm_write: Any = None,
    system_prompt: str | None = None,
) -> CodingResult:
    """Run the three-phase coding agent loop.

    Phase 1 — Plan:
        Ask the orchestrator model to analyze the task and create a plan.

    Phase 2 — Execute:
        Run the agent loop with the worker model, using tools to read
        files, make edits, and run commands. The plan from Phase 1 is
        prepended as context.

    Phase 3 — Verify (optional):
        Ask the orchestrator to review the changes and flag any issues.
        If issues are found and we haven't exceeded the retry limit,
        loop back to Phase 2 with the feedback.

    Args:
        task: The coding task description.
        engine: NVHive Engine instance.
        config: Agent configuration (tier, models, limits).
        working_dir: Root directory of the codebase to operate on.
        on_step: Callback for live step updates (step: AgentStep).
        confirm_write: Callback to confirm file writes (tool, args) -> bool.
        system_prompt: Override the default coding system prompt.
    """
    start_time = time.monotonic()
    tools = ToolRegistry(workspace=str(working_dir))

    # ── Phase 1: Plan ──────────────────────────────────────────────────
    logger.info("Agent Phase 1: Planning (orchestrator=%s/%s)",
                config.orchestrator_provider or "default",
                config.orchestrator_model or "default")

    plan_prompt = (
        f"You are a senior software engineer. Analyze this coding task and "
        f"create a step-by-step plan. List which files need to be read, "
        f"what changes to make, and in what order.\n\n"
        f"Working directory: {working_dir}\n\n"
        f"Task: {task}\n\n"
        f"Respond with a numbered plan. Be specific about file paths and "
        f"the nature of each change. Do NOT use any tools — just plan."
    )

    try:
        plan_response = await engine.query(
            prompt=plan_prompt,
            provider=config.orchestrator_provider,
            model=config.orchestrator_model,
            stream=False,
            use_cache=False,
        )
        plan = plan_response.content
    except Exception as e:
        logger.error("Planning phase failed: %s", e)
        return CodingResult(
            task=task,
            plan="",
            final_summary="",
            error=f"Planning failed: {e}",
            tier=config.tier,
            orchestrator_model=config.orchestrator_model or "default",
            worker_model=config.worker_model or "default",
            duration_ms=int((time.monotonic() - start_time) * 1000),
        )

    logger.info("Plan created (%d chars)", len(plan))

    # ── Phase 2: Execute ───────────────────────────────────────────────
    logger.info("Agent Phase 2: Executing (worker=%s/%s)",
                config.worker_provider or "default",
                config.worker_model or "default")

    execution_task = (
        f"You have the following plan for a coding task. Execute it step "
        f"by step using the tools available to you.\n\n"
        f"## Plan\n{plan}\n\n"
        f"## Original Task\n{task}\n\n"
        f"Begin by reading the files mentioned in the plan, then make "
        f"the necessary changes."
    )

    # TODO: pass coding-specific system prompt to run_agent_loop once
    # it supports a system_prompt override parameter. For now the
    # generic AGENT_SYSTEM_PROMPT in agent_loop.py is used.
    _ = system_prompt  # reserved for future use

    max_verify_retries = 2
    exec_result: AgentResult | None = None
    verification = ""

    for verify_round in range(1 + max_verify_retries):
        if verify_round > 0:
            # Append verification feedback for retry
            execution_task = (
                f"{execution_task}\n\n"
                f"## Reviewer Feedback (round {verify_round})\n"
                f"The reviewer found issues with your previous changes:\n\n"
                f"{verification}\n\n"
                f"Please fix the issues above."
            )

        exec_result = await run_agent_loop(
            task=execution_task,
            engine=engine,
            tools=tools,
            provider=config.worker_provider,
            model=config.worker_model,
            max_iterations=config.max_iterations,
            auto_approve_safe=True,
            on_step=on_step,
            confirm_unsafe=confirm_write,
        )

        if not exec_result.completed and exec_result.error:
            logger.warning("Execution failed: %s", exec_result.error)
            break

        # ── Phase 3: Verify ────────────────────────────────────────────
        if not config.verify_results:
            break

        logger.info("Agent Phase 3: Verifying (orchestrator)")

        # Build a summary of what the worker did
        changes_summary = _build_changes_summary(exec_result)
        verify_prompt = (
            f"You are reviewing changes made by a coding agent. Check for:\n"
            f"1. Correctness — do the changes actually solve the task?\n"
            f"2. Completeness — is anything missing?\n"
            f"3. Safety — are there any bugs, security issues, or regressions?\n\n"
            f"## Original Task\n{task}\n\n"
            f"## Changes Made\n{changes_summary}\n\n"
            f"If the changes look good, respond with: APPROVED\n"
            f"If there are issues, describe them specifically and respond with: NEEDS_FIX"
        )

        try:
            verify_response = await engine.query(
                prompt=verify_prompt,
                provider=config.orchestrator_provider,
                model=config.orchestrator_model,
                stream=False,
                use_cache=False,
            )
            verification = verify_response.content
        except Exception as e:
            logger.warning("Verification failed: %s", e)
            verification = f"Verification error: {e}"
            break

        if "APPROVED" in verification.upper():
            logger.info("Verification: APPROVED")
            break
        elif verify_round < max_verify_retries:
            logger.info("Verification: NEEDS_FIX — retrying (round %d/%d)",
                        verify_round + 1, max_verify_retries)
        else:
            logger.info("Verification: NEEDS_FIX — max retries reached")

    # ── Assemble result ────────────────────────────────────────────────
    modified, created, read = _extract_file_operations(exec_result) if exec_result else ([], [], [])
    commands = _extract_commands(exec_result) if exec_result else []
    elapsed = int((time.monotonic() - start_time) * 1000)

    return CodingResult(
        task=task,
        plan=plan,
        final_summary=exec_result.final_response if exec_result else "",
        files_modified=modified,
        files_created=created,
        files_read=read,
        commands_run=commands,
        total_iterations=exec_result.total_iterations if exec_result else 0,
        total_tool_calls=exec_result.total_tool_calls if exec_result else 0,
        completed=exec_result.completed if exec_result else False,
        verification=verification,
        total_cost_usd=Decimal("0"),  # TODO: track across engine calls
        duration_ms=elapsed,
        tier=config.tier,
        worker_model=config.worker_model or "default",
        orchestrator_model=config.orchestrator_model or "default",
    )


def _build_changes_summary(result: AgentResult) -> str:
    """Build a human-readable summary of what the agent did."""
    lines: list[str] = []
    for step in result.steps:
        for call, res in zip(step.tool_calls, step.tool_results):
            tool = call.get("tool", "")
            args = call.get("args", {})
            if tool == "write_file":
                path = args.get("path", "?")
                content_preview = args.get("content", "")[:200]
                lines.append(f"- Wrote `{path}`: {content_preview}...")
            elif tool == "shell":
                cmd = args.get("command", "?")
                out_preview = res.output[:100] if res.output else "(no output)"
                lines.append(f"- Ran `{cmd}`: {out_preview}")
            elif tool == "read_file":
                path = args.get("path", "?")
                lines.append(f"- Read `{path}`")
    if not lines:
        lines.append("(no tool calls recorded)")
    return "\n".join(lines)
