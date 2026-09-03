"""Student workstation helpers for Linux GPU desktop sessions."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from nvh.core import local_models
from nvh.core.local_models import TierBudget
from nvh.integrations.workspace.storage import StorageStatus, ensure_storage, storage_status


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
    storage_home: str
    storage_ok: bool
    storage_free_gb: float | None
    storage_env_file: str
    notes: list[str]
    # What the model ladders plan against (nvh.core.local_models.tier_budget):
    # the pool minus the OS reserve on unified memory, the summed VRAM otherwise.
    budget_gb: int = 0
    unified_memory: bool = False


def _first_existing_command(*names: str) -> str:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return ""


def _detect_gpu_rows() -> list[Any]:
    """GPU rows to budget against: ``nvh.utils.gpu`` first, nvidia-smi as the fallback.

    Rows only need ``name`` / ``vram_mb`` / ``unified_memory``;
    ``local_models.tier_budget`` reads them through ``getattr``.
    """
    try:
        from nvh.utils.gpu import detect_gpus

        rows: list[Any] = list(detect_gpus())
    except Exception:
        rows = []
    if rows:
        return rows

    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return []
    try:
        from nvh.utils.gpu import is_unified_memory_gpu_name

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


def _system_memory() -> Any:
    """SystemMemoryInfo for the CPU-offload bonus, or None when it cannot be read."""
    try:
        from nvh.utils.gpu import detect_system_memory

        return detect_system_memory()
    except Exception:
        return None


def _detect_gpu(rows: list[Any] | None = None) -> tuple[bool, str, int]:
    """``(has_gpu, name, total_gb)`` for the detected rows.

    ``total_gb`` is what the cards report; the recommendations read the
    :class:`TierBudget` instead (see :func:`detect_workstation_profile`).
    """
    rows = _detect_gpu_rows() if rows is None else rows
    if not rows:
        # nvidia-smi on PATH but no row could be read: a GPU with no usable data.
        return bool(shutil.which("nvidia-smi")), "", 0
    budget = local_models.tier_budget(rows, None)
    sized = [row for row in rows if float(getattr(row, "vram_mb", 0) or 0) > 0]
    primary = sized[0] if sized else rows[0]
    return True, str(getattr(primary, "name", "") or ""), int(budget.total_gb)


def _recommend_chat_models(vram_gb: int | float | TierBudget) -> list[str]:
    """Pull list for the budget from the one VRAM-tier table (nvh.core.local_models).

    Chat first, then code, vision, embeddings and the small always-fits
    fallback; MoE picks lead on a unified pool. No GPU (0 GB, or a budget
    with no sized rows) means no local pulls: the workstation leaves CPU-only
    sessions to cloud providers.
    """
    if isinstance(vram_gb, TierBudget):
        if vram_gb.sized_gpus == 0:
            return []
        budget: TierBudget | float = vram_gb
    else:
        if vram_gb <= 0:
            return []
        budget = float(vram_gb)
    return [pick.tag for pick in local_models.recommended(budget)]


def _recommend_comfy_profiles(vram_gb: int) -> list[str]:
    if vram_gb >= 24:
        return ["starter", "edit", "control", "video", "video-pro"]
    if vram_gb >= 12:
        return ["starter", "edit", "control", "video"]
    if vram_gb >= 8:
        return ["starter", "video"]
    return ["starter"]


def detect_workstation_profile(home_dir: str | Path | None = None) -> WorkstationProfile:
    """Detect a student-friendly Linux GPU workstation profile."""
    rows = _detect_gpu_rows()
    has_gpu, gpu_name, vram_gb = _detect_gpu(rows)
    # The recommendations read the budget, not the raw total: a unified pool
    # loses the OS reserve (a 128 GB GB10 plans 112 GB), a discrete card's
    # summed VRAM stands as is.
    budget = local_models.tier_budget(rows, _system_memory())
    budget_gb = int(budget.budget_gb)
    storage = storage_status(home_dir=home_dir)
    has_gui = bool(
        os.environ.get("DISPLAY")
        or os.environ.get("WAYLAND_DISPLAY")
        or os.environ.get("XDG_CURRENT_DESKTOP")
    )
    notes: list[str] = []

    if not has_gpu:
        notes.append("No NVIDIA GPU detected; local models will be CPU/cloud fallback.")
    elif budget_gb and budget_gb < 8:
        notes.append("Small VRAM GPU detected; prefer compact chat and starter image workflows.")
    if budget.unified:
        # The reserve is the pool's own (local_models.unified_os_reserve_gb):
        # 16 GB on a 128 GB GB10, 8 GB on a 64 GB pool -- not the flat GB10 figure.
        notes.append(
            f"Unified memory: {budget.total_gb:.0f} GB shared by CPU and GPU; "
            f"{budget.budget_gb:.0f} GB is planned for models after the "
            f"{budget.os_reserve_gb:.0f} GB OS reserve."
        )

    if not has_gui:
        notes.append("No desktop session detected; WebUI can still run over a forwarded port.")

    if not shutil.which("nvidia-smi"):
        notes.append("nvidia-smi is missing; install or expose NVIDIA drivers for GPU detection.")
    notes.extend(storage.warnings)

    return WorkstationProfile(
        platform=os.uname().sysname if hasattr(os, "uname") else os.name,
        has_gui=has_gui,
        has_gpu=has_gpu,
        gpu_name=gpu_name,
        vram_gb=vram_gb,
        python=_first_existing_command("python3.13", "python3.12", "python3.11", "python3"),
        nvh=_first_existing_command("nvh", "nvhive"),
        ollama=_first_existing_command("ollama"),
        recommended_chat_models=_recommend_chat_models(budget),
        recommended_comfy_profiles=_recommend_comfy_profiles(budget_gb),
        storage_home=str(storage.layout.home),
        storage_ok=storage.ok,
        storage_free_gb=storage.free_gb,
        storage_env_file=str(storage.env_file),
        notes=notes,
        budget_gb=budget_gb,
        unified_memory=budget.unified,
    )


def profile_as_dict(home_dir: str | Path | None = None) -> dict[str, Any]:
    return asdict(detect_workstation_profile(home_dir=home_dir))


def ensure_local_bin(home_dir: str | Path | None = None) -> Path:
    storage = ensure_storage(home_dir)
    return storage.layout.bin_dir


def write_launch_script(
    *,
    port: int = 3000,
    api_port: int = 8000,
    install_comfyui: bool = False,
    storage: StorageStatus | None = None,
) -> Path:
    """Create a stable launcher script used by terminal and desktop icons."""
    storage = storage or ensure_storage()
    bin_dir = storage.layout.bin_dir
    script = bin_dir / "nvhive-ai-studio"
    comfy_flag = " --with-comfyui" if install_comfyui else ""
    exports = "\n".join(storage.layout.export_lines())
    content = f"""#!/usr/bin/env bash
