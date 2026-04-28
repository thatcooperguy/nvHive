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


@dataclass(frozen=True)
class SetupIssue:
    """One proactive setup finding the wizard should surface."""

    id: str
    title: str
    severity: str
    reason: str
    fix_action_id: str | None = None
    affected_item: str | None = None
    current_version: str | None = None
    available_version: str | None = None

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


def _safe_catalog_data() -> dict[str, Any]:
    try:
        from nvh.integrations.catalog import load_setup_catalog

        return load_setup_catalog(refresh=False).get("catalog", {})
    except Exception:
        return {}


def _safe_compatibility_report(home_dir: str | Path | None = None) -> dict[str, Any]:
    try:
        from nvh.integrations.compatibility import compatibility_report

        return compatibility_report(home_dir=home_dir)
    except Exception as exc:
        return {"summary": "Compatibility unavailable", "issue_count": 0, "apps": [], "error": str(exc)}


def _safe_boot_preflight(home_dir: str | Path | None = None) -> dict[str, Any]:
    try:
        from nvh.integrations.boot_preflight import boot_preflight_status

        return boot_preflight_status(home_dir=home_dir, run_if_missing=False)
    except Exception as exc:
        return {"summary": "Boot preflight unavailable", "changes": [], "agent_helper": {}, "error": str(exc)}


def _recent_failed_job() -> dict[str, Any] | None:
    try:
        jobs = list_jobs(limit=10)
    except Exception:
        return None
    for job in jobs:
        if job.get("status") in {"failed", "interrupted", "canceled"}:
            return job
    return None


def _action_for_job(job: dict[str, Any]) -> str:
    kind = job.get("kind")
    if kind == "comfyui-install":
        return "comfyui"
    if kind == "studio-model-install":
        return "starter-models"
    if kind == "studio-pack-install":
        return "studio-packs"
    return "storage"


