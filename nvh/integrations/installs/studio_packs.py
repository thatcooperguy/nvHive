"""Rootless AI Studio pack catalog and installers.

Packs are intentionally user-space only: files, launchers, models, and caches
go under ``NVH_HOME``. The installer never calls sudo, apt,
dnf, pacman, or systemctl. Container-backed packs only run when a provider
already exposes Docker without sudo.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

from nvh.core import local_models
from nvh.core.local_models import LocalModelPick, LocalModelTier
from nvh.integrations.installs.node_runtime import (
    find_fnm_binary,
    find_rootless_node_bin,
    install_node_tarball,
)
from nvh.integrations.workspace.storage import storage_layout
from nvh.utils.gpu import detect_gpus, detect_system_memory, is_unified_memory_gpu_name

OLLAMA_PORT = 11434
BLENDER_VERSION = "4.5.4"
BLENDER_MAJOR_MINOR = "4.5"
BLENDER_LINUX_X64_URL = (
    "https://download.blender.org/release/Blender4.5/"
    f"blender-{BLENDER_VERSION}-linux-x64.tar.xz"
)
NODE_MAJOR_VERSION = "22"
NODE_MIN_VERSION = (22, 16, 0)
NPM_MIN_VERSION = (10, 0, 0)
OPENCLAW_PACKAGE = "openclaw@latest"
OPENCLAW_DOC_URL = "https://openclawdoc.com/docs/getting-started/installation/"
NEMOCLAW_INSTALL_URL = "https://www.nvidia.com/nemoclaw.sh"
NEMOCLAW_DOC_URL = "https://docs.nvidia.com/nemoclaw/latest/get-started/quickstart.html"
NEMOCLAW_PACKAGE = "nemoclaw@latest"
NVIDIA_OMNI_BLOG_URL = (
    "https://blogs.nvidia.com/blog/nemotron-3-nano-omni-multimodal-ai-agents/"
    "?nvid=nv-int-csfg-551280"
)
NVIDIA_OMNI_TECH_BLOG_URL = (
    "https://developer.nvidia.com/blog/"
    "nvidia-nemotron-3-nano-omni-powers-multimodal-agent-reasoning-in-a-single-efficient-open-model"
)
NVIDIA_OMNI_HF_URL = (
    "https://huggingface.co/nvidia/"
    "Nemotron-3-Nano-Omni-30B-A3B-Reasoning-BF16"
)
NVIDIA_BUILD_URL = "https://build.nvidia.com/"
GODOT_RELEASE_API = "https://api.github.com/repos/godotengine/godot/releases/latest"
GODOT_DOC_URL = "https://docs.godotengine.org/en/stable/"
ACE_STEP_REPO_URL = "https://github.com/ACE-Step/ACE-Step-1.5.git"
ACE_STEP_DOC_URL = "https://github.com/ACE-Step/ACE-Step-1.5/blob/main/docs/en/INSTALL.md"
AUDACITY_RELEASE_API = "https://api.github.com/repos/audacity/audacity/releases/latest"
LMMS_RELEASE_API = "https://api.github.com/repos/LMMS/lmms/releases/latest"


@dataclass(frozen=True)
class ComfyNode:
    name: str
    repo_url: str


@dataclass(frozen=True)
class StudioPack:
    id: str
    title: str
    category: str
    tagline: str
    description: str
    recommended_vram_gb: int
    estimated_disk_gb: float
    install_kind: str
    no_root: bool
    models: list[str]
    python_packages: list[str]
    comfy_nodes: list[ComfyNode]
    launchers: list[str]
    source_urls: list[str]
    notes: list[str]


@dataclass(frozen=True)
class StudioModel:
    id: str
    title: str
    provider: str
    install_target: str
    category: str
    recommended_vram_gb: int
    estimated_disk_gb: float
    priority: int
    capabilities: list[str]
    why_recommended: str
    source_url: str
    license_note: str


# --- local model catalog: rows generated from nvh.core.local_models ----------
#
# Every tag, size, VRAM floor and category below is read off LOCAL_MODEL_TIERS,
# the one registry-verified VRAM-tier table. Only the prose is written here,
# keyed by the table's catalog id; a pick the table adds later still gets a
# row (with a generated title and reason) so the WebUI picker never lags the
# ladder, and a tag the table drops disappears from the picker with it.

_LICENSE_BY_NAME: dict[str, str] = {
    "llama3.2-vision": "Meta Llama license and Ollama library terms apply.",
    "nemotron3": "NVIDIA Open Model License and Ollama library terms apply.",
}
_DEFAULT_LICENSE_NOTE = "Ollama library terms apply."

# catalog_id -> (title, why_recommended, extra capability words)
_MODEL_PROSE: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "gemma3-1b": (
        "Gemma 3 1B",
        "Tiny chat model that runs from system RAM on CPU-only sessions.",
        ("small",),
    ),
    "qwen3-1.7b": (
        "Qwen 3 1.7B",
        "Smallest coder and step-by-step reasoner; runs on the CPU.",
        ("homework helper", "small"),
    ),
    "moondream": (
        "Moondream",
        "Tiny vision fallback for constrained GPUs or CPU-only sessions.",
        ("image Q&A", "desktop screenshots", "small"),
    ),
    "nomic-embed-text": (
        "Nomic Embed Text",
        "Small embedding model for local search and document experiments.",
        ("search", "RAG"),
    ),
    "gemma3-4b": (
        "Gemma 3 4B",
        "Best first local model for small student GPUs; it sees images too.",
        (),
    ),
    "qwen3-4b": (
        "Qwen 3 4B",
        "Compact coder and reasoner for 4-6 GB entry cards.",
        ("debugging", "homework helper"),
    ),
    "qwen3-8b": (
        "Qwen 3 8B",
        "Strong general-purpose chat, coding and reasoning model for 8 GB+ GPUs.",
        ("multilingual",),
    ),
    "qwen3-vl-8b": (
        "Qwen 3 VL 8B",
        "Real vision model that fits next to an 8B chat model on 12 GB cards.",
        ("image Q&A", "desktop screenshots"),
    ),
    "qwen3-14b": (
        "Qwen 3 14B",
        "Larger dense chat and coding model for 16 GB cards.",
        ("multilingual",),
    ),
    "llama32-vision": (
        "Llama 3.2 Vision",
        "Best local vision fallback for screenshots and uploaded images on 16 GB+ GPUs.",
        ("image Q&A", "desktop screenshots"),
    ),
    "gpt-oss-20b": (
        "gpt-oss 20B (MoE)",
        "Open-weight reasoning MoE from OpenAI; the first MoE that fits a 16 GB card.",
        ("math", "step-by-step", "agent"),
    ),
    "qwen3-30b-a3b": (
        "Qwen 3 30B-A3B (MoE)",
        "30B-class chat quality with 3B active parameters per token on 24 GB+ GPUs.",
        ("multilingual", "large-model"),
    ),
    "qwen3-coder-30b": (
        "Qwen 3 Coder 30B (MoE)",
        "Dedicated coding MoE for programming, debugging and agent planning on 24 GB+ GPUs.",
        ("debugging", "agent", "large-model"),
    ),
    "nemotron3-33b": (
        "NVIDIA Nemotron 3 Nano Omni 30B (MoE, multimodal)",
        "NVIDIA's multimodal Wizard model: images, screenshots and documents "
        "alongside text, tool calling and 128K context. Leads the 40 GB tier.",
        ("agent", "large-model"),
    ),
    "nemotron3-33b-q8": (
        "NVIDIA Nemotron 3 Nano Omni 30B (MoE, multimodal, Q8_0)",
        "Nemotron 3 Nano Omni at Q8_0 for 48 GB+ workstations and unified pools.",
        ("agent", "large-model"),
    ),
    "gpt-oss-120b": (
        "gpt-oss 120B (MoE)",
        "The largest open-weight reasoning MoE from OpenAI; fits 80 GB datacenter cards.",
        ("math", "agent", "large-model"),
    ),
}

# Table use case -> picker category / capability word. model_fit scores
# "coding" / "reasoning" / "embedding" / "vision" / "fast" / "agent" /
# "large-model"; the mission builder filters on the "code" and "embedding"
# categories.
_CATEGORY_BY_USE_CASE: dict[str, str] = {
    "chat": "chat",
    "code": "code",
    "vision": "vision",
    "reasoning": "reasoning",
    "embed": "embedding",
    "cpu_fallback": "chat",
}
_CAPABILITY_BY_USE_CASE: dict[str, str] = {
    "chat": "chat",
    "code": "coding",
    "vision": "vision",
    "reasoning": "reasoning",
    "embed": "embedding",
    "cpu_fallback": "fast",
}


def _first_tier_index(pick: LocalModelPick) -> int:
    """Index of the lowest tier that lists ``pick`` -- the budget it first fits."""
    for index, tier in enumerate(local_models.LOCAL_MODEL_TIERS):
        if any(candidate.tag == pick.tag for candidate in tier.picks.values()):
            return index
    return len(local_models.LOCAL_MODEL_TIERS) - 1


def _use_cases_for(pick: LocalModelPick) -> list[str]:
    """Use cases the pick fills anywhere in the table, in ``USE_CASES`` order."""
    served = {
        use_case
        for tier in local_models.LOCAL_MODEL_TIERS
        for use_case, candidate in tier.picks.items()
        if candidate.tag == pick.tag
    }
    return [use_case for use_case in local_models.USE_CASES if use_case in served]


def _studio_model_from_pick(pick: LocalModelPick) -> StudioModel:
    tier_index = _first_tier_index(pick)
    tier = local_models.LOCAL_MODEL_TIERS[tier_index]
    use_cases = _use_cases_for(pick) or ["chat"]
    primary = use_cases[0]
    title, why, extra = _MODEL_PROSE.get(
        pick.catalog_id,
        (pick.tag, local_models.reason_for(tier.min_gb, pick), ()),
    )
    capabilities = list(dict.fromkeys(
        [_CAPABILITY_BY_USE_CASE[use_case] for use_case in use_cases]
        + (["vision", "multimodal"] if pick.vision else [])
        + (["moe"] if pick.moe else [])
        + list(extra)
    ))
    return StudioModel(
        id=pick.catalog_id,
        title=title,
        provider="ollama",
        install_target=pick.tag,
        category=_CATEGORY_BY_USE_CASE[primary],
        recommended_vram_gb=int(tier.min_gb),
        estimated_disk_gb=pick.weights_gb,
        # Strongest first: the top tier's picks rank lowest (1..), the CPU
        # tier's highest -- the order the picker and `nvh models pull
        # --recommended` present rows in, and what model_fit scores from.
        priority=(len(local_models.LOCAL_MODEL_TIERS) - 1 - tier_index) * 10
        + local_models.USE_CASES.index(primary)
        + 1,
        capabilities=capabilities,
        why_recommended=why,
        source_url=f"https://ollama.com/library/{pick.name}",
        license_note=_LICENSE_BY_NAME.get(pick.name, _DEFAULT_LICENSE_NOTE),
    )


STUDIO_MODELS: list[StudioModel] = sorted(
    (_studio_model_from_pick(pick) for pick in local_models.all_picks()),
    key=lambda model: model.priority,
)


def _tier_by_label(label: str) -> LocalModelTier:
    for tier in local_models.LOCAL_MODEL_TIERS:
        if tier.label == label:
            return tier
    raise KeyError(f"Unknown local model tier: {label}")


def _unique_picks(picks: list[LocalModelPick | None]) -> list[LocalModelPick]:
    seen: dict[str, LocalModelPick] = {}
    for pick in picks:
        if pick is not None and pick.tag not in seen:
            seen[pick.tag] = pick
    return list(seen.values())


def _pack_disk_gb(picks: list[LocalModelPick]) -> float:
    return round(sum(pick.weights_gb for pick in picks), 1)


def _library_urls(picks: list[LocalModelPick]) -> list[str]:
    return sorted({f"https://ollama.com/library/{pick.name}" for pick in picks})


# The model packs are cut from the table. The starter pack is the pull list
# of the first tier with a real 8B chat model ("small", 8 GB); the coder /
# reasoner pack adds the dedicated code and reasoning picks of the first tier
# that carries a reasoning MoE ("medium", 16 GB) -- below it code and
# reasoning are the starter's own chat model.
_STARTER_TIER = _tier_by_label("small")
_STARTER_PICKS = local_models.recommended(_STARTER_TIER.min_gb)
_CODER_TIER = _tier_by_label("medium")
_CODER_PICKS = _unique_picks([_CODER_TIER.picks["code"], _CODER_TIER.picks["reasoning"]])

# Nemotron 3 Nano Omni for the NVIDIA Omni Agent pack: every quant the table
# carries, smallest first, and the tier the smallest one first fits.
_OMNI_PICKS: list[LocalModelPick] = sorted(
    (pick for pick in local_models.all_picks() if pick.name == "nemotron3"),
    key=lambda pick: pick.weights_gb,
) or [pick for pick in local_models.all_picks() if pick.vision and pick.moe][:1]
_OMNI_TIER = local_models.LOCAL_MODEL_TIERS[
    min((_first_tier_index(pick) for pick in _OMNI_PICKS), default=len(local_models.LOCAL_MODEL_TIERS) - 1)
]
# Free persistent storage the Wizard wants before recommending local Omni
# weights: room for every published quant of the model at once.
_OMNI_MIN_FREE_GB: int = math.ceil(sum(pick.weights_gb for pick in _OMNI_PICKS))


def _omni_model_sizes_gb() -> dict[str, float]:
    """``{quant: GB on disk}`` for the Omni picks -- what the plan and pack status report."""
    return {pick.quant: pick.weights_gb for pick in _OMNI_PICKS}


STUDIO_PACKS: list[StudioPack] = [
    StudioPack(
        id="rootless-ollama",
        title="Rootless Ollama Runtime",
        category="runtime",
        tagline="Local model server without sudo",
        description=(
            "Installs the Ollama Linux bundle into NVH_HOME, writes a user launcher, "
            "and stores models under NVH_HOME/models/ollama."
        ),
        recommended_vram_gb=0,
        estimated_disk_gb=1.0,
        install_kind="rootless_ollama",
        no_root=True,
        models=[],
        python_packages=[],
        comfy_nodes=[],
        launchers=["nvhive-ollama-serve"],
        source_urls=["https://docs.ollama.com/linux"],
        notes=[
            "NVH_HOME/bin must be on PATH for the plain ollama command.",
            "If a system Ollama already exists, nvHive uses it instead of replacing it.",
        ],
    ),
    StudioPack(
        id="python-runtime-fallback",
        title="Rootless Python Runtime Fallback",
        category="runtime",
        tagline="Micromamba rescue kit when venv is broken",
        description=(
            "Keeps nvHive's default path on Python venv and pip, but installs a "
            "micromamba binary under NVH_HOME for cloud images that lack working "
            "virtualenv or Python build tooling."
        ),
        recommended_vram_gb=0,
        estimated_disk_gb=0.2,
        install_kind="micromamba_runtime",
        no_root=True,
        models=[],
        python_packages=[],
        comfy_nodes=[],
        launchers=["nvhive-micromamba"],
        source_urls=["https://mamba.readthedocs.io/en/latest/user_guide/micromamba.html"],
        notes=[
            "Not required on the normal nvHive path when Python venv and pip are available.",
            "Useful on locked-down cloud desktops where students cannot install OS packages.",
        ],
    ),
    StudioPack(
        id="llm-starter",
        title="Top Local LLM Starter",
        category="llm",
        tagline="Chat, vision, coding, and embeddings",
        description=(
            "Pulls compact, broadly useful Ollama models for student work -- the "
            f"{_STARTER_TIER.range_label} GB tier of the nvHive model table: "
            + ", ".join(pick.tag for pick in _STARTER_PICKS) + "."
        ),
        recommended_vram_gb=int(_STARTER_TIER.min_gb),
        estimated_disk_gb=_pack_disk_gb(_STARTER_PICKS),
        install_kind="ollama_models",
        no_root=True,
        models=[pick.tag for pick in _STARTER_PICKS],
        python_packages=[],
        comfy_nodes=[],
        launchers=[],
        source_urls=["https://ollama.com/library", *_library_urls(_STARTER_PICKS)],
        notes=[
            f"Good default pack for {_STARTER_TIER.min_gb:g} GB and larger NVIDIA GPUs.",
            "Model pulls can be several GB and may take a while on school Wi-Fi.",
        ],
    ),
    StudioPack(
        id="llm-coder-reasoner",
        title="Coder and Reasoner Models",
        category="llm",
        tagline="Code help, math, and slower thinking",
        description=(
            "Adds the dedicated coding and reasoning picks of the "
            f"{_CODER_TIER.range_label} GB tier ("
            + ", ".join(pick.tag for pick in _CODER_PICKS)
            + ") for programming, math, debugging, and agent planning."
        ),
        recommended_vram_gb=int(_CODER_TIER.min_gb),
        estimated_disk_gb=_pack_disk_gb(_CODER_PICKS),
        install_kind="ollama_models",
        no_root=True,
        models=[pick.tag for pick in _CODER_PICKS],
        python_packages=[],
        comfy_nodes=[],
        launchers=[],
        source_urls=["https://ollama.com/library", *_library_urls(_CODER_PICKS)],
        notes=[
            "Use with nvHive Compare or Council mode when students want multiple opinions.",
            "Reasoning models can be slower; that is expected.",
        ],
    ),
    StudioPack(
        id="agent-lab",
        title="Local Agent Lab",
        category="agents",
        tagline="LangGraph, CrewAI, AutoGen, tools, and notebooks",
        description=(
            "Creates a dedicated Python environment for local agents, tool calling, "
            "search helpers, and student automation experiments."
        ),
        recommended_vram_gb=0,
        estimated_disk_gb=2.5,
        install_kind="python_venv",
        no_root=True,
        models=[],
        python_packages=[
            "langchain",
            "langgraph",
            "crewai",
            "autogen-agentchat",
            "duckduckgo-search",
            "httpx",
            "pydantic",
            "rich",
            "typer",
            "jupyterlab",
        ],
        comfy_nodes=[],
        launchers=["nvhive-agent-lab"],
        source_urls=[
            "https://github.com/langchain-ai/langgraph",
            "https://github.com/crewAIInc/crewAI",
            "https://github.com/microsoft/autogen",
        ],
        notes=[
            "Browser automation packages may need extra browser binaries later, but no sudo is used here.",
            "This pack gives the local AI agent layer a ready Python home.",
        ],
    ),
    StudioPack(
        id="nvidia-omni-agent",
        title="NVIDIA Omni Agent",
        category="agents",
        tagline="Optional multimodal Nemotron 3 Nano Omni upgrade for AI Starter",
        description=(
            "Adds an NVIDIA Omni Agent workspace that routes first to NVIDIA NIM/build.nvidia.com "
            "and only recommends local Nemotron 3 Nano Omni weights when GPU VRAM and persistent "
            "storage are large enough."
        ),
        recommended_vram_gb=int(_OMNI_TIER.min_gb),
        estimated_disk_gb=0.2,
        install_kind="scaffold",
        no_root=True,
        # The registry-verified Ollama tags of Nemotron 3 Nano Omni, smallest
        # quant first; the local path is `ollama pull <tag>`, not a GGUF fetch.
        models=[pick.tag for pick in _OMNI_PICKS],
        python_packages=[],
        comfy_nodes=[],
        launchers=["nvhive-omni-agent"],
        source_urls=[
            NVIDIA_OMNI_BLOG_URL,
            NVIDIA_OMNI_TECH_BLOG_URL,
            NVIDIA_OMNI_HF_URL,
            NVIDIA_BUILD_URL,
            *_library_urls(_OMNI_PICKS),
        ],
        notes=[
            "AI Starter installs this as a lightweight guide and launcher, not a default model download.",
            "Use NVIDIA NIM/build.nvidia.com first on smaller student VMs.",
            "Local weights via Ollama: " + "; ".join(
                f"{pick.tag} ({pick.quant}) is roughly {pick.weights_gb:g} GB on disk"
                for pick in _OMNI_PICKS
            ) + ".",
            "AI Wizard should require persistent storage headroom before recommending local weights.",
        ],
    ),
    StudioPack(
        id="openclaw-agent",
        title="OpenClaw Agent Workspace",
        category="claw",
        tagline="Self-hosted agent platform with local model support",
        description=(
            "Installs OpenClaw into a persistent user-space Node workspace, adds a "
            "launcher, and keeps agent state under NVH_HOME/studio instead of the base OS."
        ),
        recommended_vram_gb=0,
        estimated_disk_gb=2.0,
        install_kind="openclaw_agent",
        no_root=True,
        models=[],
        python_packages=[],
        comfy_nodes=[],
        launchers=["nvhive-openclaw"],
        source_urls=[OPENCLAW_DOC_URL, "https://openclaw.ai/install.sh"],
        notes=[
            "Requires Node.js 22.16+ and npm 10+; nvHive can install a rootless Node runtime on Linux.",
            "Use with local Ollama models or a configured cloud provider.",
        ],
    ),
    StudioPack(
        id="nemoclaw-sandbox",
        title="NVIDIA NemoClaw Sandbox",
        category="claw",
        tagline="OpenClaw inside NVIDIA OpenShell guardrails",
        description=(
            "Adds NVIDIA NemoClaw as the guarded OpenClaw path when the host exposes "
            "a Docker runtime that works without sudo. The wizard blocks this pack on "
            "locked-down sessions that cannot run containers."
        ),
        recommended_vram_gb=0,
        estimated_disk_gb=40.0,
        install_kind="nemoclaw_sandbox",
        no_root=True,
        models=[],
        python_packages=[],
        comfy_nodes=[],
        launchers=["nvhive-nemoclaw"],
        source_urls=[NEMOCLAW_DOC_URL, NEMOCLAW_INSTALL_URL],
        notes=[
            "NemoClaw is alpha software and requires a usable Docker/OpenShell path.",
            "Recommended only when the cloud image grants rootless Docker or docker group access.",
        ],
    ),
    StudioPack(
        id="comfyui-power-nodes",
        title="ComfyUI Power Nodes",
        category="comfyui",
        tagline="Manager, control, video, GGUF, and workflow quality-of-life",
        description=(
            "Installs common ComfyUI node packs into the nvHive ComfyUI environment: "
            "Manager, Impact Pack, ControlNet Aux, Video Helper Suite, GGUF, and rgthree."
        ),
        recommended_vram_gb=8,
        estimated_disk_gb=4.0,
        install_kind="comfy_nodes",
        no_root=True,
        models=[],
        python_packages=[],
        comfy_nodes=[
            ComfyNode("ComfyUI-Manager", "https://github.com/ltdrdata/ComfyUI-Manager.git"),
            ComfyNode("ComfyUI-Impact-Pack", "https://github.com/ltdrdata/ComfyUI-Impact-Pack.git"),
            ComfyNode("comfyui_controlnet_aux", "https://github.com/Fannovel16/comfyui_controlnet_aux.git"),
            ComfyNode("ComfyUI-VideoHelperSuite", "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git"),
            ComfyNode("ComfyUI-GGUF", "https://github.com/city96/ComfyUI-GGUF.git"),
            ComfyNode("rgthree-comfy", "https://github.com/rgthree/rgthree-comfy.git"),
        ],
        launchers=[],
        source_urls=[
            "https://docs.comfy.org/installation/install_custom_node",
            "https://docs.comfy.org/manager/pack-management",
            "https://docs.comfy.org/registry/overview",
        ],
        notes=[
            "Custom nodes run third-party code; use this curated pack instead of random unknown nodes.",
            "Restart ComfyUI after installing or updating nodes.",
        ],
    ),
    StudioPack(
        id="game-dev-lab",
        title="Linux Game Dev AI Lab",
        category="game",
        tagline="Pygame, Panda3D, assets, and modding helpers",
        description=(
            "Creates a no-root Python game development environment for AI-assisted "
            "prototypes, texture generation workflows, and personal game projects."
        ),
        recommended_vram_gb=0,
        estimated_disk_gb=2.0,
        install_kind="python_venv",
        no_root=True,
        models=[],
        python_packages=[
            "pygame-ce",
            "panda3d",
            "moderngl",
            "numpy",
            "pillow",
            "opencv-python",
            "pygltflib",
            "trimesh",
            "opensimplex",
        ],
        comfy_nodes=[],
        launchers=["nvhive-game-lab"],
        source_urls=[
            "https://www.pygame.org/",
            "https://www.panda3d.org/",
            "https://github.com/KhronosGroup/glTF",
        ],
        notes=[
            "This does not install Steam, drivers, overlays, or kernel-level tools.",
            "Use ComfyUI packs to generate textures, sprites, icons, and concept art.",
        ],
    ),
    StudioPack(
        id="game-mod-helper",
        title="Game Mod Helper",
        category="game",
        tagline="User-space folders and launch notes for mods",
        description=(
            "Writes a small modding workspace with Linux/Wine/Steam Deck notes, "
            "asset folders, and helper launch scripts. No system packages required."
        ),
        recommended_vram_gb=0,
        estimated_disk_gb=0.1,
        install_kind="scaffold",
        no_root=True,
        models=[],
        python_packages=[],
        comfy_nodes=[],
        launchers=["nvhive-mod-lab"],
        source_urls=[],
        notes=[
            "Game-specific mods still depend on each game's license and mod loader.",
            "The helper creates structure and docs; it does not bypass anti-cheat or DRM.",
        ],
    ),
    StudioPack(
        id="godot-engine",
        title="Godot Engine",
        category="game",
        tagline="Open-source game engine as a rootless app",
        description=(
            "Downloads the latest official Godot Linux x86_64 release into NVH_HOME/apps, "
            "adds a persistent launcher, and creates a project folder beside the rest of the lab."
        ),
        recommended_vram_gb=2,
        estimated_disk_gb=0.4,
        install_kind="godot_app",
        no_root=True,
        models=[],
        python_packages=[],
        comfy_nodes=[],
        launchers=["nvhive-godot"],
        source_urls=[GODOT_RELEASE_API, GODOT_DOC_URL],
        notes=[
            "Uses the official GitHub release asset selected at install time.",
            "Godot projects stay under persistent storage and can use Blender or ComfyUI assets.",
        ],
    ),
    StudioPack(
        id="unity-hub-helper",
        title="Unity Hub Helper",
        category="game",
        tagline="Persistent Unity workspace and account handoff",
        description=(
            "Creates a rootless Unity workspace with launcher notes for Unity Hub AppImage/manual "
            "installs. The wizard keeps the storage and cache paths ready, while Unity handles sign-in."
        ),
        recommended_vram_gb=6,
        estimated_disk_gb=12.0,
        install_kind="scaffold",
        no_root=True,
        models=[],
        python_packages=[],
        comfy_nodes=[],
        launchers=["nvhive-unity-hub"],
        source_urls=["https://unity.com/download"],
        notes=[
            "Unity requires a Unity account and license acceptance.",
            "Use the helper to keep projects and downloaded editors on the persistent block volume.",
        ],
    ),
    StudioPack(
        id="unreal-engine-helper",
        title="Unreal Engine Helper",
        category="game",
        tagline="Epic/GitHub prep for a large rootless UE workspace",
        description=(
            "Creates the persistent Unreal workspace, explains the Epic-to-GitHub account link, "
            "and prepares folders for source builds or provider-supplied Unreal installs."
        ),
        recommended_vram_gb=8,
        estimated_disk_gb=150.0,
        install_kind="scaffold",
        no_root=True,
        models=[],
        python_packages=[],
        comfy_nodes=[],
        launchers=["nvhive-unreal-helper"],
        source_urls=[
            "https://www.unrealengine.com/en-US/download",
            "https://www.unrealengine.com/en-US/ue-on-github",
        ],
        notes=[
            "Unreal access requires an Epic account and linked GitHub account.",
            "Large Unreal source/editor builds can exceed 150 GB; AI Wizard should reserve the block volume first.",
        ],
    ),
    StudioPack(
        id="github-login-helper",
        title="GitHub Connect",
        category="connector",
        tagline="Simple GitHub login helper for cloning and PR work",
        description=(
            "Adds a rootless GitHub login workspace and launcher that uses GitHub CLI when present "
            "or a GITHUB_TOKEN fallback for cloud images without system package access."
        ),
        recommended_vram_gb=0,
        estimated_disk_gb=0.1,
        install_kind="scaffold",
        no_root=True,
        models=[],
        python_packages=[],
        comfy_nodes=[],
        launchers=["nvhive-github-login"],
        source_urls=[
            "https://cli.github.com/manual/gh_auth_login",
            "https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens",
        ],
        notes=[
            "The helper never stores a password. Prefer GitHub CLI browser login or a fine-grained token.",
            "Public repositories can still clone over HTTPS without login.",
        ],
    ),
    StudioPack(
        id="blender-creative",
        title="Blender Creative Studio",
        category="creative",
        tagline="Official Blender LTS without sudo",
        description=(
            "Downloads the official Blender LTS Linux archive into NVH_HOME/apps, "
            "adds a persistent launcher, and creates project folders for AI-assisted "
            "3D, animation, and game asset work."
        ),
        recommended_vram_gb=4,
        estimated_disk_gb=1.2,
        install_kind="blender_app",
        no_root=True,
        models=[],
        python_packages=[],
        comfy_nodes=[],
        launchers=["nvhive-blender"],
        source_urls=[
            "https://www.blender.org/download/lts/",
            "https://download.blender.org/release/Blender4.5/",
        ],
        notes=[
            "Installs the portable tarball; no apt, snap, sudo, or system menu edits required.",
            "Cycles GPU rendering still depends on the NVIDIA driver exposed by the cloud image.",
        ],
    ),
    StudioPack(
        id="ace-step-music",
        title="ACE-Step Music Generator",
        category="music",
        tagline="Local AI songs, loops, lyrics, and remixes",
        description=(
            "Clones ACE-Step 1.5 into persistent storage, prepares a rootless uv "
            "environment, and adds a launcher for the local Gradio music studio."
        ),
        recommended_vram_gb=6,
        estimated_disk_gb=12.0,
        install_kind="ace_step_music",
        no_root=True,
        models=[],
        python_packages=[],
        comfy_nodes=[],
        launchers=["nvhive-ace-step"],
        source_urls=[ACE_STEP_REPO_URL, ACE_STEP_DOC_URL],
        notes=[
            "ACE-Step models download on first launch and can use several additional GB.",
            "Runs from the persistent block volume; no apt, sudo, or system Python edits.",
        ],
    ),
    StudioPack(
        id="music-producer-lab",
        title="Music Producer AI Lab",
        category="music",
        tagline="Stem splitting, transcription, audio generation, and notebooks",
        description=(
            "Creates a rootless Python audio lab for GPU-backed source separation, "
            "lyrics/transcription, prompt-to-audio experiments, and batch audio tools."
        ),
        recommended_vram_gb=8,
        estimated_disk_gb=8.0,
        install_kind="python_venv",
        no_root=True,
        models=[],
        python_packages=[
            "demucs",
            "whisperx",
            "faster-whisper",
            "stable-audio-tools",
            "audio-separator",
            "librosa",
            "soundfile",
            "gradio",
            "huggingface_hub",
            "jupyterlab",
        ],
        comfy_nodes=[],
        launchers=["nvhive-music-lab"],
        source_urls=[
            "https://github.com/m-bain/whisperX",
            "https://github.com/Stability-AI/stable-audio-tools",
            "https://docs.pytorch.org/audio/stable/tutorials/hybrid_demucs_tutorial.html",
        ],
        notes=[
            "CUDA acceleration depends on the PyTorch wheels that match the host driver.",
            "Use this for remixing and cleanup; use ACE-Step for full music generation.",
        ],
    ),
    StudioPack(
        id="music-daw-helper",
        title="Rootless DAW Helper",
        category="music",
        tagline="Audacity and LMMS AppImages plus DAW workspace",
        description=(
            "Downloads official Audacity and LMMS AppImages when available, "
            "then creates a persistent music production workspace with launch helpers."
        ),
        recommended_vram_gb=0,
        estimated_disk_gb=1.0,
        install_kind="scaffold",
        no_root=True,
        models=[],
        python_packages=[],
        comfy_nodes=[],
        launchers=["nvhive-music-studio"],
        source_urls=[
            AUDACITY_RELEASE_API,
            LMMS_RELEASE_API,
            "https://support.audacityteam.org/basics/downloading-and-installing-audacity",
            "https://lmms.io/download",
            "https://www.reaper.fm/download.php",
            "https://musescore.org/en/download",
        ],
        notes=[
            "Desktop apps remain in user space and can use AppImage extract-and-run when FUSE is unavailable.",
            "Commercial DAWs may require account login or license acceptance outside nvHive.",
        ],
    ),
]


PACK_BUNDLES: dict[str, list[str]] = {
    "starter": [
        "rootless-ollama",
        "llm-starter",
        "agent-lab",
        "nvidia-omni-agent",
        "comfyui-power-nodes",
        "game-dev-lab",
        "github-login-helper",
    ],
    "llms": ["rootless-ollama", "llm-starter", "llm-coder-reasoner"],
    "agents": ["agent-lab", "nvidia-omni-agent", "openclaw-agent", "github-login-helper"],
    "claw": ["openclaw-agent", "nemoclaw-sandbox"],
    "omni": ["nvidia-omni-agent"],
    "comfy": ["comfyui-power-nodes"],
    "connectors": ["github-login-helper"],
    "music": ["ace-step-music", "music-producer-lab", "music-daw-helper", "github-login-helper"],
    "game": ["game-dev-lab", "game-mod-helper", "godot-engine", "unity-hub-helper", "unreal-engine-helper", "github-login-helper"],
    "creative": ["blender-creative", "game-dev-lab", "game-mod-helper", "godot-engine"],
    "all": [
        "rootless-ollama",
        "llm-starter",
        "llm-coder-reasoner",
        "agent-lab",
        "nvidia-omni-agent",
        "openclaw-agent",
        "nemoclaw-sandbox",
        "comfyui-power-nodes",
        "game-dev-lab",
        "game-mod-helper",
        "godot-engine",
        "unity-hub-helper",
        "unreal-engine-helper",
        "github-login-helper",
        "blender-creative",
        "ace-step-music",
        "music-producer-lab",
        "music-daw-helper",
    ],
}


def studio_root() -> Path:
    configured = os.environ.get("NVH_STUDIO_HOME")
    if configured:
        return Path(configured).expanduser()
    return storage_layout().studio_dir


def _local_bin() -> Path:
    path = storage_layout().bin_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pack_root(pack_id: str) -> Path:
    return studio_root() / "packs" / pack_id


def _comfyui_root() -> Path:
    configured = os.environ.get("COMFYUI_HOME")
    if configured:
        return Path(configured).expanduser()
    return storage_layout().comfyui_dir


def _comfyui_app_dir() -> Path:
    return _comfyui_root() / "ComfyUI"


def _comfyui_venv_python() -> Path:
    venv = _comfyui_root() / "venv"
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _marker_path(pack_id: str) -> Path:
    return _pack_root(pack_id) / "installed.json"


def _venv_python(pack_id: str) -> Path:
    root = _pack_root(pack_id) / "venv"
    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def _blender_root() -> Path:
    return storage_layout().apps_dir / "blender"


def _blender_app_dir() -> Path:
    return _blender_root() / f"blender-{BLENDER_VERSION}-linux-x64"


def _blender_binary() -> Path:
    return _blender_app_dir() / "blender"


def _godot_root() -> Path:
    return storage_layout().apps_dir / "godot"


def _godot_current_file() -> Path:
    return _godot_root() / "current.json"


def _godot_binary_from_state() -> Path | None:
    state_file = _godot_current_file()
    if not state_file.exists():
        return None
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    binary = state.get("binary")
    if not isinstance(binary, str):
        return None
    path = Path(binary)
    return path if path.exists() else None


def _ace_step_root() -> Path:
    return _pack_root("ace-step-music")


def _ace_step_app_dir() -> Path:
    return _ace_step_root() / "ACE-Step-1.5"


def _ace_step_uv_venv_python() -> Path:
    venv = _ace_step_root() / "uv-venv"
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _ace_step_uv_binary() -> Path:
    venv = _ace_step_root() / "uv-venv"
    if os.name == "nt":
        return venv / "Scripts" / "uv.exe"
    return venv / "bin" / "uv"


def _node_runtime_root() -> Path:
    return storage_layout().runtime_dir / "node"


def _fnm_root() -> Path:
    return storage_layout().runtime_dir / "fnm"


def _openclaw_workspace() -> Path:
    return _pack_root("openclaw-agent") / "workspace"


def _openclaw_prefix() -> Path:
    return _pack_root("openclaw-agent") / "node"


def _openclaw_binary() -> Path:
    suffix = ".cmd" if os.name == "nt" else ""
    return _openclaw_prefix() / "bin" / f"openclaw{suffix}"


def _nemoclaw_workspace() -> Path:
    return _pack_root("nemoclaw-sandbox") / "workspace"


def _nemoclaw_prefix() -> Path:
    return _pack_root("nemoclaw-sandbox") / "node"


def _nemoclaw_binary_from_env(env: dict[str, str] | None = None) -> str:
    suffix = ".cmd" if os.name == "nt" else ""
    candidates = [
        _nemoclaw_prefix() / "bin" / f"nemoclaw{suffix}",
        _local_bin() / "nemoclaw",
        _pack_root("nemoclaw-sandbox") / "home" / ".local" / "bin" / "nemoclaw",
        _pack_root("nemoclaw-sandbox") / "home" / ".npm-global" / "bin" / "nemoclaw",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    found = shutil.which("nemoclaw", path=env.get("PATH") if env else None)
    return found or ""


def _parse_semver(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", value)
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())  # type: ignore[return-value]


def _semver_at_least(value: tuple[int, int, int] | None, minimum: tuple[int, int, int]) -> bool:
    return bool(value and value >= minimum)


def _run_capture(cmd: list[str], *, env: dict[str, str] | None = None, timeout: float = 8.0) -> str:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except Exception:
        return ""
    return ((result.stdout or result.stderr) or "").strip().splitlines()[0] if (result.stdout or result.stderr) else ""


def _find_rootless_node_bin() -> Path | None:
    return find_rootless_node_bin(storage_layout().runtime_dir, major=NODE_MAJOR_VERSION)


def _node_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.update(storage_layout().env())
    rootless_bin = _find_rootless_node_bin()
    path_parts = [
        str(_local_bin()),
        str(_openclaw_prefix() / "bin"),
        str(_nemoclaw_prefix() / "bin"),
        str(_pack_root("nemoclaw-sandbox") / "home" / ".local" / "bin"),
        str(_pack_root("nemoclaw-sandbox") / "home" / ".npm-global" / "bin"),
    ]
    if rootless_bin:
        path_parts.insert(0, str(rootless_bin))
    env["PATH"] = os.pathsep.join(path_parts + [env.get("PATH", "")])
    env["NPM_CONFIG_PREFIX"] = str(_openclaw_prefix())
    env["OPENCLAW_HOME"] = str(_openclaw_workspace())
    env["NEMOCLAW_WORKSPACE"] = str(_nemoclaw_workspace())
    if extra:
        env.update(extra)
    return env


def _node_runtime_status(env: dict[str, str] | None = None) -> dict[str, Any]:
    env = env or _node_env()
    node = shutil.which("node", path=env.get("PATH"))
    npm = shutil.which("npm", path=env.get("PATH"))
    node_text = _run_capture([node, "--version"], env=env) if node else ""
    npm_text = _run_capture([npm, "--version"], env=env) if npm else ""
    node_version = _parse_semver(node_text)
    npm_version = _parse_semver(npm_text)
    node_ok = _semver_at_least(node_version, NODE_MIN_VERSION)
    npm_ok = _semver_at_least(npm_version, NPM_MIN_VERSION)
    can_auto_install = (
        platform.system() == "Linux"
        and bool(shutil.which("bash"))
        and bool(shutil.which("curl"))
    )
    return {
        "node": node or "",
        "npm": npm or "",
        "node_version": node_text,
        "npm_version": npm_text,
        "node_ok": node_ok,
        "npm_ok": npm_ok,
        "ready": node_ok and npm_ok,
        "can_auto_install": can_auto_install,
        "minimum_node": ".".join(str(part) for part in NODE_MIN_VERSION),
        "minimum_npm": ".".join(str(part) for part in NPM_MIN_VERSION),
    }


def _docker_status() -> dict[str, Any]:
    docker = shutil.which("docker")
    if not docker:
        return {
            "binary": "",
            "ready": False,
            "detail": "Docker was not found on PATH.",
            "rootless_hint": "NemoClaw needs Docker or a provider-enabled rootless container runtime.",
        }
    try:
        result = subprocess.run(
            [docker, "info"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        return {
            "binary": docker,
            "ready": False,
            "detail": f"Docker could not be checked: {exc}",
            "rootless_hint": "Ask the provider to enable rootless Docker or docker group access.",
        }
    if result.returncode == 0:
        return {
            "binary": docker,
            "ready": True,
            "detail": "Docker daemon is reachable without sudo.",
            "rootless_hint": "",
        }
    detail = (result.stderr or result.stdout or "Docker daemon is not reachable.").strip().splitlines()[0]
    return {
        "binary": docker,
        "ready": False,
        "detail": detail,
        "rootless_hint": "NemoClaw is blocked until Docker works without sudo in this session.",
    }


def _prepare_node_runtime() -> tuple[dict[str, str], dict[str, Any]]:
    env = _node_env()
    status = _node_runtime_status(env)
    if status["ready"]:
        return env, status
    if not status["can_auto_install"]:
        raise RuntimeError(
            "OpenClaw needs Node.js 22.16+ and npm 10+. This host cannot auto-install "
            "the rootless Node runtime because Linux, bash, and curl are not all available."
        )

    fnm_dir = _fnm_root()
    fnm_dir.mkdir(parents=True, exist_ok=True)
    install_env = os.environ.copy()
    install_env.update(storage_layout().env())
    install_env["FNM_DIR"] = str(fnm_dir)
    install_env["NODE_VERSION"] = NODE_MAJOR_VERSION
    try:
        subprocess.run(
            ["bash", "-lc", "curl -fsSL https://fnm.vercel.app/install | bash -s -- --skip-shell"],
            check=True,
            timeout=180,
            env=install_env,
        )
    except Exception:
        install_node_tarball(storage_layout().runtime_dir, major=NODE_MAJOR_VERSION)

    fnm_value = find_fnm_binary(fnm_dir)
    if not fnm_value:
        install_node_tarball(storage_layout().runtime_dir, major=NODE_MAJOR_VERSION)
    else:
        try:
            subprocess.run(
                [fnm_value, "install", NODE_MAJOR_VERSION],
                check=True,
                timeout=300,
                env=install_env,
            )
        except Exception:
            install_node_tarball(storage_layout().runtime_dir, major=NODE_MAJOR_VERSION)

    env = _node_env()
    status = _node_runtime_status(env)
    if not status["ready"]:
        raise RuntimeError(
            f"Node runtime is still not ready. Node={status['node_version'] or 'missing'} "
            f"npm={status['npm_version'] or 'missing'}."
        )
    return env, status


def _find_pack(pack_id: str) -> StudioPack:
    for pack in STUDIO_PACKS:
        if pack.id == pack_id:
            return pack
    raise KeyError(f"Unknown studio pack: {pack_id}")


def expand_pack_ids(pack_ids: list[str] | tuple[str, ...] | None) -> list[str]:
    """Expand bundle names and comma-separated ids into unique pack ids."""
    if not pack_ids:
        return []

    expanded: list[str] = []
    for raw in pack_ids:
        for item in raw.split(","):
            pack_id = item.strip()
            if not pack_id:
                continue
            expanded.extend(PACK_BUNDLES.get(pack_id, [pack_id]))

    result: list[str] = []
    seen: set[str] = set()
    valid = {pack.id for pack in STUDIO_PACKS}
    for pack_id in expanded:
        if pack_id not in valid:
            raise KeyError(f"Unknown studio pack or bundle: {pack_id}")
        if pack_id not in seen:
            result.append(pack_id)
            seen.add(pack_id)
    return result


def catalog_as_dicts() -> list[dict[str, Any]]:
    return [asdict(pack) for pack in STUDIO_PACKS]


def model_catalog_as_dicts() -> list[dict[str, Any]]:
    return [asdict(model) for model in STUDIO_MODELS]


def bundles_as_dict() -> dict[str, list[str]]:
    return {key: list(value) for key, value in PACK_BUNDLES.items()}


def _nvidia_smi_rows() -> list[Any]:
    """Duck-typed GPU rows parsed from nvidia-smi when ``nvh.utils.gpu`` found none.

    ``local_models.tier_budget`` only needs ``vram_mb`` and ``unified_memory``.
    """
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return []
    try:
        result = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    rows: list[Any] = []
    for line in result.stdout.splitlines():
        name, _, memory = line.partition(",")
        memory = memory.strip()
        if not memory.isdigit():
            continue
        rows.append(
            SimpleNamespace(
                name=name.strip(),
                vram_mb=int(memory),
                unified_memory=is_unified_memory_gpu_name(name.strip()),
            )
        )
    return rows


def _detect_tier_budget() -> local_models.TierBudget:
    """``local_models.tier_budget`` for this machine -- what every model ladder plans against.

    Unified-aware: a 128 GB GB10 / DGX Spark budgets 112 GB (the pool minus
    the OS reserve), the same figure ``nvh.utils.gpu.recommend_models`` uses,
    so the catalog's ``fits_vram`` and ``recommended`` flags cannot disagree
    with it. Discrete cards budget their summed VRAM.
    """
    try:
        gpus: list[Any] = list(detect_gpus())
    except Exception:
        gpus = []
    if not gpus:
        gpus = _nvidia_smi_rows()
    try:
        sys_mem: Any = detect_system_memory()
    except Exception:
        sys_mem = None
    return local_models.tier_budget(gpus, sys_mem)


def _detect_vram_gb() -> int:
    """Whole GB the model ladder may plan against (see :func:`_detect_tier_budget`)."""
    return int(_detect_tier_budget().budget_gb)


def _fits_vram(model: StudioModel, vram_gb: int) -> bool:
    return model.recommended_vram_gb == 0 or (
        vram_gb > 0 and model.recommended_vram_gb <= vram_gb
    )


def _recommended_model_ids(vram_gb: int | float | local_models.TierBudget) -> set[str]:
    """Catalog ids of ``local_models.recommended`` for the budget.

    0 GB is the CPU tier (tiny models that run from system RAM), so a box with
    no GPU still gets a working starter set. A :class:`TierBudget` keeps the
    pool type, so a unified GB10 also gets the tier's reasoning MoE.
    """
    return {pick.catalog_id for pick in local_models.recommended(vram_gb)}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _ollama_binary(home_dir: str | Path | None = None) -> str:
    local = storage_layout(home_dir).bin_dir / "ollama"
    if _ollama_binary_usable(local, home_dir=home_dir):
        return str(local)
    found = shutil.which("ollama")
    if found and _ollama_binary_usable(Path(found), home_dir=home_dir):
        return found
    return ""


def _binary_file_probe(path: str | Path) -> dict[str, Any]:
    """Return a tiny, non-executing binary inspection for support reports."""
    file_path = Path(path)
    probe: dict[str, Any] = {
        "path": str(file_path),
        "exists": file_path.exists(),
        "is_file": file_path.is_file() if file_path.exists() else False,
        "executable": os.access(file_path, os.X_OK) if file_path.exists() else False,
        "size": None,
        "format": "missing",
        "elf_machine": None,
        "expected_arch": None,
        "arch_match": None,
    }
    if not file_path.exists() or not file_path.is_file():
        return probe

    try:
        stat_result = file_path.stat()
        probe["size"] = stat_result.st_size
        with file_path.open("rb") as handle:
            head = handle.read(64)
    except Exception as exc:
        probe["format"] = f"unreadable: {type(exc).__name__}"
        return probe

    stripped = head.lstrip()
    if stripped.startswith(b"#!"):
        probe["format"] = "script"
        return probe
    if stripped[:16].lower().startswith(b"<!doctype html") or stripped[:5].lower().startswith(b"<html"):
        probe["format"] = "html"
        return probe
    if head.startswith(b"\x7fELF"):
        machine = int.from_bytes(head[18:20], byteorder="little", signed=False) if len(head) >= 20 else 0
        arch_by_machine = {
            0x3E: "amd64",
            0xB7: "arm64",
        }
        expected = ""
        try:
            expected = _platform_arch()
        except Exception:
            pass
        probe["format"] = "elf"
        probe["elf_machine"] = arch_by_machine.get(machine, f"unknown-0x{machine:02x}")
        probe["expected_arch"] = expected or None
        probe["arch_match"] = bool(expected and probe["elf_machine"] == expected)
        return probe

    probe["format"] = "unknown"
    return probe


def _ollama_probe_problem(probe: dict[str, Any]) -> str:
    if probe.get("format") == "html":
        return "downloaded HTML/error page instead of a Linux Ollama binary"
    if probe.get("format") == "unknown":
        return "not a Linux ELF binary or shell launcher"
    if probe.get("format") == "elf" and probe.get("arch_match") is False:
        return (
            f"wrong CPU architecture: binary is {probe.get('elf_machine')}, "
            f"VM needs {probe.get('expected_arch')}"
        )
    return ""


def _ollama_validation_error(binary: str | Path, home_dir: str | Path | None = None) -> str:
    """Return an error string when an Ollama binary is unusable."""
    path = Path(binary)
    if not path.exists():
        return "not found"
    if not path.is_file():
        return "not a file"
    if not os.access(path, os.X_OK):
        return "not executable"
    probe_problem = _ollama_probe_problem(_binary_file_probe(path))
    if probe_problem:
        return probe_problem
    try:
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=8,
            env=_ollama_env(home_dir),
        )
    except OSError as exc:
        return str(exc)
    except subprocess.TimeoutExpired:
        return "version check timed out"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return detail or f"version check exited {result.returncode}"
    return ""


def _ollama_binary_usable(binary: str | Path, home_dir: str | Path | None = None) -> bool:
    return _ollama_validation_error(binary, home_dir=home_dir) == ""


def _ollama_env(home_dir: str | Path | None = None) -> dict[str, str]:
    layout = storage_layout(home_dir)
    env = os.environ.copy()
    env.update(layout.env())
    local_lib = layout.home / "lib" / "ollama"
    existing = env.get("LD_LIBRARY_PATH", "")
    if local_lib.exists() and str(local_lib) not in existing.split(":"):
        env["LD_LIBRARY_PATH"] = f"{local_lib}:{existing}" if existing else str(local_lib)
    env["PATH"] = f"{layout.bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


def _ollama_models(home_dir: str | Path | None = None) -> set[str]:
    ollama = _ollama_binary(home_dir)
    if not ollama:
        return set()
    try:
        result = subprocess.run(
            [ollama, "list"],
            capture_output=True,
            text=True,
            timeout=10,
            env=_ollama_env(home_dir),
        )
        if result.returncode != 0:
            return set()
    except Exception:
        return set()

    installed: set[str] = set()
    for line in result.stdout.splitlines()[1:]:
        name = line.split(maxsplit=1)[0].strip()
        if name:
            installed.add(name)
            installed.add(name.split(":")[0])
    return installed


def model_catalog_with_status(home_dir: str | Path | None = None) -> dict[str, Any]:
    vram_gb = _detect_vram_gb()
    try:
        installed = _ollama_models(home_dir)
    except TypeError:
        installed = _ollama_models()
    recommended = _recommended_model_ids(vram_gb)
    models: list[dict[str, Any]] = []

    for model in sorted(STUDIO_MODELS, key=lambda item: item.priority):
        installed_model = (
            model.install_target in installed
            or model.install_target.split(":")[0] in installed
        )
        data = asdict(model)
        data["recommended"] = model.id in recommended
        data["fits_vram"] = _fits_vram(model, vram_gb)
        data["installed"] = installed_model
        data["install_command"] = f"ollama pull {model.install_target}"
        models.append(data)

    try:
        ollama_available = bool(_ollama_binary(home_dir))
    except TypeError:
        ollama_available = bool(_ollama_binary())

    return {
        "models": models,
        "recommended_ids": [model["id"] for model in models if model["recommended"]],
        "installed_targets": sorted(installed),
        "detected_vram_gb": vram_gb,
        "ollama_available": ollama_available,
        "ollama_running": _ollama_reachable(),
        "count": len(models),
    }


def _find_model(model_id: str) -> StudioModel:
    for model in STUDIO_MODELS:
        if model.id == model_id or model.install_target == model_id:
            return model
    raise KeyError(f"Unknown studio model: {model_id}")


def _ollama_reachable() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", OLLAMA_PORT), timeout=0.25):
            return True
    except OSError:
        return False


def ollama_runtime_doctor(home_dir: str | Path | None = None) -> dict[str, Any]:
    """Return a rootless, non-mutating diagnosis of the local Ollama path.

    This intentionally does not download, start, or repair anything. It gives
    the WebUI one clear state to explain why local Ask AI is or is not ready.
    """
    layout = storage_layout(home_dir)
    local_candidate = layout.bin_dir / "ollama"
    binary = _ollama_binary(home_dir)
    binary_error = ""
    if not binary and local_candidate.exists():
        binary_error = _ollama_validation_error(local_candidate, home_dir=home_dir)
    binary_probe = _binary_file_probe(local_candidate)

    catalog = model_catalog_with_status(home_dir)
    installed_targets = list(catalog.get("installed_targets", []))
    recommended_models = [
        model for model in catalog.get("models", [])
        if model.get("recommended")
    ]
    missing_recommended = [
        model for model in recommended_models
        if not model.get("installed")
    ]
    running = bool(catalog.get("ollama_running"))

    if not binary:
        status = "missing-runtime"
        ready = False
        summary = "Local AI runtime is not installed yet."
        next_action = {
            "id": "rootless-ollama",
            "label": "Install runtime",
            "description": "Install the rootless Ollama runtime under NVH_HOME.",
        }
    elif not running:
        status = "server-offline"
        ready = False
        summary = "Local AI runtime is installed but the model server is not responding."
        next_action = {
            "id": "rootless-ollama",
            "label": "Repair runtime",
            "description": "Reinstall or restart the rootless Ollama runtime.",
        }
    elif missing_recommended:
        status = "missing-models"
        ready = False
        summary = (
            f"Local AI is running; {len(missing_recommended)} recommended model(s) "
            "still need to download."
        )
        next_action = {
            "id": "starter-models",
            "label": "Download models",
            "description": "Download GPU-fit starter models for AI Wizard and Ask AI.",
        }
    else:
        status = "ready"
        ready = True
        summary = f"Local AI is ready with {len(installed_targets)} installed model target(s)."
        next_action = None

    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "status": status,
        "ready": ready,
        "summary": summary,
        "binary": binary,
        "binary_valid": bool(binary),
        "binary_error": binary_error,
        "binary_probe": binary_probe,
        "local_candidate": str(local_candidate),
        "server_running": running,
        "installed_targets": installed_targets,
        "recommended_ids": catalog.get("recommended_ids", []),
        "missing_recommended_ids": [model.get("id", "") for model in missing_recommended],
        "missing_recommended_models": missing_recommended,
        "detected_vram_gb": catalog.get("detected_vram_gb", 0),
        "next_action": next_action,
        "rootless": True,
    }


def pack_status(pack: StudioPack, context: dict[str, Any] | None = None) -> dict[str, Any]:
    context = context or {}
    marker = _read_json(_marker_path(pack.id))
    installed = marker is not None
    details: dict[str, Any] = {}

    if pack.install_kind == "rootless_ollama":
        local_candidate = storage_layout().bin_dir / "ollama"
        probe = _binary_file_probe(local_candidate)
        local_looks_runnable = bool(
            probe.get("exists")
            and probe.get("is_file")
            and probe.get("executable")
            and probe.get("format") in {"elf", "script"}
            and probe.get("arch_match") is not False
        )
        system_binary = shutil.which("ollama") or ""
        binary = str(local_candidate) if local_looks_runnable else system_binary
        installed = bool(binary)
        details["binary"] = binary
        details["binary_probe"] = probe
        details["running"] = _ollama_reachable()
    elif pack.install_kind == "micromamba_runtime":
        from nvh.integrations.services.runtime import runtime_status

        runtime = runtime_status()
        installed = runtime.micromamba_installed
        details.update(runtime.as_dict())
    elif pack.install_kind == "ollama_models":
        installed_models = _ollama_models()
        missing = [
            model for model in pack.models
            if model not in installed_models and model.split(":")[0] not in installed_models
        ]
        installed = bool(pack.models) and not missing
        details["missing_models"] = missing
    elif pack.install_kind == "python_venv":
        installed = _venv_python(pack.id).exists() and marker is not None
        details["venv"] = str(_venv_python(pack.id).parent.parent)
    elif pack.install_kind == "ace_step_music":
        app_dir = _ace_step_app_dir()
        uv_binary = _ace_step_uv_binary()
        installed = app_dir.exists() and uv_binary.exists() and marker is not None
        details["app_dir"] = str(app_dir)
        details["uv"] = str(uv_binary)
        details["launcher"] = str(_local_bin() / "nvhive-ace-step")
        details["installable"] = platform.system().lower() == "linux" and shutil.which("git") is not None
        if platform.system().lower() != "linux":
            details["blocked_reason"] = "ACE-Step music pack targets Linux cloud desktops."
        elif not details["installable"]:
            details["blocked_reason"] = "ACE-Step needs git to clone the official repository into persistent storage."
    elif pack.install_kind == "openclaw_agent":
        node = context.get("node_status") or _node_runtime_status()
        binary = _openclaw_binary()
        installed = binary.exists() or marker is not None
        installable = bool(node["ready"] or node["can_auto_install"])
        details.update(node)
        details["binary"] = str(binary)
        details["workspace"] = str(_openclaw_workspace())
        details["installable"] = installable
        if not installable:
            details["blocked_reason"] = "OpenClaw needs Node.js 22.16+ and npm 10+, or a Linux host where nvHive can install Node rootlessly."
    elif pack.install_kind == "nemoclaw_sandbox":
        node = context.get("node_status") or _node_runtime_status()
        docker = context.get("docker_status") or _docker_status()
        binary = _nemoclaw_binary_from_env(_node_env())
        installed = bool(binary) or marker is not None
        installable = bool(docker["ready"] and (node["ready"] or node["can_auto_install"]))
        details.update({
            "node": node,
            "docker": docker,
            "binary": binary,
            "workspace": str(_nemoclaw_workspace()),
            "installable": installable,
            "alpha": True,
            "estimated_min_disk_gb": 20,
            "estimated_recommended_disk_gb": 40,
            "recommended_ram_gb": 16,
        })
        if not docker["ready"]:
            details["blocked_reason"] = "NemoClaw needs Docker/OpenShell access that works without sudo; use OpenClaw or ask the provider to enable rootless Docker."
        elif not installable:
            details["blocked_reason"] = "NemoClaw needs Node.js 22.16+ and npm 10+."
    elif pack.install_kind == "comfy_nodes":
        custom_nodes = _comfyui_app_dir() / "custom_nodes"
        missing_nodes = [node.name for node in pack.comfy_nodes if not (custom_nodes / node.name).exists()]
        installed = bool(pack.comfy_nodes) and not missing_nodes
        details["missing_nodes"] = missing_nodes
        details["custom_nodes_dir"] = str(custom_nodes)
    elif pack.install_kind == "scaffold":
        installed = marker is not None
        details["workspace"] = str(_pack_root(pack.id))
        if pack.id == "nvidia-omni-agent":
            vram_gb = _detect_vram_gb()
            layout = storage_layout()
            min_local_gb = float(_OMNI_MIN_FREE_GB)
            free_gb = None
            try:
                usage = shutil.disk_usage(layout.home)
                free_gb = round(usage.free / (1024**3), 1)
            except Exception:
                pass
            local_ok = bool(
                vram_gb >= pack.recommended_vram_gb
                and free_gb is not None
                and free_gb >= min_local_gb
            )
            details.update({
                "nim_recommended": True,
                "local_recommended": local_ok,
                "detected_vram_gb": vram_gb,
                "free_gb": free_gb,
                "min_local_free_gb": min_local_gb,
                "model_sizes_gb": _omni_model_sizes_gb(),
                "recommended_path": "local" if local_ok else "nvidia-nim",
            })
        if pack.id == "music-daw-helper":
            appimages = sorted((_pack_root(pack.id) / "appimages").glob("*.AppImage"))
            details["appimages"] = [str(path) for path in appimages]
            details["installable"] = platform.system().lower() == "linux"
            if not details["installable"]:
                details["blocked_reason"] = "Audacity and LMMS AppImage setup targets Linux cloud desktops."
    elif pack.install_kind == "blender_app":
        binary = _blender_binary()
        installed = binary.exists() and os.access(binary, os.X_OK)
        details["binary"] = str(binary)
        details["app_dir"] = str(_blender_app_dir())
        details["version"] = BLENDER_VERSION
    elif pack.install_kind == "godot_app":
        binary = _godot_binary_from_state()
        installed = binary is not None and marker is not None
        details["binary"] = str(binary) if binary else ""
        details["app_dir"] = str(_godot_root())
        details["release_api"] = GODOT_RELEASE_API

    return {
        "id": pack.id,
        "installed": installed,
        "root": str(_pack_root(pack.id)),
        "marker": str(_marker_path(pack.id)),
        "details": details,
        "installed_at": marker.get("installed_at") if marker else None,
    }


def catalog_with_status() -> dict[str, Any]:
    context = {
        "node_status": _node_runtime_status(),
        "docker_status": _docker_status(),
    }
    packs = []
    for pack in STUDIO_PACKS:
        data = asdict(pack)
        data["status"] = pack_status(pack, context=context)
        packs.append(data)
    return {
        "packs": packs,
        "bundles": bundles_as_dict(),
        "root": str(studio_root()),
        "count": len(packs),
    }


async def _run_command(
    cmd: list[str],
    *,
    label: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> AsyncIterator[dict[str, Any]]:
    yield {"event": "step", "status": "running", "message": label, "command": cmd}
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        yield {
            "event": "error",
            "status": "failed",
            "message": f"{label} could not start: {exc}",
        }
        raise RuntimeError(f"{label} could not start: {exc}") from exc
    try:
        assert process.stdout is not None
        async for raw_line in process.stdout:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            if line:
                yield {"event": "log", "status": "running", "message": line}
    except asyncio.CancelledError:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        raise
    try:
        return_code = await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        yield {"event": "error", "status": "failed", "message": f"{label} timed out"}
        raise RuntimeError(f"{label} timed out")
    if return_code != 0:
        yield {
            "event": "error",
            "status": "failed",
            "message": f"{label} failed with exit code {return_code}",
            "return_code": return_code,
        }
        raise RuntimeError(f"{label} failed")
    yield {"event": "step", "status": "complete", "message": f"{label} complete"}


def _write_marker(pack: StudioPack, extra: dict[str, Any] | None = None) -> None:
    root = _pack_root(pack.id)
    root.mkdir(parents=True, exist_ok=True)
    marker = {
        "id": pack.id,
        "title": pack.title,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "no_root": True,
    }
    if extra:
        marker.update(extra)
    _marker_path(pack.id).write_text(json.dumps(marker, indent=2), encoding="utf-8")
    try:
        from nvh.integrations.services.receipts import write_receipt

        launcher_paths = [str(_local_bin() / launcher) for launcher in pack.launchers]
        version = str(marker.get("version")) if marker.get("version") else None
        write_receipt(
            kind="studio-pack",
            item_id=pack.id,
            title=pack.title,
            install_path=root,
            version=version,
            source_urls=pack.source_urls,
            launchers=launcher_paths,
            models=pack.models,
            files=[str(_marker_path(pack.id))],
            metadata={
                "category": pack.category,
                "install_kind": pack.install_kind,
                "recommended_vram_gb": pack.recommended_vram_gb,
                "estimated_disk_gb": pack.estimated_disk_gb,
                "marker": marker,
            },
        )
    except Exception:
        pass


def _write_script(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _platform_arch() -> str:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "amd64"
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    raise RuntimeError(f"Unsupported Ollama Linux architecture: {platform.machine()}")


def _ollama_download_candidates(arch: str) -> list[tuple[str, str]]:
    """Return latest-compatible Ollama archive candidates.

    The official Linux installer probes the current ``.tar.zst`` package first
    and falls back to legacy ``.tgz`` packages for older pinned versions. nvHive
    follows the same policy, adds a direct GitHub release-asset fallback, and
    extracts into NVH_HOME instead of /usr.
    """
    custom_url = os.environ.get("NVH_OLLAMA_URL", "").strip()
    if custom_url:
        archive_type = "tar.zst" if custom_url.split("?", 1)[0].endswith(".tar.zst") else "tgz"
        return [(custom_url, archive_type)]

    version = os.environ.get("NVH_OLLAMA_VERSION", "").strip()
    version_param = f"?version={quote(version)}" if version else ""
    github_tag = version if version.startswith("v") else f"v{version}" if version else "latest"
    github_base = (
        f"https://github.com/ollama/ollama/releases/download/{github_tag}"
        if version
        else "https://github.com/ollama/ollama/releases/latest/download"
    )
    base = os.environ.get("NVH_OLLAMA_DOWNLOAD_BASE", "https://ollama.com/download").rstrip("/")
    name = f"ollama-linux-{arch}"
    return [
        (f"{base}/{name}.tar.zst{version_param}", "tar.zst"),
        (f"{github_base}/{name}.tar.zst", "tar.zst"),
        (f"{base}/{name}.tgz{version_param}", "tgz"),
        (f"{github_base}/{name}.tgz", "tgz"),
    ]


async def _download_ollama_archive(
    curl: str,
    arch: str,
    stage: Path,
) -> AsyncIterator[dict[str, Any]]:
    """Download the best Ollama archive without marking fallback attempts failed."""
    last_error = ""
    attempts: list[dict[str, Any]] = []
    for url, archive_type in _ollama_download_candidates(arch):
        archive = stage / f"ollama-linux-{arch}.{archive_type}"
        yield {
            "event": "step",
            "status": "running",
            "message": f"Downloading latest compatible Ollama Linux {arch} bundle",
            "url": url,
        }
        process = await asyncio.create_subprocess_exec(
            curl,
            "-fL",
            "--retry",
            "2",
            "--connect-timeout",
            "20",
            "--show-error",
            "-o",
            str(archive),
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output = ""
        assert process.stdout is not None
        async for raw_line in process.stdout:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            if line:
                output = line
                yield {"event": "log", "status": "running", "message": line}
        code = await process.wait()
        if code == 0 and archive.exists() and archive.stat().st_size > 0:
            yield {
                "event": "step",
                "status": "complete",
                "message": f"Ollama {archive_type} bundle downloaded",
                "url": url,
                "archive": str(archive),
                "archive_type": archive_type,
            }
            yield {
                "event": "archive",
                "status": "complete",
                "message": str(archive),
                "archive": str(archive),
                "archive_type": archive_type,
            }
            return
        last_error = output or f"curl exited {code}"
        attempts.append({
            "url": url,
            "archive_type": archive_type,
            "return_code": code,
            "error": last_error,
        })
        yield {
            "event": "log",
            "status": "running",
            "message": f"Ollama candidate unavailable ({archive_type}); trying fallback. Detail: {last_error}",
            "url": url,
            "return_code": code,
        }

    yield {
        "event": "error",
        "status": "failed",
        "message": f"Could not download Ollama Linux {arch} bundle. Last error: {last_error}",
        "attempts": attempts,
    }
    raise RuntimeError(f"Could not download Ollama Linux {arch} bundle. Last error: {last_error}")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_extract_member(tar: tarfile.TarFile, member: tarfile.TarInfo, target: Path) -> None:
    target_resolved = target.resolve()
    destination = (target / member.name).resolve()
    if not _is_relative_to(destination, target_resolved):
        raise RuntimeError(f"Unsafe Ollama archive member: {member.name}")
    if member.issym() or member.islnk():
        link_target = Path(member.linkname)
        linked = link_target if link_target.is_absolute() else destination.parent / link_target
        if not _is_relative_to(linked.resolve(), target_resolved):
            raise RuntimeError(f"Unsafe Ollama archive link: {member.name} -> {member.linkname}")
    try:
        tar.extract(member, target, filter="data")
    except TypeError:
        tar.extract(member, target)


def _extract_ollama_archive(archive: Path, archive_type: str, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    if archive_type == "tar.zst":
        try:
            import zstandard as zstd
        except Exception as exc:
            raise RuntimeError(
                "Ollama now publishes Linux bundles as .tar.zst; nvHive needs the "
                "Python zstandard package to extract them without sudo."
            ) from exc
        with archive.open("rb") as fh:
            reader = zstd.ZstdDecompressor().stream_reader(fh)
            try:
                with tarfile.open(fileobj=reader, mode="r|") as tar:
                    for member in tar:
                        _safe_extract_member(tar, member, target)
            finally:
                reader.close()
        return

    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            _safe_extract_member(tar, member, target)


async def _install_rootless_ollama(pack: StudioPack, force_update: bool) -> AsyncIterator[dict[str, Any]]:
    if _ollama_binary() and not force_update:
        yield {"event": "step", "status": "complete", "message": "Ollama already available"}
        _write_ollama_launcher()
        _write_marker(pack, {"binary": _ollama_binary()})
        return

    if os.name == "nt":
        yield {"event": "error", "status": "failed", "message": "Rootless Ollama pack is for Linux desktops."}
        return

    curl = shutil.which("curl")
    if not curl:
        yield {"event": "error", "status": "failed", "message": "curl is required for rootless Ollama."}
        return

    arch = _platform_arch()
    layout = storage_layout()
    local_binary = layout.bin_dir / "ollama"
    existing_error = _ollama_validation_error(local_binary) if local_binary.exists() else ""
    if existing_error:
        yield {
            "event": "step",
            "status": "running",
            "message": f"Replacing unusable Ollama binary: {existing_error}",
            "binary": str(local_binary),
        }
    target = layout.home
    target.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="ollama-", dir=str(studio_root())))
    archive: Path | None = None
    archive_type = ""

    async for event in _download_ollama_archive(curl, arch, stage):
        if event.get("event") == "archive":
            archive = Path(str(event["archive"]))
            archive_type = str(event["archive_type"])
            continue
        yield event

    if archive is None:
        yield {"event": "error", "status": "failed", "message": "Ollama archive download did not complete."}
        return

    yield {"event": "step", "status": "running", "message": f"Extracting Ollama into {target}"}
    try:
        if (target / "lib" / "ollama").exists():
            shutil.rmtree(target / "lib" / "ollama", ignore_errors=True)
        _extract_ollama_archive(archive, archive_type, target)
    except Exception as exc:
        yield {
            "event": "error",
            "status": "failed",
            "message": f"Ollama archive extraction failed: {exc}",
            "archive": str(archive),
        }
        return
    yield {"event": "step", "status": "complete", "message": f"Extract Ollama into {target} complete"}

    verified = _ollama_binary()
    if not verified:
        error = _ollama_validation_error(local_binary) if local_binary.exists() else "binary missing after extract"
        yield {
            "event": "error",
            "status": "failed",
            "message": f"Ollama install did not produce a runnable Linux {arch} binary: {error}",
            "binary": str(local_binary),
        }
        return

    _write_ollama_launcher()
    _write_marker(pack, {"binary": verified})
    yield {
        "event": "step",
        "status": "complete",
        "message": "Rootless Ollama installed. Use nvhive-ollama-serve to start it.",
        "binary": verified,
    }


def _write_ollama_launcher() -> Path:
    script = _local_bin() / "nvhive-ollama-serve"
    layout = storage_layout()
    content = f"""#!/usr/bin/env bash
