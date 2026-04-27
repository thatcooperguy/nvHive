"""Rootless AI Studio pack catalog and installers.

Packs are intentionally user-space only: files go under ``~/.nvh`` and
launchers go under ``~/.local/bin``. The installer never calls sudo, apt,
dnf, pacman, systemctl, or Docker.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

OLLAMA_PORT = 11434


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


STUDIO_MODELS: list[StudioModel] = [
    StudioModel(
        id="gemma3-4b",
        title="Gemma 3 4B",
        provider="ollama",
        install_target="gemma3:4b",
        category="chat",
        recommended_vram_gb=6,
        estimated_disk_gb=3.3,
        priority=10,
        capabilities=["chat", "vision-capable family", "fast"],
        why_recommended="Best first local model for small student GPUs.",
        source_url="https://ollama.com/library/gemma3",
        license_note="Ollama library terms apply.",
    ),
    StudioModel(
        id="qwen3-8b",
        title="Qwen 3 8B",
        provider="ollama",
        install_target="qwen3:8b",
        category="chat",
        recommended_vram_gb=8,
        estimated_disk_gb=5.2,
        priority=20,
        capabilities=["chat", "reasoning", "multilingual"],
        why_recommended="Strong general-purpose reasoning model for 8 GB+ GPUs.",
        source_url="https://ollama.com/library/qwen3",
        license_note="Ollama library terms apply.",
    ),
    StudioModel(
        id="llama31-8b",
        title="Llama 3.1 8B",
        provider="ollama",
        install_target="llama3.1:8b",
        category="chat",
        recommended_vram_gb=8,
        estimated_disk_gb=4.9,
        priority=30,
        capabilities=["chat", "long context", "general"],
        why_recommended="Reliable baseline model for comparing answers in class.",
        source_url="https://ollama.com/library/llama3.1",
        license_note="Meta Llama license and Ollama library terms apply.",
    ),
    StudioModel(
        id="qwen25-coder-7b",
        title="Qwen 2.5 Coder 7B",
        provider="ollama",
        install_target="qwen2.5-coder:7b",
        category="code",
        recommended_vram_gb=8,
        estimated_disk_gb=4.7,
        priority=40,
        capabilities=["code", "debugging", "homework helper"],
        why_recommended="Good local coding tutor without sending code to a cloud API.",
        source_url="https://ollama.com/library/qwen2.5-coder",
        license_note="Ollama library terms apply.",
    ),
    StudioModel(
        id="deepseek-r1-8b",
        title="DeepSeek R1 8B",
        provider="ollama",
        install_target="deepseek-r1:8b",
        category="reasoning",
        recommended_vram_gb=10,
        estimated_disk_gb=5.2,
        priority=50,
        capabilities=["reasoning", "math", "step-by-step"],
        why_recommended="Useful when students want slower, more deliberate reasoning.",
        source_url="https://ollama.com/library/deepseek-r1",
        license_note="Ollama library terms apply.",
    ),
    StudioModel(
        id="nomic-embed-text",
        title="Nomic Embed Text",
        provider="ollama",
        install_target="nomic-embed-text",
        category="embedding",
        recommended_vram_gb=0,
        estimated_disk_gb=0.3,
        priority=60,
        capabilities=["embeddings", "search", "RAG"],
        why_recommended="Small embedding model for local search and document experiments.",
        source_url="https://ollama.com/library/nomic-embed-text",
        license_note="Ollama library terms apply.",
    ),
    StudioModel(
        id="llava-7b",
        title="LLaVA 7B",
        provider="ollama",
        install_target="llava:7b",
        category="vision",
        recommended_vram_gb=8,
        estimated_disk_gb=4.5,
        priority=70,
        capabilities=["vision", "image Q&A", "desktop screenshots"],
        why_recommended="Adds local image understanding for screenshots and classroom media.",
        source_url="https://ollama.com/library/llava",
        license_note="Ollama library terms apply.",
    ),
]


STUDIO_PACKS: list[StudioPack] = [
    StudioPack(
        id="rootless-ollama",
        title="Rootless Ollama Runtime",
        category="runtime",
        tagline="Local model server without sudo",
        description=(
            "Installs the Ollama Linux bundle into ~/.local, writes a user launcher, "
            "and stores models under ~/.nvh/models/ollama."
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
            "~/.local/bin must be on PATH for the plain ollama command.",
            "If a system Ollama already exists, nvHive uses it instead of replacing it.",
        ],
    ),
    StudioPack(
        id="llm-starter",
        title="Top Local LLM Starter",
        category="llm",
        tagline="Chat, vision, coding, and embeddings",
        description=(
            "Pulls compact, broadly useful Ollama models for student work: Gemma 3, "
            "Qwen 3, Llama 3.1, and Nomic embeddings."
        ),
        recommended_vram_gb=8,
        estimated_disk_gb=18.0,
        install_kind="ollama_models",
        no_root=True,
        models=["gemma3:4b", "qwen3:8b", "llama3.1:8b", "nomic-embed-text"],
        python_packages=[],
        comfy_nodes=[],
        launchers=[],
        source_urls=[
            "https://ollama.com/library",
            "https://ollama.com/library/gemma3",
            "https://ollama.com/library/qwen3",
        ],
        notes=[
            "Good default pack for 8 GB and larger NVIDIA GPUs.",
            "Model pulls can be several GB and may take a while on school Wi-Fi.",
        ],
    ),
    StudioPack(
        id="llm-coder-reasoner",
        title="Coder and Reasoner Models",
        category="llm",
        tagline="Code help, math, and slower thinking",
        description=(
            "Adds Qwen coder and DeepSeek reasoning models for programming, math, "
            "debugging, and agent planning."
        ),
        recommended_vram_gb=12,
        estimated_disk_gb=12.0,
        install_kind="ollama_models",
        no_root=True,
        models=["qwen2.5-coder:7b", "deepseek-r1:8b"],
        python_packages=[],
        comfy_nodes=[],
        launchers=[],
        source_urls=["https://ollama.com/library", "https://ollama.com/library/qwen3"],
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
            "search helpers, and classroom automation experiments."
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
            "prototypes, texture generation workflows, and classroom game projects."
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
]


PACK_BUNDLES: dict[str, list[str]] = {
    "starter": ["rootless-ollama", "llm-starter", "agent-lab", "comfyui-power-nodes", "game-dev-lab"],
    "llms": ["rootless-ollama", "llm-starter", "llm-coder-reasoner"],
    "agents": ["agent-lab"],
    "comfy": ["comfyui-power-nodes"],
    "game": ["game-dev-lab", "game-mod-helper"],
    "all": [
        "rootless-ollama",
        "llm-starter",
        "llm-coder-reasoner",
        "agent-lab",
        "comfyui-power-nodes",
        "game-dev-lab",
        "game-mod-helper",
    ],
}


def studio_root() -> Path:
    configured = os.environ.get("NVH_STUDIO_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".nvh" / "studio"


def _local_bin() -> Path:
    path = Path.home() / ".local" / "bin"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pack_root(pack_id: str) -> Path:
    return studio_root() / "packs" / pack_id


def _comfyui_root() -> Path:
    configured = os.environ.get("COMFYUI_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".nvh" / "comfyui"


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


def bundles_as_dict() -> dict[str, list[str]]:
    return {key: list(value) for key, value in PACK_BUNDLES.items()}


def _detect_vram_gb() -> int:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return 0
    try:
        result = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return 0
        values = [
            int(line.strip())
            for line in result.stdout.splitlines()
            if line.strip().isdigit()
        ]
        if not values:
            return 0
        return max(values) // 1024
    except Exception:
        return 0


def _fits_vram(model: StudioModel, vram_gb: int) -> bool:
    return model.recommended_vram_gb == 0 or (
        vram_gb > 0 and model.recommended_vram_gb <= vram_gb
    )


def _recommended_model_ids(vram_gb: int) -> set[str]:
    recommended: set[str] = {"nomic-embed-text"}
    if vram_gb >= 6:
        recommended.add("gemma3-4b")
    if vram_gb >= 8:
        recommended.update({"qwen3-8b", "llama31-8b", "qwen25-coder-7b", "llava-7b"})
    if vram_gb >= 10:
        recommended.add("deepseek-r1-8b")
    if vram_gb == 0:
        recommended.add("gemma3-4b")
    return recommended


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _ollama_binary() -> str:
    found = shutil.which("ollama")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "ollama"
    return str(local) if local.exists() else ""


def _ollama_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("OLLAMA_MODELS", str(Path.home() / ".nvh" / "models" / "ollama"))
    local_lib = Path.home() / ".local" / "lib" / "ollama"
    existing = env.get("LD_LIBRARY_PATH", "")
    if local_lib.exists() and str(local_lib) not in existing.split(":"):
        env["LD_LIBRARY_PATH"] = f"{local_lib}:{existing}" if existing else str(local_lib)
    env["PATH"] = f"{Path.home() / '.local' / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    return env


def _ollama_models() -> set[str]:
    ollama = _ollama_binary()
    if not ollama:
        return set()
    try:
        result = subprocess.run(
            [ollama, "list"],
            capture_output=True,
            text=True,
            timeout=10,
            env=_ollama_env(),
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


def model_catalog_with_status() -> dict[str, Any]:
    vram_gb = _detect_vram_gb()
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

    return {
        "models": models,
        "recommended_ids": [model["id"] for model in models if model["recommended"]],
        "installed_targets": sorted(installed),
        "detected_vram_gb": vram_gb,
        "ollama_available": bool(_ollama_binary()),
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
        with socket.create_connection(("127.0.0.1", OLLAMA_PORT), timeout=1.0):
            return True
    except OSError:
        return False


def pack_status(pack: StudioPack) -> dict[str, Any]:
    marker = _read_json(_marker_path(pack.id))
    installed = marker is not None
    details: dict[str, Any] = {}

    if pack.install_kind == "rootless_ollama":
        installed = bool(_ollama_binary())
        details["binary"] = _ollama_binary()
        details["running"] = _ollama_reachable()
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
    elif pack.install_kind == "comfy_nodes":
        custom_nodes = _comfyui_app_dir() / "custom_nodes"
        missing_nodes = [node.name for node in pack.comfy_nodes if not (custom_nodes / node.name).exists()]
        installed = bool(pack.comfy_nodes) and not missing_nodes
        details["missing_nodes"] = missing_nodes
        details["custom_nodes_dir"] = str(custom_nodes)
    elif pack.install_kind == "scaffold":
        installed = marker is not None
        details["workspace"] = str(_pack_root(pack.id))

    return {
        "id": pack.id,
        "installed": installed,
        "root": str(_pack_root(pack.id)),
        "marker": str(_marker_path(pack.id)),
        "details": details,
        "installed_at": marker.get("installed_at") if marker else None,
    }


def catalog_with_status() -> dict[str, Any]:
    packs = []
    for pack in STUDIO_PACKS:
        data = asdict(pack)
        data["status"] = pack_status(pack)
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
) -> AsyncIterator[dict[str, Any]]:
    yield {"event": "step", "status": "running", "message": label, "command": cmd}
    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert process.stdout is not None
    async for raw_line in process.stdout:
        line = raw_line.decode("utf-8", errors="replace").rstrip()
        if line:
            yield {"event": "log", "status": "running", "message": line}
    return_code = await process.wait()
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
    tar = shutil.which("tar")
    if not curl or not tar:
        yield {"event": "error", "status": "failed", "message": "curl and tar are required for rootless Ollama."}
        return

    arch = _platform_arch()
    url = f"https://ollama.com/download/ollama-linux-{arch}.tgz"
    target = Path.home() / ".local"
    target.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="ollama-", dir=str(studio_root())))
    archive = stage / f"ollama-linux-{arch}.tgz"

    async for event in _run_command(
        [curl, "-fL", url, "-o", str(archive)],
        label=f"Download Ollama Linux {arch} bundle",
    ):
        yield event
    async for event in _run_command(
        [tar, "-xzf", str(archive), "-C", str(target)],
        label="Extract Ollama into ~/.local",
    ):
        yield event

    _write_ollama_launcher()
    _write_marker(pack, {"binary": _ollama_binary()})
    yield {
        "event": "step",
        "status": "complete",
        "message": "Rootless Ollama installed. Use nvhive-ollama-serve to start it.",
    }


def _write_ollama_launcher() -> Path:
    script = _local_bin() / "nvhive-ollama-serve"
    content = """#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
