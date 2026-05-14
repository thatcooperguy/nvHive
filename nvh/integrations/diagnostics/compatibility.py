"""Host and application compatibility checks for AI Wizard.

The compatibility layer separates problems nvHive can fix rootlessly from
problems that require a different base image, GPU session, driver, or OS.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from nvh.integrations.installs.studio_packs import (
    BLENDER_VERSION,
    _docker_status,
    _node_runtime_status,
    catalog_with_status,
    model_catalog_with_status,
)
from nvh.integrations.services.runtime import runtime_status
from nvh.integrations.workspace.storage import storage_status
from nvh.utils.gpu import detect_gpu_status, gpu_architecture_info


@dataclass(frozen=True)
class HostFact:
    """One detected host capability or dependency."""

    id: str
    label: str
    value: str
    status: str
    severity: str = "info"
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CompatibilityRequirement:
    """One app requirement and how to satisfy it."""

    id: str
    label: str
    status: str
    detail: str
    fix_action_id: str | None = None
    rootless_fix_available: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AppCompatibility:
    """Compatibility summary for one nvHive-managed app or pack."""

    id: str
    title: str
    category: str
    status: str
    severity: str
    summary: str
    recommended_action_id: str | None = None
    rootless_fix_available: bool = False
    requirements: list[CompatibilityRequirement] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["requirements"] = [req.as_dict() for req in self.requirements]
        return data


OPTIONAL_PROFILE_IDS = {
    "agent-lab",
    "claw-agents",
    "blender-creative",
    "game-dev-lab",
    "music-producer-lab",
}


def _parse_version(value: str | None) -> tuple[int, ...]:
    if not value:
        return ()
    parts: list[int] = []
    for chunk in str(value).split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if digits == "":
            break
        parts.append(int(digits))
    return tuple(parts)


def _version_at_least(value: str | None, minimum: str) -> bool:
    current = _parse_version(value)
    target = _parse_version(minimum)
    if not current:
        return False
    width = max(len(current), len(target))
    return current + (0,) * (width - len(current)) >= target + (0,) * (width - len(target))


def _which(command: str) -> str | None:
    return shutil.which(command)


def _command_version(command: str, *args: str, timeout: float = 4.0) -> str:
    executable = _which(command)
    if not executable:
        return ""
    try:
        result = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return ""
    return (result.stdout or result.stderr).strip().splitlines()[0] if (result.stdout or result.stderr) else ""


def _read_os_release() -> dict[str, str]:
    path = Path("/etc/os-release")
    if not path.exists():
        return {}
    data: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key] = value.strip().strip('"')
    except Exception:
        return {}
    return data


def _nvidia_smi_query() -> dict[str, str]:
    status = detect_gpu_status()
    gpus = status.get("gpus", [])
    if not gpus:
        return {
            "detection_status": str(status.get("status") or "not-detected"),
            "detection_source": str(status.get("source") or "none"),
            "detection_issues": json.dumps(status.get("issues", [])),
            "device_files_present": str(bool(status.get("device_files_present", False))).lower(),
        }
    gpu = gpus[0]
    arch = gpu_architecture_info(gpu)
    return {
        "name": gpu.name,
        "memory_total_mb": str(gpu.vram_mb),
        "memory_free_mb": str(gpu.memory_free_mb),
        "memory_used_mb": str(gpu.memory_used_mb),
        "driver_version": gpu.driver_version,
        "cuda_version": gpu.cuda_version,
        "compute_capability": ".".join(str(part) for part in arch["compute_capability"]),
        "compute_capability_source": str(arch["compute_capability_source"]),
        "architecture": str(arch["architecture"]),
        "architecture_heuristic": str(bool(arch["heuristic"])).lower(),
        "detection_status": str(status.get("status") or "ready"),
        "detection_source": str(status.get("source") or ""),
        "detection_issues": json.dumps(status.get("issues", [])),
        "device_files_present": str(bool(status.get("device_files_present", False))).lower(),
    }


def _nvidia_cuda_version() -> str:
    if not _which("nvidia-smi"):
        return ""
    try:
        result = subprocess.run(
            ["nvidia-smi"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception:
        return ""
    text = result.stdout or ""
    marker = "CUDA Version:"
    if marker not in text:
        return ""
    return text.split(marker, 1)[1].split("|", 1)[0].strip()


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def _host_facts() -> dict[str, Any]:
    os_release = _read_os_release()
    libc_name, libc_version = platform.libc_ver()
    nvidia = _nvidia_smi_query()
    runtime = runtime_status()
    storage = storage_status(min_free_gb=20)
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return {
        "platform": sys.platform,
        "system": platform.system(),
        "machine": platform.machine(),
        "kernel": platform.release(),
        "distro": os_release.get("PRETTY_NAME") or os_release.get("NAME") or platform.platform(),
        "libc": {"name": libc_name, "version": libc_version},
        "python": {
            "executable": sys.executable,
            "version": py_version,
            "venv_available": runtime.venv_available,
            "pip_available": runtime.pip_available,
            "strategy": runtime.strategy,
        },
        "commands": {
            "git": _which("git"),
            "curl": _which("curl"),
            "tar": _which("tar"),
            "node": _which("node"),
            "npm": _which("npm"),
            "docker": _which("docker"),
            "nvidia-smi": _which("nvidia-smi"),
        },
        "command_versions": {
            "git": _command_version("git", "--version"),
            "node": _command_version("node", "--version"),
            "npm": _command_version("npm", "--version"),
            "docker": _command_version("docker", "--version"),
        },
        "gpu": nvidia,
        "display": {
            "DISPLAY": os.environ.get("DISPLAY", ""),
            "WAYLAND_DISPLAY": os.environ.get("WAYLAND_DISPLAY", ""),
        },
        "ports": {
            "ollama_11434": _port_open(11434),
            "comfyui_8188": _port_open(8188),
        },
        "storage": storage.as_dict(),
    }


def _fact_list(
    host: dict[str, Any],
    *,
    node_status: dict[str, Any] | None = None,
    docker_status: dict[str, Any] | None = None,
) -> list[HostFact]:
    commands = host["commands"]
    gpu = host["gpu"]
    node_status = node_status if node_status is not None else _node_runtime_status()
    docker_status = docker_status if docker_status is not None else _docker_status()
    gpu_ready = bool(gpu.get("name"))
    display_ready = bool(host["display"].get("DISPLAY") or host["display"].get("WAYLAND_DISPLAY"))
    return [
        HostFact("os", "Base OS", f"{host['distro']} / {host['kernel']}", "detected"),
        HostFact("arch", "Architecture", str(host["machine"]), "ok"),
        HostFact("python", "Python", host["python"]["version"], "ok" if _version_at_least(host["python"]["version"], "3.11") else "blocked", "required"),
        HostFact("pip", "pip", "available" if host["python"]["pip_available"] else "missing", "ok" if host["python"]["pip_available"] else "fixable", "recommended"),
        HostFact("venv", "Python venv", "available" if host["python"]["venv_available"] else "missing", "ok" if host["python"]["venv_available"] else "fixable", "recommended"),
        HostFact("git", "Git", commands.get("git") or "missing", "ok" if commands.get("git") else "blocked", "required"),
        HostFact("curl", "curl", commands.get("curl") or "missing", "ok" if commands.get("curl") else "blocked", "required"),
        HostFact("node", "Node.js", host["command_versions"].get("node") or "rootless install available", "ok" if node_status.get("ready") else "fixable", "recommended"),
        HostFact("docker", "Docker runtime", host["command_versions"].get("docker") or "missing", "ok" if docker_status.get("ready") else "degraded", "optional", "Required only for NemoClaw/OpenShell sandboxes."),
        HostFact("nvidia-smi", "NVIDIA driver", gpu.get("driver_version") or gpu.get("detection_status", "not detected"), "ok" if gpu_ready else "degraded", "recommended"),
        HostFact("cuda", "CUDA driver API", gpu.get("cuda_version", "unknown"), "ok" if gpu.get("cuda_version") else "degraded", "recommended"),
        HostFact("display", "Linux desktop display", "available" if display_ready else "not detected", "ok" if display_ready else "degraded", "optional"),
        HostFact("storage", "Persistent NVH_HOME", host["storage"]["layout"]["home"], "ok" if host["storage"]["ok"] and host["storage"]["configured_by"] != "default" else "fixable", "required"),
    ]


def _req(
    req_id: str,
    label: str,
    ok: bool,
    detail: str,
    *,
    fix_action_id: str | None = None,
    rootless_fix_available: bool = False,
    blocked: bool = False,
) -> CompatibilityRequirement:
    if ok:
        status = "ok"
    elif blocked:
        status = "blocked"
    elif rootless_fix_available:
        status = "fixable"
    else:
        status = "warning"
    return CompatibilityRequirement(
        id=req_id,
        label=label,
        status=status,
        detail=detail,
        fix_action_id=fix_action_id,
        rootless_fix_available=rootless_fix_available,
    )


def _overall(
    app_id: str,
    title: str,
    category: str,
    requirements: list[CompatibilityRequirement],
    *,
    recommended_action_id: str | None = None,
    notes: list[str] | None = None,
) -> AppCompatibility:
    if any(req.status == "blocked" for req in requirements):
        status = "blocked"
        severity = "required"
        summary = "Needs a different base image, driver, OS package, or admin-provided dependency."
    elif any(req.status == "fixable" for req in requirements):
        status = "fixable"
        severity = "recommended"
        summary = "nvHive can repair or install the missing pieces without root."
    elif any(req.status == "warning" for req in requirements):
        status = "degraded"
        severity = "optional"
        summary = "Can run, but some capabilities may be slower or limited."
    else:
        status = "ready"
        severity = "info"
        summary = "Ready on this host."
    return AppCompatibility(
        id=app_id,
        title=title,
        category=category,
        status=status,
        severity=severity,
        summary=summary,
        recommended_action_id=recommended_action_id if status != "ready" else None,
        rootless_fix_available=any(req.rootless_fix_available for req in requirements),
        requirements=requirements,
        notes=notes or [],
    )


def recommended_torch_profile(cuda_version: str | None) -> str:
    """Pick the safest ComfyUI torch profile from the driver-reported CUDA API."""
    if _version_at_least(cuda_version, "13.0"):
        return "nvidia-cu130"
    if _version_at_least(cuda_version, "12.1"):
        return "nvidia-cu121"
    return "cpu"


def compatibility_report(
    home_dir: str | Path | None = None,
    *,
    model_status: dict[str, Any] | None = None,
    pack_status: dict[str, Any] | None = None,
    node_status: dict[str, Any] | None = None,
    docker_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return host facts and app compatibility recommendations."""
    host = _host_facts()
    if home_dir:
        host["storage"] = storage_status(home_dir=home_dir, min_free_gb=20).as_dict()
    gpu = host["gpu"]
    gpu_ready = bool(gpu.get("name"))
    commands = host["commands"]
    py = host["python"]
    storage = host["storage"]
    is_linux = host["system"] == "Linux"
    arch = str(host["machine"]).lower()
    display_ready = bool(host["display"].get("DISPLAY") or host["display"].get("WAYLAND_DISPLAY"))
    cuda_profile = recommended_torch_profile(gpu.get("cuda_version"))
    model_status = model_status if model_status is not None else model_catalog_with_status(home_dir=home_dir)
    pack_status = pack_status if pack_status is not None else catalog_with_status()
    pack_by_id = {pack.get("id"): pack for pack in pack_status.get("packs", [])}
    node_status = node_status if node_status is not None else _node_runtime_status()
    docker_status = docker_status if docker_status is not None else _docker_status()
    recommended_models = model_status.get("recommended_ids", [])
    missing_recommended_models = [
        model["id"] for model in model_status.get("models", [])
        if model.get("recommended") and not model.get("installed")
    ]
    try:
        from nvh.integrations.installs.comfyui import detect_comfyui

        comfy_status = detect_comfyui(home_dir=home_dir)
    except Exception:
        comfy_status = {
            "installed": False,
            "running": False,
            "examples_installed": False,
            "port_conflict": False,
            "service_status": "unknown",
            "runtime_python": "",
            "url": "http://127.0.0.1:8188",
        }

    def _pack_installed(pack_id: str) -> bool:
        status = pack_by_id.get(pack_id, {}).get("status", {})
        return bool(status.get("installed"))

    apps = [
        _overall(
            "persistent-storage",
            "Persistent NVH_HOME",
            "foundation",
            [
                _req(
                    "storage",
                    "Mounted storage",
                    bool(storage["ok"] and storage["configured_by"] != "default"),
                    "Use a mounted volume that survives cloud desktop reconnects.",
                    fix_action_id="storage",
                    rootless_fix_available=True,
                )
            ],
            recommended_action_id="storage",
        ),
        _overall(
            "rootless-ollama",
            "Rootless Ollama Runtime",
            "runtime",
            [
                _req("linux", "Linux host", is_linux, "The rootless Ollama pack targets Linux cloud desktops.", blocked=not is_linux),
                _req("arch", "CPU architecture", arch in {"x86_64", "amd64", "aarch64", "arm64"}, f"Detected {host['machine']}.", blocked=arch not in {"x86_64", "amd64", "aarch64", "arm64"}),
                _req("curl", "curl", bool(commands.get("curl")), "Needed to download the Ollama bundle.", blocked=not commands.get("curl")),
                _req("tar", "tar", bool(commands.get("tar")), "Needed to extract the Ollama bundle.", blocked=not commands.get("tar")),
                _req("port", "Port 11434", not host["ports"]["ollama_11434"] or model_status.get("ollama_running"), "Ollama uses localhost:11434."),
            ],
            recommended_action_id="rootless-ollama",
        ),
        _overall(
            "local-models",
            "Recommended Local Models",
            "model",
            [
                _req("ollama", "Ollama runtime", bool(model_status.get("ollama_available")), "Required for local LLM downloads.", fix_action_id="rootless-ollama", rootless_fix_available=True),
                _req("gpu", "NVIDIA GPU", gpu_ready, "GPU acceleration is strongly recommended; CPU fallback is slower."),
                _req("models", "Recommended models", not missing_recommended_models, f"{len(missing_recommended_models)} recommended model(s) missing.", fix_action_id="starter-models", rootless_fix_available=True),
            ],
            recommended_action_id="starter-models",
            notes=[f"Recommended model ids: {', '.join(recommended_models)}"] if recommended_models else [],
        ),
        _overall(
            "comfyui",
            "ComfyUI Visual Workspace",
            "creative",
            [
                _req("git", "Git", bool(commands.get("git")), "Required to clone/update ComfyUI.", blocked=not commands.get("git")),
                _req("source", "ComfyUI source", bool(comfy_status.get("installed")), f"Install path: {comfy_status.get('app_dir', 'NVH_HOME/comfyui/ComfyUI')}.", fix_action_id="comfyui", rootless_fix_available=True),
                _req("python", "ComfyUI Python runtime", _version_at_least(py["version"], "3.11") or bool(comfy_status.get("runtime_python")), f"Runtime: {comfy_status.get('runtime_python') or py['version']}.", fix_action_id="runtime-fallback", rootless_fix_available=True),
                _req("venv", "Python venv/pip", bool(comfy_status.get("installed") or (py["venv_available"] and py["pip_available"])), f"Runtime strategy: {py['strategy']}.", fix_action_id="runtime-fallback", rootless_fix_available=True),
                _req("torch", "PyTorch CUDA profile", bool(comfy_status.get("installed") or cuda_profile != "cpu"), f"Recommended profile: {cuda_profile}. CPU fallback is available."),
                _req("storage", "Persistent storage", bool(storage["ok"]), "ComfyUI and model caches are large.", fix_action_id="storage", rootless_fix_available=True),
                _req("examples", "nvHive examples", bool(comfy_status.get("examples_installed")) or not comfy_status.get("installed"), "Starter workflow manifest is installed with ComfyUI.", fix_action_id="comfyui-examples", rootless_fix_available=True),
                _req("service", "ComfyUI service", not comfy_status.get("port_conflict"), f"Service status: {comfy_status.get('service_status')}; URL: {comfy_status.get('url')}.", fix_action_id="start-comfyui", rootless_fix_available=True),
            ],
            recommended_action_id=(
                "start-comfyui"
                if comfy_status.get("installed") and not comfy_status.get("running")
                else "comfyui"
            ),
            notes=[
                f"Recommended torch profile for this host: {cuda_profile}.",
                (
                    "ComfyUI is optional for local chat; install/start it only for visual image or video workflows."
                    if not comfy_status.get("installed")
                    else f"ComfyUI status: {comfy_status.get('service_status')}."
                ),
            ],
        ),
        _overall(
            "blender-creative",
            "Blender Creative Studio",
            "creative",
            [
                _req("linux-x64", "Linux x64 desktop", is_linux and arch in {"x86_64", "amd64"}, "The bundled Blender pack currently targets Linux x64.", blocked=not (is_linux and arch in {"x86_64", "amd64"})),
                _req("display", "Desktop display", display_ready, "Blender needs X11 or Wayland for interactive launch."),
                _req("glibc", "glibc", not is_linux or _version_at_least(host["libc"]["version"], "2.31"), f"Detected {host['libc']['name']} {host['libc']['version'] or 'unknown'}.", blocked=is_linux and bool(host["libc"]["version"]) and not _version_at_least(host["libc"]["version"], "2.31")),
                _req("storage", "Persistent app storage", bool(storage["ok"]), "Blender installs under NVH_HOME/apps/blender.", fix_action_id="storage", rootless_fix_available=True),
            ],
            recommended_action_id="creative-tools",
            notes=[f"Bundled Blender version: {BLENDER_VERSION}."],
        ),
        _overall(
            "agent-lab",
            "Local Agent Lab",
            "agent",
            [
                _req("pack", "Agent lab pack", _pack_installed("agent-lab"), "Installs the local agent helper environment under NVH_HOME.", fix_action_id="agent-lab", rootless_fix_available=True),
                _req("python", "Python 3.11+", _version_at_least(py["version"], "3.11"), f"Detected Python {py['version']}.", blocked=not _version_at_least(py["version"], "3.11")),
                _req("venv", "Python venv/pip", bool(py["venv_available"] and py["pip_available"]), f"Runtime strategy: {py['strategy']}.", fix_action_id="runtime-fallback", rootless_fix_available=True),
                _req("storage", "Persistent workspace", bool(storage["ok"]), "Agent packages install under NVH_HOME/studio.", fix_action_id="storage", rootless_fix_available=True),
            ],
            recommended_action_id="agent-lab",
        ),
        _overall(
            "claw-agents",
            "OpenClaw and NVIDIA NemoClaw",
            "agent",
            [
                _req("linux", "Linux desktop session", is_linux, "OpenClaw/NemoClaw packs are optimized for Linux cloud desktops.", blocked=not is_linux),
                _req("node", "Node.js 22.16+ and npm 10+", bool(node_status.get("ready")), f"Node={node_status.get('node_version') or 'missing'}, npm={node_status.get('npm_version') or 'missing'}.", fix_action_id="claw-agents", rootless_fix_available=bool(node_status.get("can_auto_install"))),
                _req("openclaw", "OpenClaw pack", _pack_installed("openclaw-agent"), "Simple self-hosted agent platform install.", fix_action_id="claw-agents", rootless_fix_available=True),
                _req("docker", "Docker for NemoClaw", bool(docker_status.get("ready")), docker_status.get("detail", "Docker must work without sudo.")),
                _req("storage", "Persistent workspace", bool(storage["ok"]), "Claw workspaces install under NVH_HOME/studio.", fix_action_id="storage", rootless_fix_available=True),
            ],
            recommended_action_id="claw-agents",
            notes=[
                "OpenClaw is the default rootless agent path.",
                "NemoClaw remains optional until Docker/OpenShell is available without sudo.",
            ],
        ),
        _overall(
            "game-dev-lab",
            "Game Dev Lab",
            "game",
            [
                _req("python", "Python 3.11+", _version_at_least(py["version"], "3.11"), f"Detected Python {py['version']}.", blocked=not _version_at_least(py["version"], "3.11")),
                _req("display", "Desktop display", display_ready, "Interactive samples need a display; headless asset generation can still work."),
                _req("storage", "Persistent workspace", bool(storage["ok"]), "Game projects install under NVH_HOME/studio.", fix_action_id="storage", rootless_fix_available=True),
            ],
            recommended_action_id="game-tools",
        ),
        _overall(
            "music-producer-lab",
            "Music Producer Studio",
            "music",
            [
                _req("linux", "Linux desktop session", is_linux, "ACE-Step and AppImage helpers are optimized for Linux cloud desktops.", blocked=not is_linux),
                _req("python", "Python 3.11+", _version_at_least(py["version"], "3.11"), f"Detected Python {py['version']}.", blocked=not _version_at_least(py["version"], "3.11")),
                _req("venv", "Python venv/pip", bool(py["venv_available"] and py["pip_available"]), f"Runtime strategy: {py['strategy']}.", fix_action_id="runtime-fallback", rootless_fix_available=True),
                _req("git", "Git for ACE-Step", shutil.which("git") is not None, "ACE-Step is cloned from the official GitHub repository.", blocked=shutil.which("git") is None),
                _req("storage", "Persistent music workspace", bool(storage["ok"]), "Music tools and model caches install under NVH_HOME/studio.", fix_action_id="storage", rootless_fix_available=True),
            ],
            recommended_action_id="music-tools",
            notes=[
                "ACE-Step handles AI music generation; the audio lab handles stems, transcription, and cleanup.",
                "NVIDIA GPU acceleration depends on the driver/CUDA exposed by the cloud image.",
            ],
        ),
    ]

    issue_count = sum(
        1
        for app in apps
        if app.status != "ready" and app.severity != "optional" and app.id not in OPTIONAL_PROFILE_IDS
    )
    blocked_count = sum(1 for app in apps if app.status == "blocked")
    fixable_count = sum(
        1
        for app in apps
        if (
            app.rootless_fix_available
            and app.status != "ready"
            and app.severity != "optional"
            and app.id not in OPTIONAL_PROFILE_IDS
        )
    )
    return {
        "summary": (
            "Host is ready"
            if issue_count == 0
            else f"{issue_count} app/profile compatibility item(s) need attention"
        ),
        "ready": issue_count == 0,
        "issue_count": issue_count,
        "blocked_count": blocked_count,
        "rootless_fixable_count": fixable_count,
        "recommended_torch_profile": cuda_profile,
        "host": host,
        "facts": [fact.as_dict() for fact in _fact_list(host, node_status=node_status, docker_status=docker_status)],
        "apps": [app.as_dict() for app in apps],
    }
