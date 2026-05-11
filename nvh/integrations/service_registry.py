"""Rootless service registry for nvWizard diagnostics.

Every service should answer the same small set of questions: is it installed,
is it running, where does it live, what URL should the user open, which log
should we read, and what is the next safe rootless action?
"""

from __future__ import annotations

import os
import socket
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

from nvh.integrations.storage import storage_layout


@dataclass(frozen=True)
class ServiceStatus:
    """Common status contract for rootless apps and helper daemons."""

    id: str
    name: str
    category: str
    installed: bool
    running: bool
    ready: bool
    status: str
    summary: str
    url: str | None = None
    host: str | None = None
    port: int | None = None
    install_path: str | None = None
    launcher: str | None = None
    log_path: str | None = None
    log_tail: list[str] = field(default_factory=list)
    next_action_id: str | None = None
    next_action_label: str | None = None
    command: str | None = None
    rootless: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    checked_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


Probe = Callable[[str | Path | None], ServiceStatus]


@dataclass(frozen=True)
class ServiceDefinition:
    id: str
    aliases: tuple[str, ...]
    probe: Probe


def _port_open(host: str, port: int, *, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_ok(url: str, *, timeout: float = 0.75) -> bool:
    try:
        request = Request(url, headers={"User-Agent": "nvhive-service-registry"})
        with urlopen(request, timeout=timeout) as response:
            return 200 <= int(response.status) < 500
    except (OSError, URLError, ValueError):
        return False


def _tail(path: str | Path | None, *, lines: int = 4) -> list[str]:
    if not path:
        return []
    log_path = Path(path)
    try:
        with log_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 96_000))
            data = handle.read()
    except Exception:
        return []
    decoded = data.decode("utf-8", errors="replace").splitlines()
    interesting = [
        line.strip()
        for line in decoded[-300:]
        if line.strip()
        and any(marker in line.lower() for marker in ("error", "warning", "failed", "ready", "listening", "started", "running"))
    ]
    return interesting[-lines:] or [line.strip() for line in decoded[-lines:] if line.strip()]


def _service_error(service_id: str, name: str, exc: Exception) -> ServiceStatus:
    return ServiceStatus(
        id=service_id,
        name=name,
        category="system",
        installed=False,
        running=False,
        ready=False,
        status="probe-error",
        summary=f"{name} status check failed: {type(exc).__name__}: {exc}",
        metadata={"error": str(exc), "error_type": type(exc).__name__},
    )


def _probe_api(home_dir: str | Path | None = None) -> ServiceStatus:
    layout = storage_layout(home_dir)
    port = int(os.environ.get("NVH_API_PORT", "8000"))
    url = f"http://127.0.0.1:{port}"
    health_url = f"{url}/v1/health"
    port_ready = _port_open("127.0.0.1", port)
    healthy = _http_ok(health_url) if port_ready else False
    status = "ready" if healthy else "port-open" if port_ready else "stopped"
    return ServiceStatus(
        id="nvhive-api",
        name="nvHive API",
        category="core",
        installed=True,
        running=port_ready,
        ready=healthy,
        status=status,
        summary=(
            f"API is healthy at {health_url}."
            if healthy
            else f"API port {port} is open but /v1/health did not respond."
            if port_ready
            else "API is not listening on the expected localhost port."
        ),
        url=url,
        host="127.0.0.1",
        port=port,
        log_path=str(layout.logs_dir / "api.log"),
        log_tail=_tail(layout.logs_dir / "api.log"),
        next_action_id=None if healthy else "runtime-fallback",
        next_action_label=None if healthy else "Restart WebUI/API",
        command=f"nvh serve --port {port}",
    )


def _probe_webui(home_dir: str | Path | None = None) -> ServiceStatus:
    layout = storage_layout(home_dir)
    candidates = [int(os.environ.get("NVH_WEB_PORT", "3000")), 3000, 3001, 3002, 8080]
    seen: set[int] = set()
    running_port = None
    for port in candidates:
        if port in seen:
            continue
        seen.add(port)
        if _port_open("127.0.0.1", port):
            running_port = port
            break
    installed = (layout.home / "repo" / "web").exists() or layout.webui_dir.exists()
    url = f"http://127.0.0.1:{running_port}" if running_port else None
    return ServiceStatus(
        id="nvhive-webui",
        name="nvHive WebUI",
        category="core",
        installed=installed,
        running=running_port is not None,
        ready=bool(running_port and _http_ok(url or "")),
        status="ready" if running_port else "stopped" if installed else "not-installed",
        summary=(
            f"WebUI is reachable at {url}."
            if running_port
            else "WebUI is installed but not currently listening on the expected ports."
            if installed
            else "WebUI files are not installed in this workspace yet."
        ),
        url=url,
        host="127.0.0.1" if running_port else None,
        port=running_port,
        install_path=str(layout.home / "repo" / "web"),
        log_path=str(layout.logs_dir / "webui-bootstrap.log"),
        log_tail=_tail(layout.logs_dir / "webui-bootstrap.log"),
        next_action_id=None if running_port else "webui",
        next_action_label=None if running_port else "Launch WebUI",
        command="nvh webui",
    )