set -euo pipefail

export NVH_HOME="${{NVH_HOME:-{layout.home}}}"
export NVH_BIN="${{NVH_BIN:-{layout.bin_dir}}}"
export PATH="$NVH_BIN:$PATH"
export LD_LIBRARY_PATH="{layout.home}/lib/ollama:${{LD_LIBRARY_PATH:-}}"
export OLLAMA_MODELS="${{OLLAMA_MODELS:-{layout.ollama_models_dir}}}"
mkdir -p "$OLLAMA_MODELS"
exec ollama serve
"""
    _write_script(script, content)
    return script


def _start_ollama_background() -> None:
    if _ollama_reachable():
        return
    ollama = _ollama_binary()
    if not ollama:
        return
    log = studio_root() / "ollama.log"
    pid_file = studio_root() / "ollama.pid"
    studio_root().mkdir(parents=True, exist_ok=True)
    out = log.open("ab")
    process = subprocess.Popen(
        [ollama, "serve"],
        stdout=out,
        stderr=subprocess.STDOUT,
        env=_ollama_env(),
        start_new_session=True,
    )
    pid_file.write_text(str(process.pid), encoding="utf-8")


async def _wait_for_ollama(seconds: float = 8.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _ollama_reachable():
            return True
        await asyncio.sleep(0.3)
    return _ollama_reachable()


async def _install_ollama_models(pack: StudioPack, force_update: bool) -> AsyncIterator[dict[str, Any]]:
    if not _ollama_binary():
        rootless = _find_pack("rootless-ollama")
        async for event in _install_rootless_ollama(rootless, force_update=False):
            yield event

    if not _ollama_binary():
        yield {"event": "error", "status": "failed", "message": "Ollama is still unavailable; cannot pull models."}
        return

    _start_ollama_background()
    if not await _wait_for_ollama():
        yield {
            "event": "error",
            "status": "failed",
            "message": "Ollama did not start. Try nvhive-ollama-serve in a terminal, then rerun this pack.",
        }
        return

    installed = _ollama_models()
    for model in pack.models:
        if not force_update and (model in installed or model.split(":")[0] in installed):
            yield {"event": "step", "status": "complete", "message": f"{model} already pulled"}
            continue
        async for event in _run_command(
            [_ollama_binary(), "pull", model],
            label=f"Pull {model}",
            env=_ollama_env(),
        ):
            yield event
    _write_marker(pack, {"models": pack.models})


def _python_lab_readme(pack: StudioPack) -> str:
    packages = "\n".join(f"- {package}" for package in pack.python_packages)
    return f"""# {pack.title}

