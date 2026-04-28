"""Local setup helper for student workstation onboarding.

This module is intentionally deterministic and offline. It gives the WebUI and
CLI a small "what should I do next?" brain without requiring a local LLM to be
installed first.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from nvh.integrations.catalog import catalog_status
from nvh.integrations.comfyui import detect_comfyui
from nvh.integrations.jobs import list_jobs
from nvh.integrations.receipts import receipt_summary, repair_plan
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


def _safe_receipt_summary() -> dict[str, Any]:
    try:
        return receipt_summary()
    except Exception as exc:
        return {"count": 0, "by_kind": {}, "unhealthy": 0, "root": None, "receipts": [], "error": str(exc)}


def _safe_catalog_status() -> dict[str, Any]:
    try:
        return catalog_status(refresh=False)
    except Exception as exc:
        return {"source": "unavailable", "error": str(exc)}


def _recent_failed_job() -> dict[str, Any] | None:
    try:
        jobs = list_jobs(limit=10)
    except Exception:
        return None
    for job in jobs:
        if job.get("status") in {"failed", "interrupted", "canceled"}:
            return job
    return None


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
        "receipts": _safe_receipt_summary(),
        "catalog": _safe_catalog_status(),
        "assistant": {
            "mode": "offline-deterministic",
            "can_read_jobs": True,
            "can_read_receipts": True,
            "can_refresh_catalog": True,
            "description": (
                "Local setup helper can explain next steps, inspect recent install state, "
                "and suggest rootless repair commands without requiring a cloud model."
            ),
        },
    }


def _commands_for_actions(actions: list[dict[str, Any]], *action_ids: str) -> list[str]:
    wanted = set(action_ids)
    commands = [
        action["command"] for action in actions
        if action.get("id") in wanted and action.get("command")
    ]
    return commands


def setup_assistant_reply(
    question: str,
    home_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Answer a setup question using local state and deterministic rules."""
    report = setup_helper_report(home_dir=home_dir)
    actions = report["actions"]
    q = question.strip().lower()
    receipts = report.get("receipts", {})
    failed_job = _recent_failed_job()
    commands: list[str] = []
    focus = "next-step"

    if not q:
        answer = "Ask about storage, ComfyUI, models, Blender, repair, or the next setup step."
    elif any(word in q for word in ["storage", "mount", "persistent", "home", "nvh_home"]):
        focus = "storage"
        commands = _commands_for_actions(actions, "storage") or [
            'nvh doctor --storage --home-dir "/path/on/mounted/volume/nvhive"',
        ]
        answer = (
            "Use the mounted file volume for NVH_HOME before large downloads. "
            f"Current storage source is {report['storage']['configured_by']} at "
            f"{report['storage']['layout']['home']}."
        )
    elif any(word in q for word in ["comfy", "image", "video", "workflow"]):
        focus = "comfyui"
        commands = _commands_for_actions(actions, "comfyui", "comfyui-examples") or [
            "nvh workstation --with-comfyui -y",
        ]
        answer = (
            "ComfyUI is managed as a rootless workspace under NVH_HOME. "
            "Install it from the wizard or run the command below; model weights stay explicit "
            "because many upstream downloads require license acceptance."
        )
    elif any(word in q for word in ["model", "llm", "ollama", "local ai"]):
        focus = "models"
        commands = _commands_for_actions(actions, "rootless-ollama", "starter-models") or [
            "nvh studio --install rootless-ollama -y",
            "nvh studio --install-models recommended -y",
        ]
        answer = (
            "Start with the rootless Ollama runtime, then download the recommended models "
            "that fit the detected GPU. The wizard keeps these under NVH_HOME/models."
        )
    elif any(word in q for word in ["blender", "creative", "game", "asset"]):
        focus = "creative"
        commands = _commands_for_actions(actions, "creative-tools") or [
            "nvh studio --install creative -y",
        ]
        answer = (
            "Creative tools are installed without sudo under NVH_HOME/apps and NVH_HOME/studio. "
            "The creative profile adds Blender plus game asset workspaces."
        )
    elif any(word in q for word in ["repair", "fix", "failed", "error", "broken"]):
        focus = "repair"
        if failed_job:
            answer = (
                f"The most recent problem I found is {failed_job['title']} with status "
                f"{failed_job['status']}: {failed_job.get('message', 'no message')}. "
                "Retry the matching wizard step after checking storage and network access."
            )
        elif receipts.get("unhealthy"):
            first = receipts.get("receipts", [{}])[0]
            try:
                commands = repair_plan(first["id"])["commands"]
            except Exception:
                commands = []
            answer = (
                f"I found {receipts['unhealthy']} receipt(s) with missing files or launchers. "
                "Use the repair command to rerun the rootless installer for that item."
            )
        else:
            answer = (
                "I do not see a failed recent install or unhealthy receipt. "
                "Run the wizard step again if you want to refresh an installed component."
            )
    else:
        commands = [action["command"] for action in actions[:3]]
        next_title = actions[0]["title"] if actions else "Open the setup wizard"
        answer = (
            f"Best next step: {next_title}. "
            f"{report['summary']}. Receipts tracked: {receipts.get('count', 0)}."
        )

    if not commands and actions:
        commands = [actions[0]["command"]]

    return {
        "question": question,
        "answer": answer,
        "focus": focus,
        "commands": commands,
        "observations": {
            "ready": report["ready"],
            "receipt_count": receipts.get("count", 0),
            "unhealthy_receipts": receipts.get("unhealthy", 0),
            "catalog_source": report.get("catalog", {}).get("source"),
            "recent_problem": failed_job,
        },
        "actions": actions[:5],
    }
