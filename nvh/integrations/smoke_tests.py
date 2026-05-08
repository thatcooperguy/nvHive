"""Lightweight smoke tests for rootless nvHive apps."""

from __future__ import annotations

import socket
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from nvh.integrations.comfyui import detect_comfyui
from nvh.integrations.storage import storage_status
from nvh.integrations.studio_packs import _node_runtime_status, catalog_with_status, ollama_runtime_doctor


@dataclass(frozen=True)
class SmokeTest:
    id: str
    title: str
    status: str
    summary: str
    detail: str = ""
    action_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _pack_installed(pack_id: str, packs: list[dict[str, Any]]) -> bool:
    for pack in packs:
        if pack.get("id") == pack_id:
            return bool(pack.get("status", {}).get("installed"))
    return False


def _pack_details(pack_id: str, packs: list[dict[str, Any]]) -> dict[str, Any]:
    for pack in packs:
        if pack.get("id") == pack_id:
            return pack.get("status", {}).get("details", {})
    return {}


def smoke_test_report(home_dir: str | None = None) -> dict[str, Any]:
    """Return non-destructive app health checks."""
    storage = storage_status(home_dir=home_dir)
    packs = catalog_with_status().get("packs", [])
    comfy = detect_comfyui(home_dir=home_dir)
    local_ai = ollama_runtime_doctor(home_dir=home_dir)
    node = _node_runtime_status()
    ollama_port_open = _port_open(11434)
    tests = [
        SmokeTest(
            id="storage",
            title="Persistent storage",
            status="pass" if storage.ok and storage.configured_by != "default" else "warn",
            summary="NVH_HOME is ready" if storage.ok else "NVH_HOME needs attention",
            detail=str(storage.layout.home),
            action_id=None if storage.ok and storage.configured_by != "default" else "storage",
        ),
        SmokeTest(
            id="env-file",
            title="Session env file",
            status="pass" if Path(storage.env_file).exists() else "warn",
            summary="Shell activation file exists" if Path(storage.env_file).exists() else "Shell activation file is missing",
            detail=str(storage.env_file),
            action_id="repair-workspace",
        ),
        SmokeTest(
            id="ollama",
            title="Ollama local model server",
            status="pass" if local_ai.get("ready") else "warn",
            summary=str(local_ai.get("summary") or "Local AI runtime needs attention"),
            detail=str(local_ai.get("binary_error") or "http://127.0.0.1:11434"),
            action_id="rootless-ollama",
        ),
        SmokeTest(
            id="ollama-port",
            title="Ollama API port",
            status="pass" if ollama_port_open else "warn",
            summary="Ollama API is reachable" if ollama_port_open else "Ollama API is not reachable yet",
            detail="http://127.0.0.1:11434/api/tags",
            action_id="rootless-ollama",
        ),
        SmokeTest(
            id="node-runtime",
            title="Rootless WebUI runtime",
            status="pass" if node.get("ready") else "warn",
            summary="Node/npm runtime is ready" if node.get("ready") else "Node/npm runtime can be repaired rootlessly",
            detail=str(node.get("node_version") or node.get("node") or ""),
            action_id="repair-workspace",
        ),
        SmokeTest(
            id="agent-lab",
            title="Local Agent Lab",
            status="pass" if _pack_installed("agent-lab", packs) else "skip",
            summary="Local agent helper pack is installed" if _pack_installed("agent-lab", packs) else "Optional Local Agent Lab can be installed later",
            action_id="agent-lab",
        ),
        SmokeTest(
            id="claw-agents",
            title="Claw agent options",
            status="pass" if _pack_installed("openclaw-agent", packs) else "skip",
            summary=(
                "OpenClaw is installed"
                if _pack_installed("openclaw-agent", packs)
                else "Optional OpenClaw can be installed later; NemoClaw requires Docker/OpenShell access"
            ),
            detail=str(_pack_details("nemoclaw-sandbox", packs).get("blocked_reason", "")),
            action_id="claw-agents",
        ),
        SmokeTest(
            id="comfyui",
            title="ComfyUI workspace",
            status="pass" if comfy.get("running") else "warn" if comfy.get("installed") else "skip",
            summary="ComfyUI is running" if comfy.get("running") else "ComfyUI is installed but not running" if comfy.get("installed") else "ComfyUI is optional and not installed",
            detail=str(comfy.get("app_dir", "")),
            action_id="comfyui",
        ),
        SmokeTest(
            id="comfyui-examples",
            title="ComfyUI starter examples",
            status="pass" if comfy.get("examples_installed") else "warn" if comfy.get("installed") else "skip",
            summary="Starter examples are installed" if comfy.get("examples_installed") else "Starter examples can be repaired" if comfy.get("installed") else "Install ComfyUI first",
            detail=str(comfy.get("examples_dir", "")),
            action_id="comfyui-examples",
        ),
        SmokeTest(
            id="blender",
            title="Blender creative tools",
            status="pass" if _pack_installed("blender-creative", packs) else "skip",
            summary="Blender pack is installed" if _pack_installed("blender-creative", packs) else "Blender is optional and not installed",
            action_id="creative-tools",
        ),
        SmokeTest(
            id="godot",
            title="Godot game engine",
            status="pass" if _pack_installed("godot-engine", packs) else "skip",
            summary="Godot pack is installed" if _pack_installed("godot-engine", packs) else "Godot is optional and not installed",
            action_id="game-tools",
        ),
    ]
    failed = sum(1 for test in tests if test.status == "fail")
    warnings = sum(1 for test in tests if test.status == "warn")
    passed = sum(1 for test in tests if test.status == "pass")
    return {
        "summary": f"{passed} passed, {warnings} warning(s), {failed} failed",
        "ready": failed == 0,
        "passed": passed,
        "warnings": warnings,
        "failed": failed,
        "tests": [test.as_dict() for test in tests],
    }
