"""Local setup helper for student workstation onboarding.

This module is intentionally deterministic and offline. It gives the WebUI and
CLI a small "what should I do next?" brain without requiring a local LLM to be
installed first.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from nvh.integrations.comfyui import detect_comfyui
from nvh.integrations.runtime import runtime_status
from nvh.integrations.storage import storage_status
from nvh.integrations.studio_packs import catalog_with_status, model_catalog_with_status


@dataclass(frozen=True)
class SetupAction:
    """One recommended next action in the student setup journey."""

    id: str
    title: str
    priority: int
    status: str
    command: str
    reason: str
    can_run_without_root: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _pack_by_id(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {pack["id"]: pack for pack in catalog.get("packs", [])}


def setup_helper_report(home_dir: str | Path | None = None) -> dict[str, Any]:
    """Return a local setup diagnosis and ranked action list."""
    storage = storage_status(home_dir=home_dir, min_free_gb=20)
    runtime = runtime_status()
    packs = catalog_with_status()
    models = model_catalog_with_status()
    comfy = detect_comfyui()
    by_pack = _pack_by_id(packs)
    actions: list[SetupAction] = []

    if not storage.ok or storage.configured_by == "default":
        actions.append(SetupAction(
            id="storage",
            title="Choose persistent NVH_HOME",
            priority=10,
            status="required",
            command='nvh doctor --storage --home-dir "/path/on/mounted/volume/nvhive"',
            reason="Large downloads should live on the mounted file volume that survives reconnects.",
        ))

    if runtime.strategy == "needs-runtime":
        actions.append(SetupAction(
            id="runtime-fallback",
            title="Install optional runtime fallback",
            priority=20,
            status="recommended",
            command="nvh studio --install python-runtime-fallback -y",
            reason="Python venv or pip is incomplete on this image; micromamba can rescue rootless installs.",
        ))

    ollama_pack = by_pack.get("rootless-ollama", {})
    if not ollama_pack.get("status", {}).get("installed"):
        actions.append(SetupAction(
            id="rootless-ollama",
            title="Install local model runtime",
            priority=30,
            status="recommended",
            command="nvh studio --install rootless-ollama -y",
            reason="Local chat models need an Ollama runtime. nvHive installs it without sudo.",
        ))

    missing_models = [
        model["id"] for model in models.get("models", [])
        if model.get("recommended") and not model.get("installed")
    ]
    if missing_models:
        actions.append(SetupAction(
            id="starter-models",
            title="Download recommended local models",
            priority=40,
            status="recommended",
            command="nvh studio --install-models recommended -y",
            reason=f"{len(missing_models)} recommended model(s) are not installed yet.",
        ))

    if not comfy.get("installed"):
        actions.append(SetupAction(
            id="comfyui",
            title="Install ComfyUI visual workspace",
            priority=50,
            status="optional",
            command="nvh workstation --with-comfyui -y",
            reason="ComfyUI enables local image/video workflows and nvHive starter examples.",
        ))
    elif not comfy.get("examples_installed"):
        actions.append(SetupAction(
            id="comfyui-examples",
            title="Refresh ComfyUI examples",
            priority=55,
            status="recommended",
            command="nvh workstation --with-comfyui -y",
            reason="ComfyUI exists, but the nvHive examples manifest is missing.",
        ))

    creative_pack = by_pack.get("blender-creative", {})
    if not creative_pack.get("status", {}).get("installed"):
        actions.append(SetupAction(
            id="creative-tools",
            title="Install creative tools",
            priority=70,
            status="optional",
            command="nvh studio --install creative -y",
            reason="Adds Blender LTS and asset workspaces for creative students.",
        ))

    actions.sort(key=lambda action: action.priority)
    ready = not any(action.status == "required" for action in actions)
    return {
        "ready": ready,
        "summary": "Ready for downloads" if ready else "Persistent storage needs attention",
        "storage": storage.as_dict(),
        "runtime": runtime.as_dict(),
        "comfyui": comfy,
        "model_recommendation_count": len(missing_models),
        "actions": [action.as_dict() for action in actions],
    }