{pack.description}

This environment is installed without root access at:

`{_pack_root(pack.id)}`

Packages:

{packages}

Activate it:

```bash
source {_pack_root(pack.id) / "venv" / "bin" / "activate"}
```
"""


def _write_agent_launcher(pack: StudioPack) -> None:
    script = _local_bin() / "nvhive-agent-lab"
    root = _pack_root(pack.id)
    content = f"""#!/usr/bin/env bash
set -euo pipefail

source "{root}/venv/bin/activate"
cd "{root}"
echo "NVHive Agent Lab"
echo "Try: jupyter lab --no-browser --ip 127.0.0.1 --port 8890"
python - <<'PY'
print("Agent packages are ready. Build with LangGraph, CrewAI, AutoGen, or nvHive tools.")
PY
"""
    _write_script(script, content)


def _write_game_lab(pack: StudioPack) -> None:
    root = _pack_root(pack.id)
    samples = root / "samples"
    samples.mkdir(parents=True, exist_ok=True)
    demo = samples / "pygame_demo.py"
    demo.write_text(
        """import pygame

pygame.init()
screen = pygame.display.set_mode((960, 540))
clock = pygame.time.Clock()
x = 80
running = True
while running:
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False
    x = (x + 3) % 960
    screen.fill((12, 12, 12))
    pygame.draw.rect(screen, (118, 185, 0), pygame.Rect(x, 230, 80, 80))
    pygame.display.flip()
    clock.tick(60)
