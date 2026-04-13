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
    """GPU tier — 6 levels from no-GPU to DGX Spark.

    Each tier determines which models can be loaded simultaneously,
    how many parallel workers to use, and how much to rely on cloud.
    """
    TIER_0 = "tier_0"  # no GPU or <16 GB — fully cloud
    TIER_1 = "tier_1"  # 16-23 GB (RTX 4060 Ti 16GB) — cloud orch, 7B local worker
    TIER_2 = "tier_2"  # 24-47 GB (RTX 3090, RTX 4090) — cloud orch, 27B local worker
    TIER_3 = "tier_3"  # 48-95 GB (A100 80GB, RTX A6000 48GB) — cloud orch, 70B worker, user-configurable single/multi
    TIER_4 = "tier_4"  # 96-127 GB (RTX 6000 Pro BSE 96GB) — cloud orch, dual-model: 70B + 32B
    TIER_5 = "tier_5"  # 128+ GB (DGX Spark, multi-GPU) — fully local: 3 models, parallel workers


# Tier description for display
TIER_DESCRIPTIONS: dict[AgentTier, str] = {
    AgentTier.TIER_0: "Fully cloud (no local GPU)",
    AgentTier.TIER_1: "Cloud orchestrator + 7B local worker",
    AgentTier.TIER_2: "Cloud orchestrator + 27B local worker",
    AgentTier.TIER_3: "Cloud orchestrator + 70B local worker (single/multi configurable)",
    AgentTier.TIER_4: "Cloud orchestrator + dual-model: 70B planner + 32B coder",
    AgentTier.TIER_5: "Fully local: Nemotron 70B planner + Llama 70B coder + Qwen 72B reviewer",
}


def detect_agent_tier(total_vram_gb: float) -> AgentTier:
    """Map total available VRAM to a tier.

    Uses total across all GPUs so multi-GPU setups get a higher tier
    even if no single card hits the threshold alone.
    """
    if total_vram_gb >= 128:
        return AgentTier.TIER_5
    if total_vram_gb >= 96:
        return AgentTier.TIER_4
    if total_vram_gb >= 48:
        return AgentTier.TIER_3
    if total_vram_gb >= 24:
        return AgentTier.TIER_2
    if total_vram_gb >= 16:
        return AgentTier.TIER_1
    return AgentTier.TIER_0


# ---------------------------------------------------------------------------
# Agent configuration
# ---------------------------------------------------------------------------


# Model recommendations per tier. Each key maps to (provider, model).
# None means "use whatever the engine routes to by default" (cloud).
#
# "reviewer" is optional — only used in multi-model mode (Tier 4-5).
# When present, a DIFFERENT model verifies the coder's output, which
# catches bugs the coder's architecture has blind spots for.
#
# The build_agent_config() function validates these against the
# registry and falls back to engine defaults when not available.
_TIER_MODELS: dict[AgentTier, dict[str, tuple[str | None, str | None]]] = {
    AgentTier.TIER_5: {
        # DGX Spark (128 GB+): three models loaded simultaneously.
        # Nemotron 70B for planning (NVIDIA-optimized reasoning),
        # Llama 3.3 70B for coding (strong general + coding, 128K ctx),
        # Qwen 2.5 Coder 72B for review (different architecture catches
        # different bugs). 3 × 40 GB Q4 = 120 GB, 8 GB headroom.
        "orchestrator": ("ollama", "ollama/nemotron:70b"),
        "worker": ("ollama", "ollama/llama3.3:70b"),
        "reviewer": ("ollama", "ollama/qwen2.5-coder:32b"),
    },
    AgentTier.TIER_4: {
        # RTX 6000 Pro BSE (96 GB): dual-model — 70B planner/reviewer +
        # 32B coder. 70B Q4 (40 GB) + 32B Q8 (34 GB) = 74 GB, 22 GB
        # headroom. Cloud handles planning for complex tasks.
        "orchestrator": (None, None),
        "worker": ("ollama", "ollama/llama3.3:70b"),
        "reviewer": ("ollama", "ollama/qwen2.5-coder:32b"),
    },
    AgentTier.TIER_3: {
        # 48-95 GB: single 70B model by default. Can be overridden
        # to multi-model (Qwen 32B + Gemma 9B) via --mode multi.
        # Single: Llama 70B Q4 (~40 GB) — one strong model.
        # Multi: Qwen 32B Q8 (34 GB) + Gemma 9B Q8 (9 GB) = 43 GB.
        "orchestrator": (None, None),
        "worker": ("ollama", "ollama/llama3.3:70b"),
        # Reviewer only used in --mode multi:
        "reviewer": ("ollama", "ollama/gemma2:9b"),
    },
    AgentTier.TIER_2: {
        # RTX 3090 / RTX 4090 (24 GB): single local model.
        # Gemma 2 27B Q4 (~16 GB) — strong coder in 24 GB envelope.
        "orchestrator": (None, None),
        "worker": ("ollama", "ollama/gemma2:27b"),
    },
    AgentTier.TIER_1: {
        # RTX 4060 Ti 16 GB: single small model.
        # Qwen 2.5 Coder 7B Q8 (~8 GB) — fits with room for context.
        "orchestrator": (None, None),
        "worker": ("ollama", "ollama/qwen2.5-coder:7b"),
    },
    AgentTier.TIER_0: {
        # No GPU: everything goes to cloud
        "orchestrator": (None, None),
        "worker": (None, None),
    },
}

