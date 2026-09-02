"""Safe rootless setup repair planning and execution."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from nvh.integrations.installs.comfyui import detect_comfyui, write_example_pack
from nvh.integrations.services.receipts import receipt_summary
from nvh.integrations.setup_catalog import load_setup_catalog
from nvh.integrations.workspace.storage import (
    ensure_storage,
    storage_layout,
    storage_status,
    write_env_file,
)

logger = logging.getLogger(__name__)


def _refresh_ollama_models() -> str:
    """Re-query the Ollama daemon for installed models (read-only).

    Returns a one-line summary the auto-repair pipeline can record. If Ollama
    is not running we report that — not an error, just "nothing to refresh".
    """
    try:
        import httpx

        resp = httpx.get("http://127.0.0.1:11434/api/tags", timeout=2.0)
        resp.raise_for_status()
        models = resp.json().get("models", []) or []
        return f"Refreshed local Ollama model list: {len(models)} model(s) installed."
    except Exception as exc:
        logger.debug("ollama-model-refresh: daemon unreachable (%s)", exc)
        return "Ollama daemon not reachable; skipped model refresh."


def _validate_config(config_dir: Path) -> str:
    """Parse the nvHive config.yaml without modifying it.

    Surfaces schema drift early — a corrupt config can stall the wizard, and
    catching it at reconnect-time means we can flag it before the user runs
    into a downstream failure.
    """
    config_path = Path(config_dir) / "config.yaml"
    if not config_path.exists():
        return "No config.yaml yet — wizard will create one on first install."
    try:
        import yaml

        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return "config.yaml exists but isn't a mapping — wizard will repair it on next install."
        version = data.get("version", "?")
        provider_keys = [k for k in ("providers", "advisors") if k in data]
        section = provider_keys[0] if provider_keys else "—"
        providers = data.get(section, {}) if section != "—" else {}
        configured = sum(1 for v in (providers or {}).values() if isinstance(v, dict) and v.get("enabled"))
        return f"config.yaml v{version} parsed; {configured} provider(s) enabled."
    except Exception as exc:
        logger.warning("config-validate: %s unreadable (%s)", config_path, exc)
        return f"config.yaml present but not parseable: {exc}"


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
    actions.append(_action(
        "ollama-model-refresh",
        "Refresh the local Ollama model list",
        status="queued",
        summary="Re-query the Ollama daemon for installed models so the picker doesn't show stale entries.",
        safe_to_auto_run=True,
        button_action_id="repair-workspace",
    ))
    actions.append(_action(
        "config-validate",
        "Validate the nvHive config",
        status="queued",
        summary=f"Parse {layout.config_dir / 'config.yaml'} to surface schema drift without modifying it.",
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
            elif action["id"] == "ollama-model-refresh":
                completed.append({**action, "result": _refresh_ollama_models()})
            elif action["id"] == "config-validate":
                completed.append({**action, "result": _validate_config(storage.layout.config_dir)})
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