pygame.quit()
""",
        encoding="utf-8",
    )
    launcher = _local_bin() / "nvhive-game-lab"
    content = f"""#!/usr/bin/env bash
set -euo pipefail

source "{root}/venv/bin/activate"
cd "{root}"
python "{demo}"
"""
    _write_script(launcher, content)


def _write_music_lab(pack: StudioPack) -> None:
    root = _pack_root(pack.id)
    for folder in ["inputs", "outputs", "stems", "transcripts", "notebooks"]:
        (root / folder).mkdir(parents=True, exist_ok=True)

    sample = root / "notebooks" / "README.md"
    sample.write_text(
        """# Music Producer AI Lab

Drop source audio in `inputs/`, then use the launcher to start JupyterLab.

Useful first experiments:

- Split stems with Demucs
- Transcribe lyrics or vocals with WhisperX
- Generate short audio textures with Stable Audio tools
- Batch process files into `outputs/`

Check licenses before publishing generated or transformed audio.
""",
        encoding="utf-8",
    )
    launcher = _local_bin() / "nvhive-music-lab"
    content = f"""#!/usr/bin/env bash
set -euo pipefail

source "{root}/venv/bin/activate"
cd "{root}"
if [ "$#" -gt 0 ]; then
  exec "$@"
fi
echo "NVHive Music Producer AI Lab"
echo "Inputs:  {root}/inputs"
echo "Outputs: {root}/outputs"
exec jupyter lab --no-browser --ip 127.0.0.1 --port 8891
"""
    _write_script(launcher, content)


async def _install_python_venv(pack: StudioPack, force_update: bool) -> AsyncIterator[dict[str, Any]]:
    root = _pack_root(pack.id)
    venv_python = _venv_python(pack.id)
    root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(storage_layout().env())
    env["PYTHONUTF8"] = "1"

    if force_update and venv_python.exists():
        yield {"event": "step", "status": "running", "message": "Updating existing Python environment"}
    elif not venv_python.exists():
        async for event in _run_command(
            [sys.executable, "-m", "venv", str(root / "venv")],
            env=env,
            label=f"Create {pack.title} virtual environment",
        ):
            yield event

    async for event in _run_command(
        [str(venv_python), "-m", "pip", "install", "--upgrade", "pip", "wheel", "setuptools"],
        env=env,
        label="Upgrade Python packaging tools",
    ):
        yield event
    async for event in _run_command(
        [str(venv_python), "-m", "pip", "install", *pack.python_packages],
        env=env,
        label=f"Install {pack.title} packages",
    ):
        yield event

    (root / "README.md").write_text(_python_lab_readme(pack), encoding="utf-8")
    if pack.id == "agent-lab":
        _write_agent_launcher(pack)
    if pack.id == "game-dev-lab":
        _write_game_lab(pack)
    if pack.id == "music-producer-lab":
        _write_music_lab(pack)
    _write_marker(pack, {"packages": pack.python_packages, "venv": str(root / "venv")})


def _write_openclaw_launcher() -> Path:
    root = _pack_root("openclaw-agent")
    workspace = _openclaw_workspace()
    launcher = _local_bin() / "nvhive-openclaw"
    content = f"""#!/usr/bin/env bash