# Multi-model alternate models for Tier 3 --mode multi
_TIER_3_MULTI_MODELS: dict[str, tuple[str | None, str | None]] = {
    "orchestrator": (None, None),
    "worker": ("ollama", "ollama/qwen2.5-coder:32b"),
    "reviewer": ("ollama", "ollama/gemma2:9b"),
}


class AgentMode(enum.StrEnum):
    """Single-model vs multi-model execution mode."""
    AUTO = "auto"      # system decides based on task complexity
    SINGLE = "single"  # one local model for all phases
    MULTI = "multi"    # separate planner, coder, reviewer models


@dataclass
class AgentConfig:
    """Configuration for a coding agent session."""
    tier: AgentTier
    mode: AgentMode = AgentMode.AUTO
    orchestrator_provider: str | None = None
    orchestrator_model: str | None = None
    worker_provider: str | None = None
    worker_model: str | None = None
    reviewer_provider: str | None = None
    reviewer_model: str | None = None
    max_parallel_workers: int = 1
    max_iterations: int = 10
    verify_results: bool = True
    quality_gates: bool = True  # run lint/test after changes
    git_integration: bool = False  # branch + commit
    use_memory: bool = True  # cross-session project memory


def build_agent_config(
    tier: AgentTier,
    registry=None,
    mode: AgentMode = AgentMode.AUTO,
) -> AgentConfig:
    """Build a concrete config for the given tier.

    If a registry is provided, validates that the recommended providers
    are actually available and falls back to engine defaults (cloud)
    when they're not. This means a Tier 1 user without Ollama installed
    still gets a working agent — just fully cloud.
    """
    # For Tier 3 in multi-mode, use the alternate model set
    if tier == AgentTier.TIER_3 and mode == AgentMode.MULTI:
        tier_models = _TIER_3_MULTI_MODELS
    else:
        tier_models = _TIER_MODELS[tier]

    def _resolve(role: str) -> tuple[str | None, str | None]:
        provider, model = tier_models.get(role, (None, None))
        if registry is not None and provider and not registry.has(provider):
            logger.info(
                "Tier %s %s %s not in registry — falling back to cloud",
                tier, role, provider,
            )
            return (None, None)
        return (provider, model)

    orch = _resolve("orchestrator")
    work = _resolve("worker")
    review = _resolve("reviewer")

    # Determine effective mode
    effective_mode = mode
    if mode == AgentMode.AUTO:
        # Auto: use multi-model on Tier 4+ where it's always beneficial
        if tier in (AgentTier.TIER_4, AgentTier.TIER_5):
            effective_mode = AgentMode.MULTI
        else:
            effective_mode = AgentMode.SINGLE

    return AgentConfig(
        tier=tier,
        mode=effective_mode,
        orchestrator_provider=orch[0],
        orchestrator_model=orch[1],
        worker_provider=work[0],
        worker_model=work[1],
        reviewer_provider=review[0] if effective_mode == AgentMode.MULTI else None,
        reviewer_model=review[1] if effective_mode == AgentMode.MULTI else None,
        max_parallel_workers=_parallel_workers(tier),
    )


def _parallel_workers(tier: AgentTier) -> int:
    """Max concurrent workers per tier."""
    return {
        AgentTier.TIER_0: 1,
        AgentTier.TIER_1: 1,
        AgentTier.TIER_2: 1,
        AgentTier.TIER_3: 1,
        AgentTier.TIER_4: 2,
        AgentTier.TIER_5: 4,
    }[tier]


