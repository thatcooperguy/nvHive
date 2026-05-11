"""Backend-owned nvWizard mission builds.

The setup UI should be able to start one durable job for a mission instead of
holding the dependency chain in the browser tab. This module turns the mission
cards into deterministic rootless plans and streams each install stage through
the existing persistent job runner.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from nvh.integrations.storage import ensure_storage, storage_status
from nvh.integrations.workspace_passport import rootless_policy_report

MISSION_TITLES = {
    "student": "AI Starter",
    "llm": "Local LLM Lab",
    "creator": "Graphics Creator Studio",
    "agent": "Agent Builder",
    "game": "Game Dev Lab",
    "music": "Music Producer Studio",
    "full": "Power User Workstation",
}

MISSION_PACK_GROUPS = {
    "student": ["rootless-ollama", "agent-lab", "nvidia-omni-agent"],
    "llm": ["rootless-ollama"],
    "creator": ["rootless-ollama", "creative", "comfy", "github-login-helper"],
    "agent": ["rootless-ollama", "agents", "claw"],
    "game": ["rootless-ollama", "game", "creative", "comfy"],
    "music": ["rootless-ollama", "music"],
    "full": ["all"],
}

COMFY_MISSIONS = {"creator", "game", "full"}


def _validate_profile(profile: str) -> str:
    normalized = (profile or "student").strip().lower()
    if normalized not in MISSION_TITLES:
        raise KeyError(f"Unknown nvWizard mission profile: {profile}")
    return normalized


def _installable_pack_ids(pack_ids: list[str]) -> list[str]:
    from nvh.integrations.studio_packs import catalog_with_status

    status_by_id = {
        pack["id"]: pack.get("status", {})
        for pack in catalog_with_status().get("packs", [])
    }
    result: list[str] = []
    for pack_id in pack_ids:
        details = status_by_id.get(pack_id, {}).get("details", {})
        if isinstance(details, dict) and details.get("installable") is False:
            continue
        result.append(pack_id)
    return result


def mission_profile_needs_comfy(profile: str) -> bool:
    return _validate_profile(profile) in COMFY_MISSIONS


def mission_profile_pack_ids(profile: str) -> list[str]:
    """Return installable pack ids for a mission profile."""
    from nvh.integrations.studio_packs import expand_pack_ids

    normalized = _validate_profile(profile)
    groups = MISSION_PACK_GROUPS[normalized]
    pack_ids = expand_pack_ids(groups)
    if normalized == "full":
        pack_ids = [
            pack_id for pack_id in pack_ids
            if pack_id not in {"llm-starter", "llm-coder-reasoner"}
        ]
    return _installable_pack_ids(pack_ids)


def mission_profile_model_ids(profile: str) -> list[str]:
    """Return local model ids for a mission profile using detected GPU fit."""
    from nvh.integrations.studio_packs import model_catalog_with_status

    normalized = _validate_profile(profile)
    models = model_catalog_with_status().get("models", [])
    recommended = [
        model["id"] for model in models
        if model.get("recommended")
    ]
    fit_or_recommended = [
        model["id"] for model in models
        if model.get("recommended") or model.get("fits_vram")
    ]

    if normalized == "agent":
        agent_models = [
            model["id"] for model in models
            if model.get("category") in {"code", "embedding"}
            and (model.get("recommended") or model.get("fits_vram"))
        ]
        return agent_models or recommended

    if normalized == "full":
        return fit_or_recommended or recommended

    return recommended


def mission_profile_example_ids(profile: str, *, vram_gb: int | None = None) -> list[str]:
    """Return ComfyUI example ids that fit the detected or supplied VRAM."""
    if not mission_profile_needs_comfy(profile):
        return []

    from nvh.integrations.comfyui import examples_as_dicts
    from nvh.integrations.studio_packs import model_catalog_with_status

    detected_vram = vram_gb
    if detected_vram is None:
        detected_vram = int(model_catalog_with_status().get("detected_vram_gb", 0) or 0)
    vram_limit = detected_vram or 12
    return [
        example["id"] for example in examples_as_dicts()
        if int(example.get("recommended_vram_gb", 0) or 0) <= vram_limit
    ]


def _estimated_disk_gb(pack_ids: list[str], model_ids: list[str]) -> float:
    from nvh.integrations.studio_packs import STUDIO_MODELS, STUDIO_PACKS

    pack_disk = sum(
        pack.estimated_disk_gb for pack in STUDIO_PACKS
        if pack.id in set(pack_ids)
    )
    model_disk = sum(
        model.estimated_disk_gb for model in STUDIO_MODELS
        if model.id in set(model_ids)
    )
    return round(pack_disk + model_disk, 1)


def mission_profile_plan(
    profile: str,
    *,
    torch_profile: str = "nvidia-cu130",
    min_free_gb: float = 200.0,
    home_dir: str | None = None,
) -> dict[str, Any]:
    """Return the concrete server-side mission build plan."""
    normalized = _validate_profile(profile)
    pack_ids = mission_profile_pack_ids(normalized)
    model_ids = mission_profile_model_ids(normalized)
    example_ids = mission_profile_example_ids(normalized)
    comfy_node_pack_ids = [pack_id for pack_id in pack_ids if pack_id == "comfyui-power-nodes"]
    first_pack_ids = (
        [pack_id for pack_id in pack_ids if pack_id != "comfyui-power-nodes"]
        if mission_profile_needs_comfy(normalized)
        else pack_ids
    )
    storage = storage_status(home_dir=home_dir, min_free_gb=min_free_gb).as_dict()
    stages: list[dict[str, Any]] = []
    if first_pack_ids:
        stages.append({"id": "studio-packs", "title": "Install rootless tools", "pack_ids": first_pack_ids})
    if mission_profile_needs_comfy(normalized):
        stages.append({"id": "comfyui", "title": "Install ComfyUI", "torch_profile": torch_profile})
        if comfy_node_pack_ids:
            stages.append({"id": "comfyui-nodes", "title": "Install ComfyUI nodes", "pack_ids": comfy_node_pack_ids})
        if example_ids:
            stages.append({"id": "comfyui-plan", "title": "Save ComfyUI model plan", "example_ids": example_ids})
    if model_ids:
        stages.append({"id": "models", "title": "Download local models", "model_ids": model_ids})

    return {
        "schema_version": 1,
        "profile": normalized,
        "title": MISSION_TITLES[normalized],
        "storage": storage,
        "rootless_safe": storage.get("ok") and storage.get("configured_by") != "default",
        "needs_comfyui": mission_profile_needs_comfy(normalized),
        "torch_profile": torch_profile,
        "pack_ids": pack_ids,
        "first_pack_ids": first_pack_ids,
        "comfy_node_pack_ids": comfy_node_pack_ids,
        "model_ids": model_ids,
        "example_ids": example_ids,
        "estimated_disk_gb": _estimated_disk_gb(pack_ids, model_ids),
        "stages": stages,
    }


def _stage_event(stage: str, event: dict[str, Any]) -> dict[str, Any]:
    child_event = str(event.get("event", "progress"))
    child_status = str(event.get("status", "running"))
    wrapped = {
        **event,
        "stage": stage,
        "child_event": child_event,
        "child_status": child_status,
    }
    if child_event == "complete" or child_status == "complete":
        wrapped["event"] = "stage-complete"
        wrapped["status"] = "running"
    return wrapped


def _failed(event: dict[str, Any]) -> bool:
    return event.get("event") == "error" or event.get("status") == "failed"


async def install_mission_profile(
    profile: str,
    *,
    torch_profile: str = "nvidia-cu130",
    force_update: bool = False,
    home_dir: str | None = None,
    min_free_gb: float = 200.0,
) -> AsyncIterator[dict[str, Any]]:
    """Install one mission as a single persistent background job."""
    from nvh.integrations.comfyui import install_comfyui, start_comfyui, write_model_plan
    from nvh.integrations.studio_packs import install_studio_models, install_studio_packs

    normalized = _validate_profile(profile)
    if home_dir:
        ensure_storage(home_dir, min_free_gb=min_free_gb, activate=True)

    policy = rootless_policy_report(home_dir=home_dir, min_free_gb=min_free_gb)
    if policy["status"] == "blocked":
        yield {
            "event": "error",
            "status": "failed",
            "message": policy["summary"],
            "policy": policy,
        }
        return

    plan = mission_profile_plan(
        normalized,
        torch_profile=torch_profile,
        min_free_gb=min_free_gb,
        home_dir=home_dir,
    )
    yield {
        "event": "plan",
        "status": "running",
        "message": f"Building {plan['title']} as one tracked rootless mission.",
        "profile": normalized,
        "plan": plan,
    }

    if plan["first_pack_ids"]:
        yield {
            "event": "step",
            "status": "running",
            "stage": "studio-packs",
            "message": "Installing rootless runtimes and mission tools.",
        }
        async for event in install_studio_packs(plan["first_pack_ids"], force_update=force_update):
            wrapped = _stage_event("studio-packs", event)
            yield wrapped
            if _failed(wrapped):
                return

    if plan["needs_comfyui"]:
        yield {
            "event": "step",
            "status": "running",
            "stage": "comfyui",
            "message": "Installing ComfyUI with the selected NVIDIA PyTorch profile.",
        }
        async for event in install_comfyui(torch_profile=torch_profile, force_update=force_update):
            wrapped = _stage_event("comfyui", event)
            yield wrapped
            if _failed(wrapped):
                return

        if plan["comfy_node_pack_ids"]:
            yield {
                "event": "step",
                "status": "running",
                "stage": "comfyui-nodes",
                "message": "Installing ComfyUI power nodes after the base app is ready.",
            }
            async for event in install_studio_packs(plan["comfy_node_pack_ids"], force_update=force_update):
                wrapped = _stage_event("comfyui-nodes", event)
                yield wrapped
                if _failed(wrapped):
                    return

        if plan["example_ids"]:
            plan_path = write_model_plan(plan["example_ids"])
            yield {
                "event": "stage-complete",
                "status": "running",
                "stage": "comfyui-plan",
                "message": "Saved ComfyUI starter workflow model plan.",
                "path": str(plan_path),
                "example_ids": plan["example_ids"],
            }

        yield {
            "event": "step",
            "status": "running",
            "stage": "comfyui-start",
            "message": "Starting ComfyUI on a free localhost port.",
        }
        try:
            status = await asyncio.to_thread(start_comfyui)
            yield {
                "event": "stage-complete",
                "status": "running",
                "stage": "comfyui-start",
                "message": (
                    f"ComfyUI is running at {status.get('url')}"
                    if status.get("ready") or status.get("running")
                    else "ComfyUI was started but is still warming up; check the ComfyUI log."
                ),
                "status_snapshot": status,
                "url": status.get("url"),
            }
        except Exception as exc:
            yield {
                "event": "service-warning",
                "status": "running",
                "stage": "comfyui-start",
                "message": f"ComfyUI installed, but auto-start needs attention: {exc}",
            }

    if plan["model_ids"]:
        yield {
            "event": "step",
            "status": "running",
            "stage": "models",
            "message": "Downloading the local model queue that fits this GPU profile.",
        }
        async for event in install_studio_models(plan["model_ids"], force_update=force_update):
            wrapped = _stage_event("models", event)
            yield wrapped
            if _failed(wrapped):
                return

    yield {
        "event": "complete",
        "status": "complete",
        "stage": "mission",
        "message": f"{plan['title']} mission build complete.",
        "profile": normalized,
        "plan": plan,
    }