set -euo pipefail

export NVH_HOME="${{NVH_HOME:-{storage_layout().home}}}"
export OPENCLAW_HOME="${{OPENCLAW_HOME:-{workspace}}}"
export NPM_CONFIG_PREFIX="${{NPM_CONFIG_PREFIX:-{_openclaw_prefix()}}}"
export PATH="{_openclaw_prefix()}/bin:{_local_bin()}:$PATH"
mkdir -p "$OPENCLAW_HOME" "{root}/logs"
cd "$OPENCLAW_HOME"
if [ "$#" -eq 0 ]; then
  exec openclaw onboard --install-daemon
fi
exec openclaw "$@"
"""
    _write_script(launcher, content)
    return launcher


def _write_openclaw_readme() -> None:
    root = _pack_root("openclaw-agent")
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(
        f"""# OpenClaw Agent Workspace

OpenClaw is installed in this rootless nvHive pack:

`{root}`

Launch the guided OpenClaw onboarding:

```bash
nvhive-openclaw
```

Advanced overrides:

```bash
nvhive-openclaw --help
nvhive-openclaw tui
```

The wizard keeps OpenClaw state in `{_openclaw_workspace()}` and can route to
local Ollama models or configured cloud model providers.
""",
        encoding="utf-8",
    )


async def _install_openclaw_agent(pack: StudioPack, force_update: bool) -> AsyncIterator[dict[str, Any]]:
    if os.name == "nt":
        yield {"event": "error", "status": "failed", "message": "OpenClaw rootless pack currently targets Linux/WSL sessions."}
        return

    root = _pack_root(pack.id)
    root.mkdir(parents=True, exist_ok=True)
    _openclaw_workspace().mkdir(parents=True, exist_ok=True)

    if _openclaw_binary().exists() and not force_update:
        launcher = _write_openclaw_launcher()
        _write_openclaw_readme()
        _write_marker(pack, {"binary": str(_openclaw_binary()), "launcher": str(launcher), "workspace": str(_openclaw_workspace())})
        yield {"event": "step", "status": "complete", "message": "OpenClaw already installed"}
        return

    yield {"event": "step", "status": "running", "message": "Checking Node.js 22.16+ and npm 10+ for OpenClaw"}
    try:
        env, node_status = await asyncio.to_thread(_prepare_node_runtime)
    except Exception as exc:
        yield {"event": "error", "status": "failed", "message": str(exc)}
        return
    npm = shutil.which("npm", path=env.get("PATH"))
    if not npm:
        yield {"event": "error", "status": "failed", "message": "npm is unavailable after Node runtime setup."}
        return

    _openclaw_prefix().mkdir(parents=True, exist_ok=True)
    async for event in _run_command(
        [npm, "install", "--prefix", str(_openclaw_prefix()), OPENCLAW_PACKAGE],
        label="Install OpenClaw package",
        env=env,
    ):
        yield event

    launcher = _write_openclaw_launcher()
    _write_openclaw_readme()
    _write_marker(pack, {
        "binary": str(_openclaw_binary()),
        "launcher": str(launcher),
        "workspace": str(_openclaw_workspace()),
        "node": node_status,
    })
    yield {
        "event": "complete",
        "status": "complete",
        "message": "OpenClaw installed. Launch nvhive-openclaw to onboard the agent.",
        "launcher": str(launcher),
    }


def _write_nemoclaw_launcher() -> Path:
    root = _pack_root("nemoclaw-sandbox")
    workspace = _nemoclaw_workspace()
    launcher = _local_bin() / "nvhive-nemoclaw"
    content = f"""#!/usr/bin/env bash