def _probe_ollama(home_dir: str | Path | None = None) -> ServiceStatus:
    from nvh.integrations.studio_packs import ollama_runtime_doctor

    layout = storage_layout(home_dir)
    doctor = ollama_runtime_doctor(home_dir=home_dir)
    running = bool(doctor.get("server_running"))
    ready = bool(doctor.get("ready"))
    next_action = doctor.get("next_action") or {}
    return ServiceStatus(
        id="ollama",
        name="Ollama Local AI",
        category="ai-runtime",
        installed=bool(doctor.get("binary_valid")),
        running=running,
        ready=ready,
        status=str(doctor.get("status") or "unknown"),
        summary=str(doctor.get("summary") or "Ollama status unavailable."),
        url="http://127.0.0.1:11434" if running else None,
        host="127.0.0.1",
        port=11434,
        install_path=str(doctor.get("local_candidate") or layout.bin_dir / "ollama"),
        launcher=str(layout.bin_dir / "nvhive-ollama-serve"),
        log_path=str(layout.studio_dir / "ollama.log"),
        log_tail=_tail(layout.studio_dir / "ollama.log"),
        next_action_id=next_action.get("id"),
        next_action_label=next_action.get("label"),
        command="nvh studio --install rootless-ollama -y" if not doctor.get("binary_valid") else "nvhive-ollama-serve",
        metadata=doctor,
    )


def _probe_comfyui(home_dir: str | Path | None = None) -> ServiceStatus:
    from nvh.integrations.comfyui import detect_comfyui

    status = detect_comfyui(home_dir=home_dir, check_http=True)
    installed = bool(status.get("installed"))
    running = bool(status.get("running") or status.get("ready"))
    service_status = str(status.get("service_status") or status.get("next_action") or "unknown")
    if running and status.get("url"):
        summary = f"ComfyUI is running at {status['url']}."
        next_action = "start-comfyui"
        next_label = "Open ComfyUI"
    elif installed:
        summary = "ComfyUI is installed but not running."
        next_action = "start-comfyui"
        next_label = "Start ComfyUI"
    else:
        summary = "ComfyUI is not installed in this workspace."
        next_action = "comfyui"
        next_label = "Install ComfyUI"
    return ServiceStatus(
        id="comfyui",
        name="ComfyUI",
        category="creative-runtime",
        installed=installed,
        running=running,
        ready=running,
        status=service_status,
        summary=summary,
        url=status.get("url"),
        host=status.get("host"),
        port=status.get("port"),
        install_path=status.get("app_dir") or status.get("install_root"),
        launcher=str(storage_layout(home_dir).bin_dir / "nvhive-comfyui"),
        log_path=status.get("log_path"),
        log_tail=list(status.get("log_tail") or []),
        next_action_id=next_action,
        next_action_label=next_label,
        command="nvh workstation --with-comfyui -y" if not installed else None,
        metadata=status,
    )


def _pack_installed(pack_id: str, packs: dict[str, Any]) -> bool:
    for pack in packs.get("packs", []):
        if pack.get("id") == pack_id:
            return bool(pack.get("status", {}).get("installed"))
    return False


def _probe_pack_group(
    home_dir: str | Path | None,
    *,
    service_id: str,
    name: str,
    category: str,
    pack_ids: tuple[str, ...],
    action_id: str,
    action_label: str,
    command: str,
    log_name: str,
) -> ServiceStatus:
    from nvh.integrations.studio_packs import catalog_with_status

    layout = storage_layout(home_dir)
    packs = catalog_with_status()
    installed_ids = [pack_id for pack_id in pack_ids if _pack_installed(pack_id, packs)]
    installed = bool(installed_ids)
    ready = len(installed_ids) == len(pack_ids)
    missing = [pack_id for pack_id in pack_ids if pack_id not in installed_ids]
    return ServiceStatus(
        id=service_id,
        name=name,
        category=category,
        installed=installed,
        running=False,
        ready=ready,
        status="ready" if ready else "partial" if installed else "not-installed",
        summary=(
            f"{name} is installed."
            if ready
            else f"{name} is partially installed; missing {', '.join(missing)}."
            if installed
            else f"{name} is not installed yet."
        ),
        install_path=str(layout.studio_dir / "packs"),
        log_path=str(layout.studio_dir / log_name),
        log_tail=_tail(layout.studio_dir / log_name),
        next_action_id=None if ready else action_id,
        next_action_label=None if ready else action_label,
        command=command,
        metadata={"installed_pack_ids": installed_ids, "missing_pack_ids": missing},
    )


def _probe_openclaw(home_dir: str | Path | None = None) -> ServiceStatus:
    return _probe_pack_group(
        home_dir,
        service_id="openclaw",
        name="OpenClaw Agent Tools",
        category="agent-runtime",
        pack_ids=("openclaw-agent",),
        action_id="claw-agents",
        action_label="Install Agent Tools",
        command="nvh studio --install claw -y",
        log_name="openclaw.log",
    )