export LD_LIBRARY_PATH="$HOME/.local/lib/ollama:${LD_LIBRARY_PATH:-}"
export OLLAMA_MODELS="${OLLAMA_MODELS:-$HOME/.nvh/models/ollama}"
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


async def _install_python_venv(pack: StudioPack, force_update: bool) -> AsyncIterator[dict[str, Any]]:
    root = _pack_root(pack.id)
    venv_python = _venv_python(pack.id)
    root.mkdir(parents=True, exist_ok=True)

    if force_update and venv_python.exists():
        yield {"event": "step", "status": "running", "message": "Updating existing Python environment"}
    elif not venv_python.exists():
        async for event in _run_command(
            [sys.executable, "-m", "venv", str(root / "venv")],
            label=f"Create {pack.title} virtual environment",
        ):
            yield event

    async for event in _run_command(
        [str(venv_python), "-m", "pip", "install", "--upgrade", "pip", "wheel", "setuptools"],
        label="Upgrade Python packaging tools",
    ):
        yield event
    async for event in _run_command(
        [str(venv_python), "-m", "pip", "install", *pack.python_packages],
        label=f"Install {pack.title} packages",
    ):
        yield event

    (root / "README.md").write_text(_python_lab_readme(pack), encoding="utf-8")
    if pack.id == "agent-lab":
        _write_agent_launcher(pack)
    if pack.id == "game-dev-lab":
        _write_game_lab(pack)
    _write_marker(pack, {"packages": pack.python_packages, "venv": str(root / "venv")})


