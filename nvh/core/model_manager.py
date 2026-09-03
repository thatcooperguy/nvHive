"""Model lifecycle manager — handles loading, unloading, and swapping
models to fit within GPU framebuffer (VRAM) constraints.

On GPUs with limited VRAM (24-96 GB), running several 30B-class models
simultaneously isn't always possible. This module manages the model
lifecycle:

1. Tracks which models are currently loaded in Ollama
2. Calculates VRAM requirements for requested models
3. Unloads models that aren't needed to make room
4. Loads required models with progress feedback
5. Preserves conversation context across model swaps

The key challenge: when we unload a model to make room for another,
we don't lose the CONTEXT from the previous model's work. The
agent's plan, tool results, and accumulated knowledge are stored
in the agent loop's message history — not in the model's memory.
Model swaps are transparent to the agent.

Usage:
    manager = ModelManager(vram_gb=96)
    plan = manager.plan_swap("qwen3-coder:30b")  # what must unload for it to fit
    await manager.execute_swap(plan)              # unload + load, context preserved
    status = await manager.get_vram_status()      # what's loaded, VRAM usage
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass

from nvh.core import local_models

logger = logging.getLogger(__name__)


# Loaded size per model tag in GB — the tier table's ``runtime_gb`` (weights
# plus KV-cache and CUDA-context headroom), so this manager, the emulator and
# the Wizard agree on what a model costs. Kept a plain dict for callers that
# ``.get()`` it; the lookups below go through _model_size_gb() so a
# ``name:latest`` tag from ``ollama ps`` resolves too.
MODEL_VRAM_GB: dict[str, float] = dict(local_models.size_table())

# Daemon-wide overhead (Ollama runner, CUDA context) counted once on top of
# the loaded models.
VRAM_OVERHEAD_GB = 2.0

# What a tag the table does not know is assumed to take.
DEFAULT_MODEL_VRAM_GB = 5.0


def _model_size_gb(name: str) -> float:
    """Loaded size for an Ollama tag: its table row (``moondream:latest`` finds ``moondream``) or the default."""
    if name in MODEL_VRAM_GB:
        return MODEL_VRAM_GB[name]
    pick = local_models.pick_for_tag(name)
    return pick.runtime_gb if pick is not None else DEFAULT_MODEL_VRAM_GB


@dataclass
class ModelStatus:
    """Status of a model in Ollama."""
    name: str
    loaded: bool
    size_gb: float  # approximate VRAM when loaded
    last_used: float = 0.0  # timestamp


@dataclass
class VRAMStatus:
    """Current VRAM allocation status."""
    total_gb: float
    used_gb: float
    available_gb: float
    loaded_models: list[ModelStatus]
    can_fit: list[str]  # models that could fit in remaining VRAM
    must_unload_for: dict[str, list[str]]  # model → what to unload


@dataclass
class SwapPlan:
    """Plan for swapping models to fit a new one."""
    target_model: str
    unload: list[str]  # models to unload first
    estimated_free_after: float  # GB free after unloading
    fits: bool  # will the target fit?
    message: str  # human-readable explanation


class ModelManager:
    """Manages model loading/unloading within VRAM constraints.

    Communicates with Ollama via its CLI/API to track which models
    are loaded and manage the lifecycle.
    """

    def __init__(self, vram_gb: float = 24.0, ollama_host: str = "http://localhost:11434"):
        self.vram_gb = vram_gb
        self.ollama_host = ollama_host
        self._loaded: dict[str, ModelStatus] = {}

    async def get_loaded_models(self) -> list[ModelStatus]:
        """Query Ollama for currently loaded models."""
        try:
            result = subprocess.run(
                ["ollama", "ps"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=10,
            )
            if result.returncode != 0:
                return []

            models: list[ModelStatus] = []
            for line in result.stdout.strip().split("\n")[1:]:  # skip header
                parts = line.split()
                if parts:
                    name = parts[0]
                    size_gb = _model_size_gb(name)
                    models.append(ModelStatus(
                        name=name, loaded=True, size_gb=size_gb,
                        last_used=time.time(),
                    ))
            self._loaded = {m.name: m for m in models}
            return models
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []

    async def get_vram_status(self) -> VRAMStatus:
        """Get current VRAM allocation status."""
        loaded = await self.get_loaded_models()
        used = sum(m.size_gb for m in loaded) + VRAM_OVERHEAD_GB
        available = max(0, self.vram_gb - used)

        can_fit = [
            name for name, size in MODEL_VRAM_GB.items()
            if size <= available and name not in self._loaded
        ]

        must_unload: dict[str, list[str]] = {}
        for target_name, target_size in MODEL_VRAM_GB.items():
            if target_name in self._loaded:
                continue
            if target_size <= available:
                must_unload[target_name] = []
            else:
                needed = target_size - available
                unload_candidates = sorted(
                    loaded, key=lambda m: m.last_used,
                )
                to_unload: list[str] = []
                freed = 0.0
                for m in unload_candidates:
                    to_unload.append(m.name)
                    freed += m.size_gb
                    if freed >= needed:
                        break
                if freed >= needed:
                    must_unload[target_name] = to_unload

        return VRAMStatus(
            total_gb=self.vram_gb,
            used_gb=used,
            available_gb=available,
            loaded_models=loaded,
            can_fit=can_fit,
            must_unload_for=must_unload,
        )

    def plan_swap(self, target_model: str) -> SwapPlan:
        """Plan how to load a target model within VRAM constraints.

        Returns a SwapPlan explaining what needs to happen. Does NOT
        execute — call execute_swap() to actually do it.
        """
        target_size = _model_size_gb(target_model)
        used = sum(m.size_gb for m in self._loaded.values()) + VRAM_OVERHEAD_GB
        available = max(0, self.vram_gb - used)

        if target_model in self._loaded:
            return SwapPlan(
                target_model=target_model,
                unload=[],
                estimated_free_after=available,
                fits=True,
                message=f"{target_model} is already loaded.",
            )

        if target_size <= available:
            return SwapPlan(
                target_model=target_model,
                unload=[],
                estimated_free_after=available - target_size,
                fits=True,
                message=f"{target_model} fits in {available:.1f} GB available VRAM.",
            )

        # Need to unload something
        needed = target_size - available
        candidates = sorted(
            self._loaded.values(),
            key=lambda m: m.last_used,
        )
        to_unload: list[str] = []
        freed = 0.0
        for m in candidates:
            to_unload.append(m.name)
            freed += m.size_gb
            if freed >= needed:
                break

        if freed >= needed:
            return SwapPlan(
                target_model=target_model,
                unload=to_unload,
                estimated_free_after=available + freed - target_size,
                fits=True,
                message=(
                    f"Need to unload {', '.join(to_unload)} "
                    f"(freeing {freed:.1f} GB) to fit {target_model} "
                    f"({target_size:.1f} GB). Context is preserved — "
                    f"only the model weights are swapped."
                ),
            )

        return SwapPlan(
            target_model=target_model,
            unload=list(self._loaded.keys()),
            estimated_free_after=self.vram_gb - VRAM_OVERHEAD_GB,
            fits=(self.vram_gb - VRAM_OVERHEAD_GB) >= target_size,
            message=(
                f"{target_model} ({target_size:.1f} GB) exceeds total "
                f"VRAM ({self.vram_gb:.1f} GB) even with all models unloaded."
                if (self.vram_gb - VRAM_OVERHEAD_GB) < target_size
                else f"Need to unload ALL loaded models to fit {target_model}."
            ),
        )

    async def execute_swap(self, plan: SwapPlan, on_progress=None) -> bool:
        """Execute a swap plan — unload old models, load the new one.

        Important: conversation context is NOT lost during a swap.
        The agent's plan, tool results, and accumulated knowledge
        live in the message history (Python memory), not in the
        model's weights. Swapping models is like switching which
        expert is reading the same shared document.

        Args:
            plan: SwapPlan from plan_swap()
            on_progress: callback(message: str) for status updates
        """
        if not plan.fits:
            logger.warning("Swap plan says model won't fit: %s", plan.message)
            return False

        if plan.target_model in self._loaded and not plan.unload:
            return True  # already loaded

        # Unload models
        for model_name in plan.unload:
            if on_progress:
                on_progress(f"Unloading {model_name} to free VRAM...")
            try:
                subprocess.run(
                    ["ollama", "stop", model_name],
                    capture_output=True, timeout=30,
                )
                self._loaded.pop(model_name, None)
                logger.info("Unloaded %s", model_name)
            except Exception as e:
                logger.warning("Failed to unload %s: %s", model_name, e)

        # Load target model (warm it up with a tiny prompt)
        if on_progress:
            on_progress(f"Loading {plan.target_model}...")

        try:
            result = subprocess.run(
                ["ollama", "run", plan.target_model, "hi"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                target_size = _model_size_gb(plan.target_model)
                self._loaded[plan.target_model] = ModelStatus(
                    name=plan.target_model,
                    loaded=True,
                    size_gb=target_size,
                    last_used=time.time(),
                )
                logger.info("Loaded %s", plan.target_model)
                if on_progress:
                    on_progress(f"{plan.target_model} ready. Context preserved.")
                return True
            else:
                logger.error("Failed to load %s: %s", plan.target_model, result.stderr[:200])
                return False
        except subprocess.TimeoutExpired:
            logger.error("Timeout loading %s (120s)", plan.target_model)
            return False
        except FileNotFoundError:
            logger.error("Ollama not found - install rootlessly: nvh studio --install rootless-ollama -y")
            return False

    def format_status(self) -> str:
        """Format the current model status for display."""
        if not self._loaded:
            return "[dim]No models loaded in Ollama.[/dim]"

        lines = ["[bold]Loaded Models:[/bold]"]
        total_used = VRAM_OVERHEAD_GB
        for m in self._loaded.values():
            total_used += m.size_gb
            lines.append(f"  {m.name:30s}  {m.size_gb:.1f} GB")
        lines.append(f"  {'VRAM used:':30s}  {total_used:.1f} / {self.vram_gb:.1f} GB")
        lines.append(f"  {'Available:':30s}  {max(0, self.vram_gb - total_used):.1f} GB")
        return "\n".join(lines)