def auto_detect_config(
    engine,
    mode: AgentMode = AgentMode.AUTO,
) -> AgentConfig:
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

    return build_agent_config(tier, registry=engine.registry, mode=mode)


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
    mode: AgentMode = AgentMode.SINGLE
    worker_model: str = ""
    orchestrator_model: str = ""
    reviewer_model: str = ""
    quality_gate_passed: bool | None = None  # None = not run
    quality_gate_output: str = ""
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
    on_token: Any = None,
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
        if on_token is not None:
            plan_response = await engine.query_stream(
                prompt=plan_prompt,
                provider=config.orchestrator_provider,
                model=config.orchestrator_model,
                on_token=on_token,
            )
        else:
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

        # In multi-model mode, use the dedicated reviewer model so a
        # DIFFERENT architecture checks the coder's work (catches
        # blind spots the coder's training data doesn't cover).
        # In single-model mode, fall back to the orchestrator.
        verify_provider = (
            config.reviewer_provider
            if config.mode == AgentMode.MULTI and config.reviewer_provider
            else config.orchestrator_provider
        )
        verify_model = (
            config.reviewer_model
            if config.mode == AgentMode.MULTI and config.reviewer_model
            else config.orchestrator_model
        )

        logger.info("Agent Phase 3: Verifying (reviewer=%s/%s)",
                     verify_provider or "default", verify_model or "default")

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
            if on_token is not None:
                verify_response = await engine.query_stream(
                    prompt=verify_prompt,
                    provider=verify_provider,
                    model=verify_model,
                    on_token=on_token,
                )
            else:
                verify_response = await engine.query(
                    prompt=verify_prompt,
                    provider=verify_provider,
                    model=verify_model,
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

    # ── Phase 4: Quality gates (lint/test) ────────────────────────────
    quality_passed: bool | None = None
    quality_output = ""
    modified, created, read = _extract_file_operations(exec_result) if exec_result else ([], [], [])
    commands = _extract_commands(exec_result) if exec_result else []

    if config.quality_gates and (modified or created):
        quality_passed, quality_output = await _run_quality_gates(
            working_dir, modified + created,
        )
        if quality_passed is False and exec_result and exec_result.completed:
            # Re-enter the loop with lint/test feedback
            logger.info("Quality gate failed — feeding errors back to worker")
            gate_task = (
                f"{execution_task}\n\n"
                f"## Quality Gate Failures\n"
                f"After your changes, the following quality checks failed:\n\n"
                f"```\n{quality_output[:2000]}\n```\n\n"
                f"Please fix these issues."
            )
            gate_result = await run_agent_loop(
                task=gate_task,
                engine=engine,
                tools=tools,
                provider=config.worker_provider,
                model=config.worker_model,
                max_iterations=5,
                auto_approve_safe=True,
                on_step=on_step,
                confirm_unsafe=confirm_write,
            )
            if gate_result.completed:
                exec_result = gate_result
                modified, created, read = _extract_file_operations(exec_result)
                commands = _extract_commands(exec_result)
                # Re-check quality
                quality_passed, quality_output = await _run_quality_gates(
                    working_dir, modified + created,
                )

    # ── Phase 5: Git integration ───────────────────────────────────────
    if config.git_integration and (modified or created):
        try:
            from nvh.core.agent_git import (
                commit_agent_changes,
                create_agent_branch,
                is_git_repo,
            )
            if is_git_repo(working_dir):
                branch = create_agent_branch(working_dir, task)
                if branch:
                    logger.info("Created branch: %s", branch)
                sha = commit_agent_changes(
                    working_dir, task, modified, created,
                )
                if sha:
                    logger.info("Committed changes: %s", sha)
        except ImportError:
            logger.debug("agent_git module not available — skipping git integration")
        except Exception as e:
            logger.warning("Git integration failed: %s", e)

    # ── Phase 6: Save memory ──────────────────────────────────────────
    if config.use_memory:
        try:
            from nvh.core.agent_memory import (
                load_memory,
                save_memory,
                update_memory_from_result,
            )
            memory = load_memory(working_dir)
            # Build a lightweight result proxy for memory update
            memory = update_memory_from_result(memory, {
                "task": task,
                "files_modified": modified,
                "files_created": created,
                "completed": exec_result.completed if exec_result else False,
            })
            save_memory(memory, working_dir)
        except ImportError:
            logger.debug("agent_memory module not available — skipping memory save")
        except Exception as e:
            logger.debug("Memory save failed: %s", e)

    # ── Assemble result ────────────────────────────────────────────────
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
        mode=config.mode,
        worker_model=config.worker_model or "default",
        orchestrator_model=config.orchestrator_model or "default",
        reviewer_model=config.reviewer_model or "",
        quality_gate_passed=quality_passed,
        quality_gate_output=quality_output,
    )


async def _run_quality_gates(
    working_dir: Path,
    changed_files: list[str],
) -> tuple[bool | None, str]:
    """Run lint and test quality gates on changed files.

    Returns (passed: bool | None, output: str). None means no gates
    were applicable (no Python files, no test runner found, etc.).
    """
    import subprocess

    outputs: list[str] = []
    all_passed = True

    # Gate 1: ruff lint (Python files only)
    py_files = [f for f in changed_files if f.endswith(".py")]
    if py_files:
        try:
            result = subprocess.run(
                ["python", "-m", "ruff", "check", *py_files],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(working_dir),
                timeout=30,
            )
            if result.returncode != 0:
                all_passed = False
                outputs.append(f"ruff lint FAILED:\n{result.stdout or result.stderr}")
            else:
                outputs.append("ruff lint: passed")
        except FileNotFoundError:
            outputs.append("ruff: not installed (skipped)")
        except Exception as e:
            outputs.append(f"ruff: error ({e})")

    # Gate 2: syntax check (Python files)
    for f in py_files:
        try:
            result = subprocess.run(
                ["python", "-c", f"import ast; ast.parse(open(r'{f}').read())"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(working_dir),
                timeout=10,
            )
            if result.returncode != 0:
                all_passed = False
                outputs.append(f"syntax check FAILED for {f}:\n{result.stderr}")
        except Exception:
            pass

    if not outputs:
        return None, ""

    return all_passed, "\n".join(outputs)


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
