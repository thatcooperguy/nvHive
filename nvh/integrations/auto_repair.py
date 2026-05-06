"""Safe rootless setup repair planning and execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nvh.integrations.catalog import load_setup_catalog
from nvh.integrations.comfyui import detect_comfyui, write_example_pack
from nvh.integrations.receipts import receipt_summary
from nvh.integrations.storage import ensure_storage, storage_layout, storage_status, write_env_file
from nvh.integrations.studio_packs import catalog_with_status


def _action(
    action_id: str,
    title: str,
    *,
    status: str,
    summary: str,
    safe_to_auto_run: bool,
    action_type: str = "repair",
    button_action_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": action_id,
        "title": title,
        "status": status,
        "summary": summary,
        "safe_to_auto_run": safe_to_auto_run,
        "action_type": action_type,
        "button_action_id": button_action_id or action_id,
    }


def auto_repair_plan(home_dir: str | Path | None = None) -> dict[str, Any]:
    """Return a queue of safe repairs plus explicit user actions."""
    storage = storage_status(home_dir=home_dir)
    layout = storage.layout
    comfy = detect_comfyui(home_dir=home_dir)
    receipts = receipt_summary(home_dir=home_dir)
    packs = catalog_with_status().get("packs", [])
    pack_by_id = {pack.get("id"): pack for pack in packs}
    agent_installed = bool(pack_by_id.get("agent-lab", {}).get("status", {}).get("installed"))
    actions: list[dict[str, Any]] = []
    storage_auto_safe = storage.ok and storage.configured_by != "default"

    actions.append(_action(
        "storage-env-file",
        "Rebuild shell environment file",
        status="queued" if storage_auto_safe else "needs-user" if storage.configured_by == "default" else "blocked",
        summary=f"Write activation exports to {layout.home / 'nvh-env.sh'}.",
        safe_to_auto_run=storage_auto_safe,
        button_action_id="repair-workspace",
    ))
    actions.append(_action(
        "catalog-cache",
        "Verify setup catalog fallback",
        status="queued",
        summary="Ensure the setup catalog can load from cache or the bundled fallback.",
        safe_to_auto_run=True,
        button_action_id="repair-workspace",
    ))
    if comfy.get("installed") and not comfy.get("examples_installed"):
        actions.append(_action(
            "comfyui-examples",
            "Repair ComfyUI starter examples",
            status="queued",
            summary="Rewrite the nvHive example manifest and README into the ComfyUI install.",
            safe_to_auto_run=True,
            button_action_id="repair-workspace",
        ))
    if receipts.get("unhealthy"):
        actions.append(_action(
            "receipts",
            "Review unhealthy receipts",
            status="needs-user",
            summary=f"{receipts['unhealthy']} install receipt(s) point at missing files or launchers.",
            safe_to_auto_run=False,
            button_action_id="repair-receipts",
        ))
    if not agent_installed:
        actions.append(_action(
            "agent-lab",
            "Install Local Agent Lab",
            status="needs-user",
            summary="Install the fuller local AI helper. This may download Python packages, so it asks first.",
            safe_to_auto_run=False,
            action_type="install",
            button_action_id="agent-lab",
        ))

    auto_count = sum(1 for action in actions if action["safe_to_auto_run"] and action["status"] == "queued")
    return {
        "summary": f"{auto_count} safe repair(s) can run automatically; downloads stay explicit.",
        "auto_count": auto_count,
        "needs_user_count": sum(1 for action in actions if action["status"] == "needs-user"),
        "actions": actions,
    }


def run_safe_repairs(home_dir: str | Path | None = None) -> dict[str, Any]:
    """Run only idempotent repairs that do not install large packages or models."""
    plan = auto_repair_plan(home_dir=home_dir)
    completed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    storage = storage_status(home_dir=home_dir)

    for action in plan["actions"]:
        if not action["safe_to_auto_run"] or action["status"] != "queued":
            skipped.append({**action, "reason": "Requires user approval or is blocked."})
            continue
        try:
            if action["id"] == "storage-env-file":
                ensure_storage(storage.layout.home, activate=False)
                env_file = write_env_file(storage_layout(storage.layout.home))
                completed.append({**action, "result": str(env_file)})
            elif action["id"] == "catalog-cache":
                loaded = load_setup_catalog(refresh=False)
                completed.append({**action, "result": loaded.get("source")})
            elif action["id"] == "comfyui-examples":
                examples_dir = write_example_pack(storage.layout.comfyui_dir)
                completed.append({**action, "result": str(examples_dir)})
            else:
                skipped.append({**action, "reason": "No safe repair handler."})
        except Exception as exc:
            errors.append({**action, "error": str(exc)})

    return {
        "summary": f"{len(completed)} safe repair(s) completed, {len(skipped)} skipped, {len(errors)} error(s).",
        "completed": completed,
        "skipped": skipped,
        "errors": errors,
        "plan": plan,
    }