set -euo pipefail

export NVH_HOME="${{NVH_HOME:-{storage_layout().home}}}"
export NEMOCLAW_WORKSPACE="${{NEMOCLAW_WORKSPACE:-{workspace}}}"
export NPM_CONFIG_PREFIX="${{NPM_CONFIG_PREFIX:-{_nemoclaw_prefix()}}}"
export PATH="{_nemoclaw_prefix()}/bin:{_local_bin()}:$PATH"
mkdir -p "$NEMOCLAW_WORKSPACE" "{root}/logs"
cd "$NEMOCLAW_WORKSPACE"
if [ "$#" -eq 0 ]; then
  exec nemoclaw onboard
fi
exec nemoclaw "$@"
"""
    _write_script(launcher, content)
    return launcher


def _write_nemoclaw_readme(docker: dict[str, Any]) -> None:
    root = _pack_root("nemoclaw-sandbox")
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(
        f"""# NVIDIA NemoClaw Sandbox

NemoClaw is the guarded OpenClaw path. It uses NVIDIA OpenShell and requires a
Docker runtime that works without sudo in this Linux session.

Docker check:

`{docker.get("detail", "not checked")}`

Launch onboarding:

```bash
nvhive-nemoclaw
```

Advanced overrides:

```bash
nvhive-nemoclaw --help
nvhive-nemoclaw <sandbox-name> status
```

Keep the sandbox workspace on the persistent mount:

`{_nemoclaw_workspace()}`
""",
        encoding="utf-8",
    )


async def _install_nemoclaw_sandbox(pack: StudioPack, force_update: bool) -> AsyncIterator[dict[str, Any]]:
    if os.name == "nt":
        yield {"event": "error", "status": "failed", "message": "NemoClaw requires a Linux, macOS, or WSL2 container runtime; nvHive only enables this pack on Linux sessions."}
        return
    if platform.system() != "Linux":
        yield {"event": "error", "status": "failed", "message": "This nvHive pack targets Linux cloud desktops."}
        return

    docker = _docker_status()
    if not docker["ready"]:
        yield {
            "event": "error",
            "status": "failed",
            "message": f"NemoClaw is blocked: {docker['detail']} {docker['rootless_hint']}",
            "details": docker,
        }
        return

    root = _pack_root(pack.id)
    root.mkdir(parents=True, exist_ok=True)
    _nemoclaw_workspace().mkdir(parents=True, exist_ok=True)

    current_env = _node_env({"NPM_CONFIG_PREFIX": str(_nemoclaw_prefix())})
    existing_binary = _nemoclaw_binary_from_env(current_env)
    if existing_binary and not force_update:
        launcher = _write_nemoclaw_launcher()
        _write_nemoclaw_readme(docker)
        _write_marker(pack, {"binary": existing_binary, "launcher": str(launcher), "workspace": str(_nemoclaw_workspace()), "docker": docker})
        yield {"event": "step", "status": "complete", "message": "NemoClaw CLI already installed"}
        return

    yield {"event": "step", "status": "running", "message": "Checking Node.js 22.16+ and npm 10+ for NemoClaw"}
    try:
        env, node_status = await asyncio.to_thread(_prepare_node_runtime)
    except Exception as exc:
        yield {"event": "error", "status": "failed", "message": str(exc)}
        return
    env = _node_env({"NPM_CONFIG_PREFIX": str(_nemoclaw_prefix())})
    npm = shutil.which("npm", path=env.get("PATH"))
    if not npm:
        yield {"event": "error", "status": "failed", "message": "npm is unavailable after Node runtime setup."}
        return

    _nemoclaw_prefix().mkdir(parents=True, exist_ok=True)
    async for event in _run_command(
        [npm, "install", "--prefix", str(_nemoclaw_prefix()), NEMOCLAW_PACKAGE],
        label="Install NemoClaw CLI",
        env=env,
    ):
        yield event

    binary = _nemoclaw_binary_from_env(env)
    launcher = _write_nemoclaw_launcher()
    _write_nemoclaw_readme(docker)
    _write_marker(pack, {
        "binary": binary,
        "launcher": str(launcher),
        "workspace": str(_nemoclaw_workspace()),
        "node": node_status,
        "docker": docker,
        "onboard_next": "nvhive-nemoclaw",
    })
    yield {
        "event": "complete",
        "status": "complete",
        "message": "NemoClaw CLI installed. Launch nvhive-nemoclaw to create the OpenShell sandbox.",
        "launcher": str(launcher),
    }


def _write_ace_step_launcher() -> Path:
    root = _ace_step_root()
    app_dir = _ace_step_app_dir()
    uv_binary = _ace_step_uv_binary()
    launcher = _local_bin() / "nvhive-ace-step"
    content = f"""#!/usr/bin/env bash
set -euo pipefail

export NVH_HOME="${{NVH_HOME:-{storage_layout().home}}}"
export HF_HOME="${{HF_HOME:-{storage_layout().models_dir / "huggingface"}}}"
export TRANSFORMERS_CACHE="${{TRANSFORMERS_CACHE:-$HF_HOME/transformers}}"
export XDG_CACHE_HOME="${{XDG_CACHE_HOME:-{storage_layout().cache_dir}}}"
cd "{app_dir}"
if [ "$#" -gt 0 ]; then
  exec "{uv_binary}" run "$@"
fi
exec "{uv_binary}" run acestep --server-name 127.0.0.1 --port 7865
"""
    _write_script(launcher, content)
    return launcher


def _write_ace_step_readme() -> None:
    root = _ace_step_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(
        f"""# ACE-Step Music Generator

ACE-Step 1.5 is installed under persistent nvHive storage:

`{_ace_step_app_dir()}`

Launch the local music studio:

```bash
nvhive-ace-step
```

Then open http://127.0.0.1:7865.

Models download on first launch and are kept on the persistent mount through
`HF_HOME`. For lower-VRAM GPUs, use ACE-Step's built-in lighter model/offload
options in the UI.
""",
        encoding="utf-8",
    )


async def _install_ace_step_music(pack: StudioPack, force_update: bool) -> AsyncIterator[dict[str, Any]]:
    if platform.system().lower() != "linux":
        yield {"event": "error", "status": "failed", "message": "ACE-Step music pack targets Linux cloud desktops."}
        return
    git = shutil.which("git")
    if not git:
        yield {"event": "error", "status": "failed", "message": "Git is required to install ACE-Step."}
        return

    root = _ace_step_root()
    root.mkdir(parents=True, exist_ok=True)
    app_dir = _ace_step_app_dir()
    env = os.environ.copy()
    env.update(storage_layout().env())
    env["PYTHONUTF8"] = "1"
    env.setdefault("HF_HOME", str(storage_layout().models_dir / "huggingface"))
    env.setdefault("XDG_CACHE_HOME", str(storage_layout().cache_dir))

    if not app_dir.exists():
        async for event in _run_command(
            [git, "clone", "--depth", "1", ACE_STEP_REPO_URL, str(app_dir)],
            env=env,
            label="Clone ACE-Step 1.5",
        ):
            yield event
    elif force_update:
        async for event in _run_command(
            [git, "-C", str(app_dir), "pull", "--ff-only"],
            env=env,
            label="Update ACE-Step 1.5",
        ):
            yield event
    else:
        yield {"event": "step", "status": "complete", "message": "ACE-Step repository already present"}

    uv_python = _ace_step_uv_venv_python()
    if not uv_python.exists():
        async for event in _run_command(
            [sys.executable, "-m", "venv", str(root / "uv-venv")],
            env=env,
            label="Create ACE-Step uv environment",
        ):
            yield event

    async for event in _run_command(
        [str(uv_python), "-m", "pip", "install", "--upgrade", "pip", "wheel", "setuptools", "uv"],
        env=env,
        label="Install rootless uv for ACE-Step",
    ):
        yield event

    uv_binary = _ace_step_uv_binary()
    async for event in _run_command(
        [str(uv_binary), "sync"],
        cwd=app_dir,
        env=env,
        label="Install ACE-Step dependencies",
        timeout=1800.0,
    ):
        yield event

    launcher = _write_ace_step_launcher()
    _write_ace_step_readme()
    _write_marker(pack, {
        "repo": ACE_STEP_REPO_URL,
        "app_dir": str(app_dir),
        "uv": str(uv_binary),
        "launcher": str(launcher),
        "models_home": env["HF_HOME"],
    })
    yield {
        "event": "complete",
        "status": "complete",
        "message": "ACE-Step music generator installed. Launch nvhive-ace-step.",
        "launcher": str(launcher),
    }


async def _install_comfy_nodes(pack: StudioPack, force_update: bool) -> AsyncIterator[dict[str, Any]]:
    app_dir = _comfyui_app_dir()
    venv_python = _comfyui_venv_python()
    custom_nodes = app_dir / "custom_nodes"
    env = os.environ.copy()
    env.update(storage_layout().env())
    env["PYTHONUTF8"] = "1"

    if not app_dir.exists() or not venv_python.exists():
        yield {
            "event": "skip",
            "status": "skipped",
            "message": (
                "ComfyUI is not installed yet, so custom nodes were skipped. "
                "Install ComfyUI from the setup wizard or run "
                "nvh workstation --with-comfyui -y, then rerun nvh studio --install comfy -y."
            ),
        }
        return
    if shutil.which("git") is None:
        yield {"event": "error", "status": "failed", "message": "Git is required to install ComfyUI custom nodes."}
        return

    custom_nodes.mkdir(parents=True, exist_ok=True)
    for node in pack.comfy_nodes:
        target = custom_nodes / node.name
        if target.exists():
            if force_update:
                async for event in _run_command(
                    ["git", "-C", str(target), "pull", "--ff-only"],
                    label=f"Update {node.name}",
                ):
                    yield event
            else:
                yield {"event": "step", "status": "complete", "message": f"{node.name} already installed"}
        else:
            async for event in _run_command(
                ["git", "clone", "--depth", "1", node.repo_url, str(target)],
                label=f"Install {node.name}",
            ):
                yield event

        requirements = target / "requirements.txt"
        if requirements.exists():
            async for event in _run_command(
                [str(venv_python), "-m", "pip", "install", "-r", str(requirements)],
                cwd=target,
                env=env,
                label=f"Install {node.name} requirements",
            ):
                yield event

    _write_marker(pack, {
        "comfyui_root": str(_comfyui_root()),
        "custom_nodes": [asdict(node) for node in pack.comfy_nodes],
    })


def _write_mod_helper(pack: StudioPack) -> None:
    root = _pack_root(pack.id)
    for folder in ["mods", "workshop-notes", "textures", "exports", "wine-prefixes"]:
        (root / folder).mkdir(parents=True, exist_ok=True)
    readme = root / "README.md"
    readme.write_text(
        """# NVHive Game Mod Helper