def _probe_nemoclaw(home_dir: str | Path | None = None) -> ServiceStatus:
    return _probe_pack_group(
        home_dir,
        service_id="nemoclaw",
        name="NemoClaw Sandbox",
        category="agent-runtime",
        pack_ids=("nemoclaw-sandbox",),
        action_id="claw-agents",
        action_label="Install Agent Tools",
        command="nvh studio --install claw -y",
        log_name="nemoclaw.log",
    )


def _probe_music(home_dir: str | Path | None = None) -> ServiceStatus:
    status = _probe_pack_group(
        home_dir,
        service_id="music-studio",
        name="Music Producer Studio",
        category="creative-runtime",
        pack_ids=("ace-step-music", "music-producer-lab", "music-daw-helper"),
        action_id="music-tools",
        action_label="Install Music Studio",
        command="nvh studio --install music -y",
        log_name="ace-step.log",
    )
    running = _port_open("127.0.0.1", 7865)
    data = status.as_dict()
    data.update(
        running=running,
        ready=bool(status.ready or running),
        url="http://127.0.0.1:7865" if running else None,
        host="127.0.0.1",
        port=7865,
        status="running" if running else status.status,
        summary="ACE-Step music studio is running at http://127.0.0.1:7865." if running else status.summary,
    )
    return ServiceStatus(**data)


def _probe_vault(home_dir: str | Path | None = None) -> ServiceStatus:
    from nvh.integrations.vault import vault_status

    status = vault_status(home_dir=home_dir)
    obsidian = status.get("obsidian") or {}
    initialized = bool(status.get("initialized"))
    return ServiceStatus(
        id="vault",
        name="nvHive Vault Memory",
        category="memory",
        installed=initialized,
        running=False,
        ready=initialized,
        status="ready" if initialized else "not-initialized",
        summary="Vault memory is initialized." if initialized else "Vault memory has not been initialized yet.",
        install_path=status.get("vault_dir"),
        launcher=obsidian.get("launcher_path"),
        next_action_id=None if initialized else "vault",
        next_action_label=None if initialized else "Initialize Vault",
        command="nvh vault init",
        metadata=status,
    )


SERVICE_DEFINITIONS: tuple[ServiceDefinition, ...] = (
    ServiceDefinition("nvhive-api", ("api", "hive api", "server", "backend"), _probe_api),
    ServiceDefinition("nvhive-webui", ("webui", "web ui", "browser", "dashboard", "setup page"), _probe_webui),
    ServiceDefinition("ollama", ("ollama", "local ai", "local model", "llm", "chat", "ask ai"), _probe_ollama),
    ServiceDefinition("comfyui", ("comfy", "comfyui", "image", "video", "workflow"), _probe_comfyui),
    ServiceDefinition("openclaw", ("openclaw", "claw", "agent tools"), _probe_openclaw),
    ServiceDefinition("nemoclaw", ("nemoclaw", "nemo claw", "sandbox"), _probe_nemoclaw),
    ServiceDefinition("music-studio", ("music", "audio", "ace-step", "demucs", "whisperx"), _probe_music),
    ServiceDefinition("vault", ("vault", "obsidian", "memory", "notes"), _probe_vault),
)


def list_service_statuses(home_dir: str | Path | None = None) -> dict[str, Any]:
    services: list[dict[str, Any]] = []
    for definition in SERVICE_DEFINITIONS:
        try:
            status = definition.probe(home_dir)
        except Exception as exc:
            status = _service_error(definition.id, definition.id, exc)
        services.append(status.as_dict())
    ready = sum(1 for service in services if service.get("ready"))
    running = sum(1 for service in services if service.get("running"))
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "service_count": len(services),
        "ready_count": ready,
        "running_count": running,
        "rootless": True,
        "services": services,
    }


def service_status(service_id: str, home_dir: str | Path | None = None) -> dict[str, Any]:
    normalized = service_id.strip().lower()
    for definition in SERVICE_DEFINITIONS:
        if normalized == definition.id or normalized in definition.aliases:
            try:
                return definition.probe(home_dir).as_dict()
            except Exception as exc:
                return _service_error(definition.id, definition.id, exc).as_dict()
    raise KeyError(f"Unknown nvHive service: {service_id}")


def service_for_question(question: str) -> dict[str, Any] | None:
    q = question.lower()
    matches: list[tuple[int, ServiceDefinition]] = []
    for definition in SERVICE_DEFINITIONS:
        score = 0
        for alias in (definition.id, *definition.aliases):
            if alias and alias in q:
                score = max(score, len(alias))
        if score:
            matches.append((score, definition))
    if not matches:
        if any(word in q for word in ("service", "services", "port", "url", "launch", "running", "start", "logs")):
            return {"id": "all", "aliases": (), "probe": None}
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    definition = matches[0][1]
    return {"id": definition.id, "aliases": definition.aliases, "probe": definition.probe}