set -euo pipefail

{exports}

if ! command -v nvh >/dev/null 2>&1; then
  echo "nvh is not on PATH. Run: source {storage.env_file}"
  exit 1
fi

export NVH_STUDENT_WORKSTATION=1
exec nvh workstation --home-dir "{storage.layout.home}" --launch --port {port} --api-port {api_port}{comfy_flag}
"""
    script.write_text(content, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def write_desktop_launch_script(
    *,
    port: int = 3000,
    api_port: int = 8000,
    storage: StorageStatus | None = None,
) -> Path:
    """Create a GUI-friendly launcher that opens the WebUI without a terminal."""
    storage = storage or ensure_storage()
    bin_dir = storage.layout.bin_dir
    script = bin_dir / "nvhive-ai-studio-desktop"
    exports = "\n".join(storage.layout.export_lines())
    content = f"""#!/usr/bin/env bash
set -u

{exports}

PORT="{port}"
API_PORT="{api_port}"
URL="http://localhost:${{PORT}}/setup"
API_URL="http://localhost:${{API_PORT}}/v1/health"
LOG_DIR="${{NVH_LOGS:-{storage.layout.logs_dir}}}"
LOG="$LOG_DIR/desktop-launcher.log"
mkdir -p "$LOG_DIR"

log() {{
  printf '%s %s\\n' "$(date -Is 2>/dev/null || date)" "$*" >> "$LOG"
}}

webui_ready() {{
  if command -v curl >/dev/null 2>&1; then
    curl -fsS "http://localhost:${{PORT}}/setup" >/dev/null 2>&1 \
      || curl -fsS "http://localhost:${{PORT}}/" >/dev/null 2>&1
    return $?
  fi
  if command -v wget >/dev/null 2>&1; then
    wget -qO- "http://localhost:${{PORT}}/setup" >/dev/null 2>&1 \
      || wget -qO- "http://localhost:${{PORT}}/" >/dev/null 2>&1
    return $?
  fi
  return 1
}}

api_ready() {{
  # The WebUI is useless without the API — every panel renders empty,
  # producing the silent "nothing ever loaded" failure mode. Check API
  # health (port {api_port} by default) before declaring the stack ready.
  if command -v curl >/dev/null 2>&1; then
    curl -fsS "$API_URL" >/dev/null 2>&1
    return $?
  fi
  if command -v wget >/dev/null 2>&1; then
    wget -qO- "$API_URL" >/dev/null 2>&1
    return $?
  fi
  return 1
}}