This workspace is rootless. It gives students a clean place for:

- Steam Workshop notes
- Wine/Proton prefix notes
- texture and sprite exports from ComfyUI
- generated JSON, glTF, PNG, and audio assets

Do not use this to bypass anti-cheat, DRM, school policy, or a game's license.
""",
        encoding="utf-8",
    )
    launcher = _local_bin() / "nvhive-mod-lab"
    content = f"""#!/usr/bin/env bash
set -euo pipefail

cd "{root}"
echo "NVHive mod workspace: {root}"
find . -maxdepth 2 -type d | sort
"""
    _write_script(launcher, content)


def _write_github_login_helper(pack: StudioPack) -> None:
    root = _pack_root(pack.id)
    for folder in ["repos", "tokens", "notes"]:
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(
        f"""# {pack.title}

This helper keeps GitHub setup rootless and persistent.

Preferred path:

1. Run `nvhive-github-login`.
2. If GitHub CLI is available, use browser login.
3. If GitHub CLI is not available, add a fine-grained token as `GITHUB_TOKEN`
   in your nvHive environment file and relaunch the WebUI.

Public repositories can clone over HTTPS without login. Private repositories,
pull requests, and Unreal Engine source access need authenticated GitHub.
""",
        encoding="utf-8",
    )
    launcher = _local_bin() / "nvhive-github-login"
    token_hint = storage_layout().config_dir / "env"
    content = f"""#!/usr/bin/env bash
set -euo pipefail

echo "nvHive GitHub Connect"
echo "Workspace: {root}"

if [ -n "${{GITHUB_TOKEN:-}}" ]; then
  echo "GITHUB_TOKEN is present in this shell. GitHub API and private HTTPS clones can use it."
fi

if command -v gh >/dev/null 2>&1; then
  if gh auth status >/dev/null 2>&1; then
    echo "GitHub CLI is already authenticated."
    gh auth status
    exit 0
  fi
  echo "Starting GitHub browser login with GitHub CLI..."
  gh auth login --web --git-protocol https
  gh auth setup-git || true
  gh auth status
  exit 0
fi

cat <<'EOF'
GitHub CLI is not installed on this image.

Rootless fallback:
1. Create a fine-grained GitHub token in the browser.
2. Add this line to the nvHive env file:
   export GITHUB_TOKEN=your_token_here
3. Relaunch nvHive.

Env file:
EOF
echo "{token_hint}"
"""
    _write_script(launcher, content)


def _write_game_engine_helper(pack: StudioPack) -> None:
    root = _pack_root(pack.id)
    for folder in ["projects", "downloads", "notes", "assets"]:
        (root / folder).mkdir(parents=True, exist_ok=True)

    if pack.id == "unity-hub-helper":
        body = """# Unity Hub Helper

Unity requires a Unity account and license acceptance. nvHive prepares the
persistent storage layout, then students can keep Unity editors and projects on
the block volume instead of the read-only OS disk.

Suggested paths:

- Projects: `projects/`
- Downloads: `downloads/`
- Shared AI assets: `assets/`

Open https://unity.com/download if the provider image does not already include
Unity Hub.
"""
        launcher_name = "nvhive-unity-hub"
        launcher_message = "Unity Hub requires account sign-in. Use this workspace for downloads and projects."
    else:
        body = """# Unreal Engine Helper

Unreal Engine is large and account-gated. nvHive prepares persistent storage,
GitHub/Epic notes, and asset folders so the setup can survive cloud session
rebuilds.

Checklist:

1. Connect GitHub with `nvhive-github-login`.
2. Link Epic and GitHub accounts for Unreal source access.
3. Keep source trees, derived data cache, and projects on the block volume.

Unreal source/editor installs can exceed 150 GB.
"""
        launcher_name = "nvhive-unreal-helper"
        launcher_message = "Unreal setup needs Epic/GitHub access and plenty of persistent storage."

    (root / "README.md").write_text(body, encoding="utf-8")
    launcher = _local_bin() / launcher_name
    content = f"""#!/usr/bin/env bash
set -euo pipefail

cd "{root}"
echo "{launcher_message}"
echo "Workspace: {root}"
find . -maxdepth 2 -type d | sort
"""
    _write_script(launcher, content)


def _write_music_daw_helper(pack: StudioPack) -> None:
    root = _pack_root(pack.id)
    for folder in ["projects", "appimages", "plugins", "samples", "exports", "notes"]:
        (root / folder).mkdir(parents=True, exist_ok=True)
    readme = root / "README.md"
    readme.write_text(
        f"""# {pack.title}

This is the persistent desktop-audio workspace for nvHive.

nvHive attempts to download these rootless apps during install:

- Audacity AppImage for waveform editing, vocal cleanup, and quick exports
- LMMS AppImage for beat making and MIDI sketches

Manual optional additions:

- REAPER Linux tarball for a compact professional DAW trial path
- MuseScore AppImage for notation and sheet music

Downloaded or manually added apps live in `appimages/`. The launcher lists them
and can run one by name without writing to the base OS. If FUSE is unavailable
on a locked-down VM, the launcher uses AppImage extract-and-run.

AI tools live beside this pack:

- `nvhive-ace-step` for full local AI music generation
- `nvhive-music-lab` for stems, transcription, and batch processing

Always check model and sample licenses before publishing music.
""",
        encoding="utf-8",
    )
    launcher = _local_bin() / "nvhive-music-studio"
    content = f"""#!/usr/bin/env bash
set -euo pipefail

cd "{root}"
mkdir -p appimages projects plugins samples exports notes
if [ "$#" -eq 0 ]; then
  echo "NVHive Music Producer Studio"
  echo "Workspace: {root}"
  echo
  echo "Audacity and LMMS AppImages are downloaded during install when official assets are available."
  echo "You can also drop REAPER or MuseScore AppImages/tarballs into appimages/."
  echo "Run: nvhive-music-studio <partial-name>"
  echo
  find appimages -maxdepth 1 -type f | sort || true
  exit 0
fi

target="$(find appimages -maxdepth 1 -type f -iname "*$1*" | head -n 1)"
if [ -z "$target" ]; then
  echo "No matching AppImage/tarball found in {root}/appimages"
  exit 1
fi
chmod +x "$target" || true
if [[ "$target" == *.AppImage ]]; then
  export APPIMAGE_EXTRACT_AND_RUN=1
fi
exec "$target"
"""
    _write_script(launcher, content)


def _write_omni_agent_helper(pack: StudioPack) -> None:
    root = _pack_root(pack.id)
    root.mkdir(parents=True, exist_ok=True)
    size_lines = "\n".join(
        f"- `ollama pull {pick.tag}` ({pick.quant}): {pick.weights_gb:g} GB on disk, "
        f"~{pick.runtime_gb:g} GB loaded"
        for pick in _OMNI_PICKS
    )
    readme = f"""# {pack.title}

{pack.description}

## Default Path

Use NVIDIA NIM / build.nvidia.com first. This keeps AI Starter fast, rootless,
and usable on smaller student VMs while still exposing the new multimodal
Nemotron 3 Nano Omni workflow.

## Local Path

Only try a local download when AI Wizard reports enough persistent storage and
GPU headroom ({pack.recommended_vram_gb} GB of model budget, {_OMNI_MIN_FREE_GB} GB
free). The Ollama registry carries these quants (sizes as `ollama list` prints them):

{size_lines}

The local path should be treated as an advanced option for large NVIDIA GPUs or
cloud instances with ample block storage.

## Use Cases

- Document intelligence and OCR
- Screenshot / GUI reasoning
- Audio-video reasoning
- Multimodal agent perception before OpenClaw or NemoClaw actions

## Sources

- {NVIDIA_OMNI_BLOG_URL}
- {NVIDIA_OMNI_TECH_BLOG_URL}
- {NVIDIA_OMNI_HF_URL}
- {NVIDIA_BUILD_URL}
"""
    (root / "README.md").write_text(readme, encoding="utf-8")
    plan = {
        "name": "nvidia-omni-agent",
        "default_path": "nvidia-nim",
        "local_guardrails": {
            "min_free_gb": _OMNI_MIN_FREE_GB,
            "recommended_vram_gb": pack.recommended_vram_gb,
            "model_sizes_gb": _omni_model_sizes_gb(),
        },
        "models": pack.models,
        "sources": pack.source_urls,
    }
    (root / "omni-agent-plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    launcher = _local_bin() / "nvhive-omni-agent"
    content = f"""#!/usr/bin/env bash
set -euo pipefail

ROOT="{root}"
echo "NVHive NVIDIA Omni Agent"
echo "Workspace: $ROOT"
echo
echo "Default: use NVIDIA NIM / build.nvidia.com for Nemotron 3 Nano Omni."
echo "Local weights are advanced and gated by AI Wizard storage + GPU checks."
echo
echo "Open: $ROOT/README.md"
"""
    _write_script(launcher, content)


def _write_scaffold_pack(pack: StudioPack) -> None:
    if pack.id == "game-mod-helper":
        _write_mod_helper(pack)
    elif pack.id == "github-login-helper":
        _write_github_login_helper(pack)
    elif pack.id in {"unity-hub-helper", "unreal-engine-helper"}:
        _write_game_engine_helper(pack)
    elif pack.id == "music-daw-helper":
        _write_music_daw_helper(pack)
    elif pack.id == "nvidia-omni-agent":
        _write_omni_agent_helper(pack)
    else:
        root = _pack_root(pack.id)
        root.mkdir(parents=True, exist_ok=True)
        (root / "README.md").write_text(
            f"# {pack.title}\n\n{pack.description}\n",
            encoding="utf-8",
        )


async def _download_appimage_asset(
    client: Any,
    *,
    api_url: str,
    app_name: str,
    downloads: Path,
    force_update: bool,
    required_tokens: tuple[str, ...] = (),
    preferred_tokens: tuple[str, ...] = (),
) -> tuple[Path, dict[str, Any]]:
    release_response = await client.get(api_url)
    release_response.raise_for_status()
    release = release_response.json()
    asset = _select_appimage_asset(
        release,
        app_name=app_name,
        required_tokens=required_tokens,
        preferred_tokens=preferred_tokens,
    )
    asset_name = str(asset["name"])
    asset_url = str(asset["browser_download_url"])
    release_tag = str(release.get("tag_name") or "latest")
    target = downloads / asset_name
    if target.exists() and not force_update:
        target.chmod(target.stat().st_mode | stat.S_IXUSR)
        return target, {"asset": asset_name, "version": release_tag, "url": asset_url, "cached": True}

    async with client.stream("GET", asset_url) as response:
        response.raise_for_status()
        with target.open("wb") as handle:
            async for chunk in response.aiter_bytes():
                if chunk:
                    handle.write(chunk)
    target.chmod(target.stat().st_mode | stat.S_IXUSR)
    return target, {"asset": asset_name, "version": release_tag, "url": asset_url, "cached": False}


async def _install_music_daw_helper(pack: StudioPack, force_update: bool) -> AsyncIterator[dict[str, Any]]:
    if platform.system().lower() != "linux":
        yield {"event": "error", "status": "failed", "message": "Music DAW AppImage setup targets Linux cloud desktops."}
        return

    _write_music_daw_helper(pack)
    root = _pack_root(pack.id)
    downloads = root / "appimages"
    downloads.mkdir(parents=True, exist_ok=True)
    downloaded: list[dict[str, Any]] = []
    download_errors: list[str] = []

    try:
        import httpx
    except Exception as exc:
        yield {"event": "error", "status": "failed", "message": f"Could not import httpx for AppImage downloads: {exc}"}
        return

    async with httpx.AsyncClient(follow_redirects=True, timeout=600) as client:
        for app_name, api_url, required, preferred in [
            ("Audacity", AUDACITY_RELEASE_API, ("linux",), ("22.04", "x64")),
            ("LMMS", LMMS_RELEASE_API, tuple(), ("linux", "x86_64", "x64")),
        ]:
            try:
                yield {"event": "step", "status": "running", "message": f"Checking latest official {app_name} AppImage"}
                target, metadata = await _download_appimage_asset(
                    client,
                    api_url=api_url,
                    app_name=app_name,
                    downloads=downloads,
                    force_update=force_update,
                    required_tokens=required,
                    preferred_tokens=preferred,
                )
                downloaded.append({"name": app_name, "path": str(target), **metadata})
                verb = "Using cached" if metadata.get("cached") else "Downloaded"
                yield {"event": "step", "status": "complete", "message": f"{verb} {app_name} AppImage", "path": str(target)}
            except Exception as exc:
                message = f"{app_name} AppImage could not be auto-downloaded: {exc}"
                download_errors.append(message)
                yield {"event": "warning", "status": "warning", "message": message}

    if not downloaded:
        yield {"event": "error", "status": "failed", "message": "No Audacity or LMMS AppImages were downloaded; retry later or use manual AppImage overrides."}
        return

    _write_marker(pack, {"workspace": str(root), "appimages": downloaded, "download_errors": download_errors, "force_update": force_update})
    yield {"event": "step", "status": "complete", "message": f"{pack.title} workspace ready"}


async def _install_scaffold(pack: StudioPack, force_update: bool) -> AsyncIterator[dict[str, Any]]:
    if pack.id == "music-daw-helper":
        async for event in _install_music_daw_helper(pack, force_update):
            yield event
        return
    _write_scaffold_pack(pack)
    _write_marker(pack, {"workspace": str(_pack_root(pack.id)), "force_update": force_update})
    yield {"event": "step", "status": "complete", "message": f"{pack.title} workspace ready"}


def _safe_extract_tar(archive: Path, target: Path) -> None:
    """Extract a tar archive while refusing path traversal entries."""
    target.mkdir(parents=True, exist_ok=True)
    target_resolved = target.resolve()
    with tarfile.open(archive) as tar:
        members = []
        for member in tar.getmembers():
            destination = (target / member.name).resolve()
            if not str(destination).startswith(str(target_resolved)):
                raise RuntimeError(f"Archive member escapes target directory: {member.name}")
            members.append(member)
        tar.extractall(target, members=members)


def _safe_extract_zip(archive: Path, target: Path) -> None:
    """Extract a zip archive while refusing path traversal entries."""
    target.mkdir(parents=True, exist_ok=True)
    target_resolved = target.resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            destination = (target / member.filename).resolve()
            try:
                destination.relative_to(target_resolved)
            except ValueError as exc:
                raise RuntimeError(f"Archive member escapes target directory: {member.filename}") from exc
        zf.extractall(target)


def _select_godot_asset(release: dict[str, Any]) -> dict[str, Any]:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise RuntimeError("Godot release metadata did not include assets")

    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        lower_name = name.lower()
        url = str(asset.get("browser_download_url") or "")
        if (
            url
            and lower_name.endswith(".zip")
            and "linux" in lower_name
            and "x86_64" in lower_name
            and "mono" not in lower_name
            and "server" not in lower_name
            and "template" not in lower_name
        ):
            return asset
    raise RuntimeError("No official Godot Linux x86_64 zip asset was found in the latest release")


def _select_appimage_asset(
    release: dict[str, Any],
    *,
    app_name: str,
    required_tokens: tuple[str, ...] = (),
    preferred_tokens: tuple[str, ...] = (),
) -> dict[str, Any]:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise RuntimeError(f"{app_name} release metadata did not include assets")

    candidates: list[tuple[int, dict[str, Any]]] = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        lower_name = name.lower()
        url = str(asset.get("browser_download_url") or "")
        if not url or not lower_name.endswith(".appimage"):
            continue
        if any(token not in lower_name for token in required_tokens):
            continue
        if any(token in lower_name for token in ("aarch64", "arm64", "armv7")):
            continue
        score = sum(10 for token in preferred_tokens if token in lower_name)
        score += sum(1 for token in ("x64", "x86_64", "amd64", "linux") if token in lower_name)
        candidates.append((score, asset))

    if not candidates:
        raise RuntimeError(f"No Linux x64 AppImage asset was found in the latest {app_name} release")
    return sorted(candidates, key=lambda item: item[0], reverse=True)[0][1]


def _find_godot_binary(root: Path) -> Path | None:
    candidates: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        if name.startswith("godot") and "linux" in name and "x86_64" in name and not name.endswith(".zip"):
            candidates.append(path)
    if candidates:
        return sorted(candidates, key=lambda item: len(str(item)))[0]
    return None


def _write_godot_launcher(binary: Path) -> Path:
    layout = storage_layout()
    root = _godot_root()
    projects = root / "projects"
    settings = layout.config_dir / "godot"
    projects.mkdir(parents=True, exist_ok=True)
    settings.mkdir(parents=True, exist_ok=True)
    launcher = _local_bin() / "nvhive-godot"
    content = f"""#!/usr/bin/env bash