async def _install_comfy_nodes(pack: StudioPack, force_update: bool) -> AsyncIterator[dict[str, Any]]:
    app_dir = _comfyui_app_dir()
    venv_python = _comfyui_venv_python()
    custom_nodes = app_dir / "custom_nodes"

    if not app_dir.exists() or not venv_python.exists():
        yield {
            "event": "error",
            "status": "failed",
            "message": "ComfyUI is not installed yet. Run nvh workstation --with-comfyui first.",
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


async def _install_scaffold(pack: StudioPack, force_update: bool) -> AsyncIterator[dict[str, Any]]:
    _write_mod_helper(pack)
    _write_marker(pack, {"workspace": str(_pack_root(pack.id)), "force_update": force_update})
    yield {"event": "step", "status": "complete", "message": f"{pack.title} workspace ready"}


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
            elif pack.install_kind == "ollama_models":
                async for event in _install_ollama_models(pack, force_update):
                    yield {**event, "pack_id": pack.id}
            elif pack.install_kind == "python_venv":
                async for event in _install_python_venv(pack, force_update):
                    yield {**event, "pack_id": pack.id}
            elif pack.install_kind == "comfy_nodes":
                async for event in _install_comfy_nodes(pack, force_update):
                    yield {**event, "pack_id": pack.id}
            elif pack.install_kind == "scaffold":
                async for event in _install_scaffold(pack, force_update):
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
        "message": "AI Studio packs installed",
        "status_snapshot": catalog_with_status(),
    }