server_ready() {{
  webui_ready && api_ready
}}

open_url() {{
  log "opening $URL"
  ROOTLESS_FIREFOX="${{NVH_APPS_HOME:-{storage.layout.apps_dir}}}/firefox/firefox"
  FIREFOX_PROFILE="${{NVH_FIREFOX_PROFILE:-${{NVH_STATE:-{storage.layout.state_dir}}}/browser-profiles/desktop}}"
  mkdir -p "$FIREFOX_PROFILE"
  if [ -x "$ROOTLESS_FIREFOX" ]; then
    "$ROOTLESS_FIREFOX" --new-instance --no-remote --profile "$FIREFOX_PROFILE" --new-window "$URL" >/dev/null 2>&1 &
  elif command -v firefox >/dev/null 2>&1; then
    firefox --new-instance --no-remote --profile "$FIREFOX_PROFILE" --new-window "$URL" >/dev/null 2>&1 &
  elif command -v firefox-esr >/dev/null 2>&1; then
    firefox-esr --new-instance --no-remote --profile "$FIREFOX_PROFILE" --new-window "$URL" >/dev/null 2>&1 &
  elif command -v chromium >/dev/null 2>&1; then
    chromium --new-window "$URL" >/dev/null 2>&1 &
  elif command -v chromium-browser >/dev/null 2>&1; then
    chromium-browser --new-window "$URL" >/dev/null 2>&1 &
  elif command -v google-chrome-stable >/dev/null 2>&1; then
    google-chrome-stable --new-window "$URL" >/dev/null 2>&1 &
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 &
  elif command -v gio >/dev/null 2>&1; then
    gio open "$URL" >/dev/null 2>&1 &
  elif command -v sensible-browser >/dev/null 2>&1; then
    sensible-browser "$URL" >/dev/null 2>&1 &
  else
    log "no browser opener found; open $URL manually"
  fi
}}

if ! command -v nvh >/dev/null 2>&1; then
  log "nvh is not on PATH. Run: source {storage.env_file}"
  exit 1
fi

if server_ready; then
  open_url
  exit 0
fi

log "starting nvHive WebUI on port $PORT"
nohup nvh webui --port "$PORT" --api-port "$API_PORT" -y >> "$LOG" 2>&1 &

attempts=0
while [ "$attempts" -lt 90 ]; do
  if server_ready; then
    log "WebUI + API both ready after ${{attempts}}s"
    open_url
    exit 0
  fi
  attempts=$((attempts + 1))
  sleep 1
done

# Diagnose which of the two stalled so the user can grep this log.
if webui_ready; then
  log "WebUI is up on $PORT but API on $API_PORT did not respond after 90s"
  log "Panels will be empty until the API is healthy. See $LOG_DIR/api-server.log."
else
  log "Neither WebUI on $PORT nor API on $API_PORT responded after 90s"
  log "Opening anyway. See $LOG_DIR/api-server.log for the underlying error."
fi
open_url
exit 0
"""
    script.write_text(content, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def write_desktop_launcher(
    *,
    port: int = 3000,
    api_port: int = 8000,
    install_comfyui: bool = False,
    storage: StorageStatus | None = None,
) -> Path:
    """Create a Linux desktop launcher for the nvHive AI Studio."""
    storage = storage or ensure_storage()
    write_launch_script(
        port=port,
        api_port=api_port,
        install_comfyui=install_comfyui,
        storage=storage,
    )
    desktop_script = write_desktop_launch_script(
        port=port,
        api_port=api_port,
        storage=storage,
    )
    desktop_dir = Path.home() / ".local" / "share" / "applications"
    desktop_dir.mkdir(parents=True, exist_ok=True)
    desktop_file = desktop_dir / "nvhive-ai-studio.desktop"
    content = f"""[Desktop Entry]
Type=Application
Name=NVHive AI Studio
Comment=Launch nvHive, local models, and ComfyUI setup
Exec={desktop_script}
Terminal=false
Icon=applications-development
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


def workstation_next_steps(port: int = 3000, storage: StorageStatus | None = None) -> list[str]:
    storage = storage or storage_status()
    return [
        f"Persist this session: source {storage.env_file}",
        f"Open WebUI: nvh webui --port {port}",
        "Install the rootless student pack: nvh studio --install starter -y",
        "Install ComfyUI from Setup > ComfyUI, or run: nvh workstation --with-comfyui",
        "Pull local chat models: nvh status --deep --fix",
        "Ask anything: nvh \"explain transformers like I am in high school\"",
    ]