set -euo pipefail

export GODOT_EDITOR_SETTINGS_DIR="${{GODOT_EDITOR_SETTINGS_DIR:-{settings}}}"
mkdir -p "$GODOT_EDITOR_SETTINGS_DIR" "{projects}"
cd "{projects}"
exec "{binary}" "$@"
"""
    _write_script(launcher, content)
    return launcher


def _write_blender_launcher() -> Path:
    layout = storage_layout()
    binary = _blender_binary()
    projects = _blender_root() / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    launcher = _local_bin() / "nvhive-blender"
    content = f"""#!/usr/bin/env bash
set -euo pipefail

export NVH_HOME="${{NVH_HOME:-{layout.home}}}"
export BLENDER_USER_CONFIG="${{BLENDER_USER_CONFIG:-{layout.config_dir}/blender/{BLENDER_VERSION}}}"
export BLENDER_USER_SCRIPTS="${{BLENDER_USER_SCRIPTS:-{_blender_root()}/scripts}}"
export BLENDER_USER_DATAFILES="${{BLENDER_USER_DATAFILES:-{_blender_root()}/datafiles}}"
mkdir -p "$BLENDER_USER_CONFIG" "$BLENDER_USER_SCRIPTS" "$BLENDER_USER_DATAFILES" "{projects}"
cd "{projects}"
exec "{binary}" "$@"
"""
    _write_script(launcher, content)
    return launcher


def _write_model_receipt(model: StudioModel) -> None:
    try:
        from nvh.integrations.services.receipts import write_receipt

        layout = storage_layout()
        write_receipt(
            kind="studio-model",
            item_id=model.id,
            title=model.title,
            install_path=layout.ollama_models_dir,
            source_urls=[model.source_url],
            models=[model.install_target],
            metadata={
                "provider": model.provider,
                "install_target": model.install_target,
                "category": model.category,
                "recommended_vram_gb": model.recommended_vram_gb,
                "estimated_disk_gb": model.estimated_disk_gb,
                "capabilities": model.capabilities,
                "license_note": model.license_note,
            },
        )
    except Exception:
        pass


async def _install_godot_app(pack: StudioPack, force_update: bool) -> AsyncIterator[dict[str, Any]]:
    if platform.system().lower() != "linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        yield {
            "event": "error",
            "status": "failed",
            "message": "The Godot rootless pack currently supports Linux x86_64 desktops.",
        }
        return

    root = _godot_root()
    downloads = root / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)

    existing = _godot_binary_from_state()
    if existing and not force_update:
        launcher = _write_godot_launcher(existing)
        _write_marker(pack, {"binary": str(existing), "launcher": str(launcher), "force_update": force_update})
        yield {"event": "step", "status": "complete", "message": "Godot already installed"}
        return

    yield {"event": "step", "status": "running", "message": "Checking latest official Godot release"}
    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=600) as client:
        release_response = await client.get(GODOT_RELEASE_API)
        release_response.raise_for_status()
        release = release_response.json()
        asset = _select_godot_asset(release)
        asset_name = str(asset["name"])
        asset_url = str(asset["browser_download_url"])
        release_tag = str(release.get("tag_name") or "latest")
        safe_tag = re.sub(r"[^A-Za-z0-9._-]+", "-", release_tag).strip("-") or "latest"
        app_dir = root / safe_tag

        if app_dir.exists() and not force_update:
            binary = _find_godot_binary(app_dir)
            if binary:
                binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
                launcher = _write_godot_launcher(binary)
                _godot_current_file().write_text(
                    json.dumps({"version": release_tag, "binary": str(binary), "app_dir": str(app_dir)}, indent=2),
                    encoding="utf-8",
                )
                _write_marker(pack, {"binary": str(binary), "launcher": str(launcher), "version": release_tag})
                yield {"event": "step", "status": "complete", "message": f"Godot {release_tag} already installed"}
                return

        if app_dir.exists():
            shutil.rmtree(app_dir)

        archive = downloads / asset_name
        yield {"event": "step", "status": "running", "message": f"Downloading Godot {release_tag}", "url": asset_url}
        async with client.stream("GET", asset_url) as response:
            response.raise_for_status()
            with archive.open("wb") as fh:
                async for chunk in response.aiter_bytes():
                    if chunk:
                        fh.write(chunk)

    stage = Path(tempfile.mkdtemp(prefix="godot-", dir=str(root)))
    try:
        yield {"event": "step", "status": "running", "message": "Extracting Godot archive"}
        _safe_extract_zip(archive, stage)
        binary = _find_godot_binary(stage)
        if not binary:
            raise RuntimeError("Godot archive did not contain the expected Linux executable")
        app_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stage), str(app_dir))
        final_binary = app_dir / binary.relative_to(stage)
        final_binary.chmod(final_binary.stat().st_mode | stat.S_IXUSR)
        launcher = _write_godot_launcher(final_binary)
    except Exception as exc:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        yield {"event": "error", "status": "failed", "message": f"Godot install failed: {exc}"}
        return

    (root / "README.md").write_text(
        f"""# Godot Engine

Godot {release_tag} is installed without root access at:

`{final_binary}`

Launch it with:

```bash
nvhive-godot
```

Projects live in `{root / "projects"}` so game prototypes, Blender exports, and
ComfyUI textures stay on persistent storage.
""",
        encoding="utf-8",
    )
    _godot_current_file().write_text(
        json.dumps(
            {
                "version": release_tag,
                "binary": str(final_binary),
                "app_dir": str(app_dir),
                "asset": asset_name,
                "source_url": asset_url,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    _write_marker(
        pack,
        {
            "binary": str(final_binary),
            "launcher": str(launcher),
            "version": release_tag,
            "asset": asset_name,
        },
    )
    yield {"event": "step", "status": "complete", "message": f"Godot {release_tag} installed"}


async def _install_blender_app(pack: StudioPack, force_update: bool) -> AsyncIterator[dict[str, Any]]:
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        yield {
            "event": "error",
            "status": "failed",
            "message": "The Blender rootless pack currently supports Linux x64 desktops.",
        }
        return

    root = _blender_root()
    app_dir = _blender_app_dir()
    binary = _blender_binary()
    if binary.exists() and not force_update:
        launcher = _write_blender_launcher()
        _write_marker(pack, {"binary": str(binary), "launcher": str(launcher)})
        yield {"event": "step", "status": "complete", "message": "Blender already installed"}
        return

    download_dir = storage_layout().cache_dir / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    archive = download_dir / f"blender-{BLENDER_VERSION}-linux-x64.tar.xz"
    root.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="blender-", dir=str(root.parent)))

    yield {
        "event": "plan",
        "status": "running",
        "message": f"Installing Blender {BLENDER_VERSION} LTS into NVH_HOME",
        "url": BLENDER_LINUX_X64_URL,
        "target": str(app_dir),
    }

    try:
        import httpx

        async with httpx.AsyncClient(follow_redirects=True, timeout=600) as client:
            async with client.stream("GET", BLENDER_LINUX_X64_URL) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length", "0") or "0")
                downloaded = 0
                last_emit = time.monotonic()
                with archive.open("wb") as handle:
                    async for chunk in response.aiter_bytes(chunk_size=1024 * 512):
                        handle.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        if total and now - last_emit > 0.75:
                            yield {
                                "event": "download",
                                "status": "running",
                                "message": f"Downloaded {downloaded / 1024 / 1024:.1f} MB",
                                "progress": min(85, int(downloaded / total * 85)),
                            }
                            last_emit = now

        yield {"event": "step", "status": "running", "message": "Extracting Blender archive"}
        await asyncio.to_thread(_safe_extract_tar, archive, stage)
        extracted = stage / app_dir.name
        if not extracted.is_dir():
            raise RuntimeError("Blender archive did not contain the expected application folder")
        if app_dir.exists():
            shutil.rmtree(app_dir)
        root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(extracted), str(app_dir))
        binary.chmod(0o755)
        launcher = _write_blender_launcher()
    except Exception as exc:
        yield {"event": "error", "status": "failed", "message": f"Blender install failed: {exc}"}
        return
    finally:
        archive.unlink(missing_ok=True)
        shutil.rmtree(stage, ignore_errors=True)

    readme = root / "README.md"
    readme.write_text(
        f"""# Blender Creative Studio

Blender {BLENDER_VERSION} LTS is installed without root access at:

`{app_dir}`

Launch it:

```bash
nvhive-blender
```

Project files are stored in `{root / "projects"}` so students can pair Blender
with ComfyUI textures, game-dev assets, and nvHive prompts.
""",
        encoding="utf-8",
    )
    _write_marker(pack, {"binary": str(binary), "launcher": str(launcher), "version": BLENDER_VERSION})
    yield {
        "event": "complete",
        "status": "complete",
        "message": "Blender Creative Studio installed",
        "binary": str(binary),
        "launcher": str(launcher),
    }


async def install_studio_models(
    model_ids: list[str] | tuple[str, ...],
    *,
    force_update: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """Install selected Ollama models from the model picker."""
    try:
        models = [_find_model(model_id) for model_id in model_ids]
    except KeyError as exc:
        yield {"event": "error", "status": "failed", "message": str(exc)}
        return

    if not models:
        yield {"event": "error", "status": "failed", "message": "No models selected."}
        return

    yield {
        "event": "plan",
        "status": "running",
        "message": f"Installing {len(models)} selected local model(s)",
        "model_ids": [model.id for model in models],
        "estimated_disk_gb": round(sum(model.estimated_disk_gb for model in models), 1),
        "status_snapshot": model_catalog_with_status(),
    }

    if not _ollama_binary():
        rootless = _find_pack("rootless-ollama")
        async for event in _install_rootless_ollama(rootless, force_update=False):
            yield {**event, "model_ids": [model.id for model in models]}

    if not _ollama_binary():
        yield {
            "event": "error",
            "status": "failed",
            "message": "Ollama is unavailable; install the Rootless Ollama pack first.",
            "status_snapshot": model_catalog_with_status(),
        }
        return

    _start_ollama_background()
    if not await _wait_for_ollama():
        yield {
            "event": "error",
            "status": "failed",
            "message": "Ollama did not start. Try nvhive-ollama-serve in a terminal, then retry.",
            "status_snapshot": model_catalog_with_status(),
        }
        return

    installed = _ollama_models()
    for model in models:
        if not force_update and (
            model.install_target in installed
            or model.install_target.split(":")[0] in installed
        ):
            _write_model_receipt(model)
            yield {
                "event": "model",
                "status": "complete",
                "message": f"{model.install_target} already installed",
                "model_id": model.id,
            }
            continue
        async for event in _run_command(
            [_ollama_binary(), "pull", model.install_target],
            label=f"Pull {model.title}",
            env=_ollama_env(),
        ):
            yield {**event, "model_id": model.id}
        _write_model_receipt(model)

    yield {
        "event": "complete",
        "status": "complete",
        "message": "Selected local models installed",
        "status_snapshot": model_catalog_with_status(),
    }


async def install_studio_packs(
    pack_ids: list[str] | tuple[str, ...],
    *,
    force_update: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """Install studio packs and stream progress events."""
    try:
        resolved = expand_pack_ids(list(pack_ids))
    except KeyError as exc:
        yield {"event": "error", "status": "failed", "message": str(exc)}
        return

    if not resolved:
        yield {"event": "error", "status": "failed", "message": "No studio packs selected."}
        return

    packs = [_find_pack(pack_id) for pack_id in resolved]
    yield {
        "event": "plan",
        "status": "running",
        "message": f"Installing {len(packs)} rootless AI Studio pack(s)",
        "pack_ids": resolved,
        "estimated_disk_gb": round(sum(pack.estimated_disk_gb for pack in packs), 1),
    }

    studio_root().mkdir(parents=True, exist_ok=True)
    for pack in packs:
        yield {
            "event": "pack",
            "status": "running",
            "message": f"Installing {pack.title}",
            "pack_id": pack.id,
        }
        try:
            if pack.install_kind == "rootless_ollama":
                async for event in _install_rootless_ollama(pack, force_update):
                    yield {**event, "pack_id": pack.id}
            elif pack.install_kind == "micromamba_runtime":
                from nvh.integrations.services.runtime import install_micromamba

                async for event in install_micromamba(force_update=force_update):
                    yield {**event, "pack_id": pack.id}
            elif pack.install_kind == "ollama_models":
                async for event in _install_ollama_models(pack, force_update):
                    yield {**event, "pack_id": pack.id}
            elif pack.install_kind == "python_venv":
                async for event in _install_python_venv(pack, force_update):
                    yield {**event, "pack_id": pack.id}
            elif pack.install_kind == "ace_step_music":
                async for event in _install_ace_step_music(pack, force_update):
                    yield {**event, "pack_id": pack.id}
            elif pack.install_kind == "openclaw_agent":
                async for event in _install_openclaw_agent(pack, force_update):
                    yield {**event, "pack_id": pack.id}
            elif pack.install_kind == "nemoclaw_sandbox":
                async for event in _install_nemoclaw_sandbox(pack, force_update):
                    yield {**event, "pack_id": pack.id}
            elif pack.install_kind == "comfy_nodes":
                async for event in _install_comfy_nodes(pack, force_update):
                    yield {**event, "pack_id": pack.id}
            elif pack.install_kind == "scaffold":
                async for event in _install_scaffold(pack, force_update):
                    yield {**event, "pack_id": pack.id}
            elif pack.install_kind == "godot_app":
                async for event in _install_godot_app(pack, force_update):
                    yield {**event, "pack_id": pack.id}
            elif pack.install_kind == "blender_app":
                async for event in _install_blender_app(pack, force_update):
                    yield {**event, "pack_id": pack.id}
            else:
                raise RuntimeError(f"Unsupported pack type: {pack.install_kind}")
        except Exception as exc:
            yield {
                "event": "error",
                "status": "failed",
                "message": str(exc),
                "pack_id": pack.id,
                "status_snapshot": catalog_with_status(),
            }
            return

    yield {
        "event": "complete",
        "status": "complete",
        "message": "AI Studio pack setup finished",
        "status_snapshot": catalog_with_status(),
    }