def _catalog_entry_by_id(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for key in ("packs", "models", "comfyui_examples"):
        for item in catalog.get(key, []):
            item_id = item.get("id")
            if item_id:
                entries[str(item_id)] = item
    return entries


def _version_from_catalog(entry: dict[str, Any] | None) -> str | None:
    if not entry:
        return None
    value = entry.get("latest_version") or entry.get("version")
    return str(value) if value else None


def _looks_older(current: str | None, latest: str | None) -> bool:
    if not current or not latest or current == latest:
        return False
    try:
        from packaging.version import Version

        return Version(current) < Version(latest)
    except Exception:
        return current != latest


def setup_helper_report(home_dir: str | Path | None = None) -> dict[str, Any]:
    """Return a local setup diagnosis and ranked action list."""
    storage = storage_status(home_dir=home_dir, min_free_gb=20)
    runtime = runtime_status()
    packs = catalog_with_status()
    models = model_catalog_with_status()
    comfy = detect_comfyui()
    by_pack = _pack_by_id(packs)
    actions: list[SetupAction] = []
    issues: list[SetupIssue] = []

    if not storage.ok or storage.configured_by == "default":
        issues.append(SetupIssue(
            id="storage",
            title="Persistent storage is not ready",
            severity="required",
            reason="Large installs may be lost if NVH_HOME is still using the default or an unwritable path.",
            fix_action_id="storage",
            affected_item="NVH_HOME",
        ))
        actions.append(SetupAction(
            id="storage",
            title="Choose persistent NVH_HOME",
            priority=10,
            status="required",
            command='nvh doctor --storage --home-dir "/path/on/mounted/volume/nvhive"',
            reason="Large downloads should live on the mounted file volume that survives reconnects.",
        ))

    if runtime.strategy == "needs-runtime":
        issues.append(SetupIssue(
            id="runtime-fallback",
            title="Python runtime needs a fallback",
            severity="recommended",
            reason="This image does not appear to have a complete Python venv/pip path.",
            fix_action_id="runtime-fallback",
            affected_item="python-runtime-fallback",
        ))
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
        issues.append(SetupIssue(
            id="rootless-ollama",
            title="Local model runtime is missing",
            severity="recommended",
            reason="Local LLM downloads need a rootless Ollama runtime.",
            fix_action_id="rootless-ollama",
            affected_item="rootless-ollama",
        ))
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
        issues.append(SetupIssue(
            id="starter-models",
            title="Recommended local models are missing",
            severity="recommended",
            reason=f"{len(missing_models)} recommended model(s) are not installed yet.",
            fix_action_id="starter-models",
            affected_item="local-models",
        ))
        actions.append(SetupAction(
            id="starter-models",
            title="Download recommended local models",
            priority=40,
            status="recommended",
            command="nvh studio --install-models recommended -y",
            reason=f"{len(missing_models)} recommended model(s) are not installed yet.",
        ))

    if not comfy.get("installed"):
        issues.append(SetupIssue(
            id="comfyui",
            title="ComfyUI is not installed",
            severity="optional",
            reason="Visual image/video workflows are unavailable until ComfyUI is installed.",
            fix_action_id="comfyui",
            affected_item="comfyui",
        ))
        actions.append(SetupAction(
            id="comfyui",
            title="Install ComfyUI visual workspace",
            priority=50,
            status="optional",
            command="nvh workstation --with-comfyui -y",
            reason="ComfyUI enables local image/video workflows and nvHive starter examples.",
        ))
    elif not comfy.get("examples_installed"):
        issues.append(SetupIssue(
            id="comfyui-examples",
            title="ComfyUI starter examples need repair",
            severity="recommended",
            reason="ComfyUI is present, but the nvHive example manifest is missing.",
            fix_action_id="comfyui-examples",
            affected_item="comfyui",
        ))
        actions.append(SetupAction(
            id="comfyui-examples",
            title="Refresh ComfyUI examples",
            priority=55,
            status="recommended",
            command="nvh workstation --with-comfyui -y",
            reason="ComfyUI exists, but the nvHive examples manifest is missing.",
        ))

    openclaw_pack = by_pack.get("openclaw-agent", {})
    nemoclaw_pack = by_pack.get("nemoclaw-sandbox", {})
    openclaw_status = openclaw_pack.get("status", {})
    nemoclaw_status = nemoclaw_pack.get("status", {})
    nemoclaw_details = nemoclaw_status.get("details", {})
    if not openclaw_status.get("installed"):
        issues.append(SetupIssue(
            id="claw-agents",
            title="OpenClaw agent option is not installed",
            severity="optional",
            reason="OpenClaw gives students a self-hosted agent platform that can use local or cloud models.",
            fix_action_id="claw-agents",
            affected_item="openclaw-agent",
        ))
        actions.append(SetupAction(
            id="claw-agents",
            title="Install Claw agent options",
            priority=65,
            status="optional",
            command="nvh studio --install claw -y",
            reason="Adds OpenClaw, and adds NemoClaw too when Docker/OpenShell is usable without sudo.",
        ))
    elif nemoclaw_details.get("installable") and not nemoclaw_status.get("installed"):
        actions.append(SetupAction(
            id="claw-agents",
            title="Add NemoClaw sandbox option",
            priority=66,
            status="optional",
            command="nvh studio --install claw -y",
            reason="Docker is reachable, so nvHive can add the NVIDIA NemoClaw/OpenShell path.",
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

    music_pack = by_pack.get("ace-step-music", {})
    if not music_pack.get("status", {}).get("installed"):
        actions.append(SetupAction(
            id="music-tools",
            title="Install music producer tools",
            priority=72,
            status="optional",
            command="nvh studio --install music -y",
            reason="Adds ACE-Step music generation, audio AI tools, and a rootless DAW workspace.",
        ))

    receipts = _safe_receipt_summary()
    for receipt in receipts.get("receipts", []):
        health = receipt.get("health", {})
        if not health.get("healthy", True):
            action_id = f"repair-receipt:{receipt['id']}"
            missing = len(health.get("missing_launchers", [])) + len(health.get("missing_files", []))
            issues.append(SetupIssue(
                id=f"receipt:{receipt['id']}",
                title=f"{receipt.get('title', receipt['id'])} needs repair",
                severity="recommended",
                reason=f"{missing or 1} expected file or launcher path is missing.",
                fix_action_id=action_id,
                affected_item=receipt["id"],
            ))
            try:
                command = repair_plan(receipt["id"])["commands"][0]
            except Exception:
                command = f"nvh setup repair {receipt['id']}"
            actions.append(SetupAction(
                id=action_id,
                title=f"Repair {receipt.get('title', receipt['id'])}",
                priority=25,
                status="recommended",
                command=command,
                reason="A previous rootless install receipt has missing files or launchers.",
            ))

    catalog_data = _safe_catalog_data()
    catalog_entries = _catalog_entry_by_id(catalog_data)
    for receipt in receipts.get("receipts", []):
        current_version = receipt.get("version")
        latest_version = _version_from_catalog(catalog_entries.get(receipt.get("item_id")))
        if _looks_older(current_version, latest_version):
            action_id = f"repair-receipt:{receipt['id']}"
            issues.append(SetupIssue(
                id=f"outdated:{receipt['id']}",
                title=f"{receipt.get('title', receipt['id'])} has an update",
                severity="recommended",
                reason="A newer version is available in the setup catalog.",
                fix_action_id=action_id,
                affected_item=receipt["id"],
                current_version=str(current_version),
                available_version=str(latest_version),
            ))

    failed_job = _recent_failed_job()
    if failed_job:
        action_id = _action_for_job(failed_job)
        issues.append(SetupIssue(
            id=f"job:{failed_job['id']}",
            title=f"{failed_job.get('title', 'Install job')} needs attention",
            severity="recommended",
            reason=str(failed_job.get("message") or "A recent setup job did not finish."),
            fix_action_id=action_id,
            affected_item=failed_job.get("kind"),
        ))

    compatibility = _safe_compatibility_report(home_dir=home_dir)
    boot_preflight = _safe_boot_preflight(home_dir=home_dir)
    for app in compatibility.get("apps", []):
        if app.get("status") == "ready":
            continue
        action_id = app.get("recommended_action_id")
        severity = "required" if app.get("status") == "blocked" else app.get("severity", "recommended")
        issues.append(SetupIssue(
            id=f"compat:{app['id']}",
            title=f"{app.get('title', app['id'])} compatibility needs attention",
            severity=severity,
            reason=app.get("summary", "Compatibility check needs attention."),
            fix_action_id=action_id,
            affected_item=app["id"],
        ))

    boot_changes = boot_preflight.get("changes") or []
    if boot_changes:
        issues.append(SetupIssue(
            id="boot:vm-image-changed",
            title="Base VM image changed since the last nvHive boot",
            severity="recommended",
            reason=boot_preflight.get("summary", "Re-run the setup preflight before launching installed apps."),
            fix_action_id=None,
            affected_item="boot-preflight",
        ))

    actions.sort(key=lambda action: action.priority)
    issues.sort(key=lambda issue: {"required": 0, "recommended": 1, "optional": 2}.get(issue.severity, 3))
    ready = not any(action.status == "required" for action in actions)
    agent_helper = boot_preflight.get("agent_helper") or {}
    return {
        "ready": ready,
        "summary": (
            "Ready for downloads"
            if ready and not issues
            else f"{len(issues)} setup item(s) need attention"
        ),
        "storage": storage.as_dict(),
        "runtime": runtime.as_dict(),
        "comfyui": comfy,
        "model_recommendation_count": len(missing_models),
        "actions": [action.as_dict() for action in actions],
        "issues": [issue.as_dict() for issue in issues],
        "issue_count": len(issues),
        "receipts": receipts,
        "catalog": _safe_catalog_status(),
        "compatibility": {
            "summary": compatibility.get("summary"),
            "issue_count": compatibility.get("issue_count", 0),
            "blocked_count": compatibility.get("blocked_count", 0),
            "rootless_fixable_count": compatibility.get("rootless_fixable_count", 0),
            "recommended_torch_profile": compatibility.get("recommended_torch_profile"),
        },
        "boot_preflight": {
            "summary": boot_preflight.get("summary"),
            "checked_at": boot_preflight.get("checked_at"),
            "changed": bool(boot_preflight.get("changed")),
            "change_count": len(boot_changes),
            "agent_helper": agent_helper,
        },
        "assistant": {
            "mode": agent_helper.get("mode", "offline-deterministic"),
            "can_read_jobs": True,
            "can_read_receipts": True,
            "can_refresh_catalog": True,
            "description": (
                "nvWizard is the rootless setup questmaster: it checks the GPU forge, "
                "watches VM image drift, and suggests repairs without requiring a cloud model."
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


def _persona_wrap(answer: str) -> str:
    return f"nvWizard says: {answer}"


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
            f"{report['storage']['layout']['home']}. The wizard should guide this with a folder picker; "
            "the CLI command is only an advanced override."
        )
    elif any(word in q for word in ["comfy", "image", "video", "workflow"]):
        focus = "comfyui"
        commands = _commands_for_actions(actions, "comfyui", "comfyui-examples") or [
            "nvh workstation --with-comfyui -y",
        ]
        answer = (
            "ComfyUI is managed as a rootless workspace under NVH_HOME. "
            "Use the install button from the wizard; model weights stay explicit "
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
            "that fit the detected GPU. The wizard can run both steps and keeps files under NVH_HOME/models."
        )
    elif any(word in q for word in ["claw", "openclaw", "nemo", "nemoclaw", "desktop agent", "sandbox agent"]):
        focus = "claw-agents"
        commands = _commands_for_actions(actions, "claw-agents") or [
            "nvh studio --install claw -y",
        ]
        answer = (
            "OpenClaw is the simple rootless agent install. NemoClaw is the guarded NVIDIA/OpenShell "
            "path and only lights up when Docker works without sudo. In the wizard, use the Claw Agents "
            "pack; manual commands are just the advanced override."
        )
    elif any(word in q for word in ["blender", "creative", "game", "asset"]):
        focus = "creative"
        commands = _commands_for_actions(actions, "creative-tools") or [
            "nvh studio --install creative -y",
        ]
        answer = (
            "Creative tools are installed without sudo under NVH_HOME/apps and NVH_HOME/studio. "
            "Use the creative profile or repair button; manual commands are just overrides."
        )
    elif any(word in q for word in ["repair", "fix", "failed", "error", "broken"]):
        focus = "repair"
        if failed_job:
            answer = (
                f"The most recent problem I found is {failed_job['title']} with status "
                f"{failed_job['status']}: {failed_job.get('message', 'no message')}. "
                "Use the matching repair/install button after checking storage and network access."
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
        "answer": _persona_wrap(answer),
        "focus": focus,
        "commands": commands,
        "observations": {
            "ready": report["ready"],
            "issue_count": report.get("issue_count", 0),
            "receipt_count": receipts.get("count", 0),
            "unhealthy_receipts": receipts.get("unhealthy", 0),
            "catalog_source": report.get("catalog", {}).get("source"),
            "recent_problem": failed_job,
        },
        "actions": actions[:5],
    }
