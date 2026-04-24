"""Student workstation helpers for Linux GPU desktop sessions."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WorkstationProfile:
    """Detected workstation state for setup guidance."""

    platform: str
    has_gui: bool
    has_gpu: bool
    gpu_name: str
    vram_gb: int
    python: str
    nvh: str
    ollama: str
    recommended_chat_models: list[str]
    recommended_comfy_profiles: list[str]
    notes: list[str]


def _first_existing_command(*names: str) -> str:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return ""


def _detect_gpu() -> tuple[bool, str, int]:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return False, "", 0

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
        if result.returncode != 0 or not result.stdout.strip():
            return True, "", 0
        first = result.stdout.strip().splitlines()[0]
        name, _, memory = first.partition(",")
        vram_mb = int(memory.strip() or "0")
        return True, name.strip(), max(0, vram_mb // 1024)
    except Exception:
        return True, "", 0


def _recommend_chat_models(vram_gb: int) -> list[str]:
    if vram_gb >= 24:
        return ["nemotron", "llama3.1:8b", "nemotron-mini"]
    if vram_gb >= 8:
        return ["llama3.1:8b", "nemotron-mini"]
    if vram_gb > 0:
        return ["nemotron-mini"]
    return []


def _recommend_comfy_profiles(vram_gb: int) -> list[str]:
    if vram_gb >= 24:
        return ["starter", "edit", "control", "video", "video-pro"]
    if vram_gb >= 12:
        return ["starter", "edit", "control", "video"]
    if vram_gb >= 8:
        return ["starter", "video"]
    return ["starter"]


def detect_workstation_profile() -> WorkstationProfile:
    """Detect a student-friendly Linux GPU workstation profile."""
    has_gpu, gpu_name, vram_gb = _detect_gpu()
    has_gui = bool(
        os.environ.get("DISPLAY")
        or os.environ.get("WAYLAND_DISPLAY")
        or os.environ.get("XDG_CURRENT_DESKTOP")
    )
    notes: list[str] = []

    if not has_gpu:
        notes.append("No NVIDIA GPU detected; local models will be CPU/cloud fallback.")
    elif vram_gb and vram_gb < 8:
        notes.append("Small VRAM GPU detected; prefer compact chat and starter image workflows.")

    if not has_gui:
        notes.append("No desktop session detected; WebUI can still run over a forwarded port.")

    if not shutil.which("nvidia-smi"):
        notes.append("nvidia-smi is missing; install or expose NVIDIA drivers for GPU detection.")

    return WorkstationProfile(
        platform=os.uname().sysname if hasattr(os, "uname") else os.name,
        has_gui=has_gui,
        has_gpu=has_gpu,
        gpu_name=gpu_name,
        vram_gb=vram_gb,
        python=_first_existing_command("python3.13", "python3.12", "python3.11", "python3"),
        nvh=_first_existing_command("nvh", "nvhive"),
        ollama=_first_existing_command("ollama"),
        recommended_chat_models=_recommend_chat_models(vram_gb),
        recommended_comfy_profiles=_recommend_comfy_profiles(vram_gb),
        notes=notes,
    )


def profile_as_dict() -> dict[str, Any]:
    return asdict(detect_workstation_profile())


def ensure_local_bin() -> Path:
    path = Path.home() / ".local" / "bin"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_launch_script(
    *,
    port: int = 3000,
    api_port: int = 8000,
    install_comfyui: bool = False,
) -> Path:
    """Create a stable launcher script used by terminal and desktop icons."""
    bin_dir = ensure_local_bin()
    script = bin_dir / "nvhive-ai-studio"
    comfy_flag = " --with-comfyui" if install_comfyui else ""
    content = f"""#!/usr/bin/env bash
set -euo pipefail

if ! command -v nvh >/dev/null 2>&1; then
  echo "nvh is not on PATH. Open a new terminal or run: source ~/.bashrc"
  exit 1
fi

export NVH_STUDENT_WORKSTATION=1
exec nvh workstation --launch --port {port} --api-port {api_port}{comfy_flag}
"""
    script.write_text(content, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def write_desktop_launcher(
    *,
    port: int = 3000,
    api_port: int = 8000,
    install_comfyui: bool = False,
) -> Path:
    """Create a Linux desktop launcher for the nvHive AI Studio."""
    script = write_launch_script(
        port=port,
        api_port=api_port,
        install_comfyui=install_comfyui,
    )
    desktop_dir = Path.home() / ".local" / "share" / "applications"
    desktop_dir.mkdir(parents=True, exist_ok=True)
    desktop_file = desktop_dir / "nvhive-ai-studio.desktop"
    content = f"""[Desktop Entry]
Type=Application
Name=NVHive AI Studio
Comment=Launch nvHive, local models, and ComfyUI setup
Exec={script}
Terminal=true
Categories=Development;Education;Science;
StartupNotify=true
"""
    desktop_file.write_text(content, encoding="utf-8")
    desktop_file.chmod(desktop_file.stat().st_mode | stat.S_IXUSR)

    desktop_copy = Path.home() / "Desktop" / "NVHive AI Studio.desktop"
    if desktop_copy.parent.is_dir():
        desktop_copy.write_text(content, encoding="utf-8")
        desktop_copy.chmod(desktop_copy.stat().st_mode | stat.S_IXUSR)

    return desktop_file


def workstation_next_steps(port: int = 3000) -> list[str]:
    return [
        f"Open WebUI: nvh webui --port {port}",
        "Install the rootless student pack: nvh studio --install starter -y",
        "Install ComfyUI from Setup > ComfyUI, or run: nvh workstation --with-comfyui",
        "Pull local chat models: nvh doctor --fix",
        "Ask anything: nvh \"explain transformers like I am in high school\"",
    ]
