"""Local setup helper for student workstation onboarding.

This module is intentionally deterministic and offline. It gives the WebUI and
CLI a small "what should I do next?" brain without requiring a local LLM to be
installed first.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from nvh.integrations.catalog import catalog_status
from nvh.integrations.comfyui import detect_comfyui
from nvh.integrations.jobs import list_jobs
from nvh.integrations.local_chat import local_chat_smoke_status
from nvh.integrations.receipts import receipt_summary, repair_plan
from nvh.integrations.runtime import runtime_status
from nvh.integrations.service_registry import (
    list_service_statuses,
    service_for_question,
    service_status,
)
from nvh.integrations.storage import storage_status
from nvh.integrations.studio_packs import catalog_with_status, model_catalog_with_status
from nvh.integrations.troubleshooter import analyze_setup_failure

logger = logging.getLogger(__name__)

OFFICIAL_REPO_URL = "https://github.com/thatcooperguy/nvHive"
OFFICIAL_README_URL = "https://github.com/thatcooperguy/nvHive/blob/main/README.md"
OFFICIAL_PYPI_URL = "https://pypi.org/project/nvhive/"
DEFAULT_SETUP_MIN_FREE_GB = 200
OPTIONAL_COMPATIBILITY_IDS = {
    "agent-lab",
    "claw-agents",
    "blender-creative",
    "game-dev-lab",
    "music-producer-lab",
}

PRODUCT_CONTEXT_FALLBACK = """You are AI Wizard, the local setup and repair guide inside nvHive.

nvHive is a rootless NVIDIA AI lab for students, creators, agents, ComfyUI, and
local models. It turns a fresh Linux cloud GPU desktop into an AI workstation
without sudo by finding persistent storage, recommending GPU-fit models,
installing workload missions, tracking jobs/receipts, and running safe rootless
repairs.

Personality: calm mission-control mentor, lightly playful, practical, and
confidence-building. Translate scary logs into plain language, name the safest
button, and keep jokes brief. Official repo: https://github.com/thatcooperguy/nvHive
"""


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


def nvwizard_system_prompt() -> str:
    """Return packaged product grounding for deterministic and LLM-backed helpers."""
    try:
        return resources.files("nvh.prompts").joinpath("nvwizard_system.md").read_text(
            encoding="utf-8",
        ).strip()
    except Exception as exc:
        logger.warning("nvwizard_system.md unreadable, using built-in fallback: %s", exc)
        return PRODUCT_CONTEXT_FALLBACK.strip()


def _assistant_metadata(agent_helper: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": agent_helper.get("mode", "offline-deterministic"),
        "product": "nvHive / AI Wizard",
        "available_immediately": True,
        "requires_local_model": False,
        "can_read_jobs": True,
        "can_read_receipts": True,
        "can_refresh_catalog": True,
        "can_use_local_state": True,
        "can_use_internet_when_available": True,
        "official_repo_url": OFFICIAL_REPO_URL,
        "readme_url": OFFICIAL_README_URL,
        "pypi_url": OFFICIAL_PYPI_URL,
        "grounding_sources": [
            "packaged AI Wizard product brief",
            "local setup helper report",
            "boot preflight state",
            "install job logs",
            "install receipts",
            "setup catalog",
            "Workspace Passport",
            OFFICIAL_README_URL,
        ],
        "system_prompt": nvwizard_system_prompt(),
        "description": (
            "AI Wizard is the rootless setup guide for nvHive: it understands the "
            "product, checks the Linux GPU workstation, watches VM image drift, "
            "and suggests safe repairs without requiring a cloud model. Its voice "
            "is calm, lightly playful, and focused on getting the user unstuck."
        ),
    }


def _pack_by_id(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {pack["id"]: pack for pack in catalog.get("packs", [])}


def _safe_receipt_summary(home_dir: str | Path | None = None) -> dict[str, Any]:
    try:
        return receipt_summary(home_dir=home_dir)
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
    except Exception as exc:
        logger.warning("load_setup_catalog failed, returning empty catalog: %s", exc)
        return {}


def _safe_compatibility_report(
    home_dir: str | Path | None = None,
    *,
    model_status: dict[str, Any] | None = None,
    pack_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        from nvh.integrations.compatibility import compatibility_report

        return compatibility_report(
            home_dir=home_dir,
            model_status=model_status,
            pack_status=pack_status,
        )
    except Exception as exc:
        return {"summary": "Compatibility unavailable", "issue_count": 0, "apps": [], "error": str(exc)}


def _safe_boot_preflight(home_dir: str | Path | None = None) -> dict[str, Any]:
    try:
        from nvh.integrations.boot_preflight import boot_preflight_status

        return boot_preflight_status(home_dir=home_dir, run_if_missing=False)
    except Exception as exc:
        return {"summary": "Boot preflight unavailable", "changes": [], "agent_helper": {}, "error": str(exc)}


def _recent_failed_job(home_dir: str | Path | None = None) -> dict[str, Any] | None:
    try:
        jobs = list_jobs(limit=10, home_dir=home_dir)
    except Exception as exc:
        logger.debug("list_jobs failed in _recent_failed_job: %s", exc)
        return None
    for job in jobs:
        if job.get("status") in {"failed", "interrupted", "canceled"}:
            return job
    return None


def _short_text(value: Any, *, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[:limit - 3]}..."


def _question_wants_debug(q: str) -> bool:
    if ("nvidia-smi" in q or "without sudo" in q) and not any(
        word in q for word in ["log", "logs", "traceback", "failed job", "exec format"]
    ):
        return False
    return any(
        phrase in q
        for phrase in [
            "debug",
            "diagnose",
            "diagnosis",
            "what broke",
            "why did",
            "nothing happens",
            "error",
            "failed",
            "failure",
            "broken",
            "issue",
            "issues",
            "log",
            "logs",
            "traceback",
            "exec format",
            "read local",
        ]
    )


def _safe_diagnostics_report(home_dir: str | Path | None = None) -> dict[str, Any]:
    try:
        from nvh.integrations.diagnostics import diagnostics_report

        return diagnostics_report(home_dir=home_dir, include_logs=True, log_lines=80)
    except Exception as exc:
        return {"report_id": None, "checks": {}, "logs": {"recent": []}, "error": str(exc)}


def _event_text(event: Any) -> str:
    if isinstance(event, dict):
        for key in ("message", "error", "summary", "command", "stage"):
            if event.get(key):
                return str(event[key])
    return str(event)


def _diagnostic_job_findings(
    diagnostics: dict[str, Any],
    failed_job: dict[str, Any] | None,
) -> list[str]:
    findings: list[str] = []
    jobs_check = diagnostics.get("checks", {}).get("jobs", {})
    jobs_data = jobs_check.get("data") if jobs_check.get("ok") else {}
    for tail in (jobs_data or {}).get("failed_event_tails", [])[:3]:
        title = tail.get("kind") or tail.get("job_id") or "setup job"
        status = tail.get("status") or "failed"
        message = tail.get("message") or "no message"
        events = tail.get("events") or []
        event_hint = ""
        for event in reversed(events):
            text = _short_text(_event_text(event), limit=180)
            if text:
                event_hint = f" Last event: {text}"
                break
        findings.append(_short_text(f"{title} {status}: {message}.{event_hint}", limit=260))

    if not findings and failed_job:
        findings.append(
            _short_text(
                f"{failed_job.get('title', 'setup job')} {failed_job.get('status', 'failed')}: "
                f"{failed_job.get('message', 'no message')}",
                limit=260,
            )
        )
    return findings


def _diagnostic_log_highlights(diagnostics: dict[str, Any]) -> list[str]:
    highlights: list[str] = []
    for entry in diagnostics.get("logs", {}).get("recent", []):
        path = Path(str(entry.get("path", "log"))).name
        for line in entry.get("lines", []):
            text = _short_text(line, limit=220)
            if text:
                highlights.append(f"{path}: {text}")
            if len(highlights) >= 4:
                return highlights
    return highlights


def _debug_repair_note(findings: list[str], log_highlights: list[str]) -> str:
    joined = " ".join([*findings, *log_highlights]).lower()
    if "exec format" in joined and "ollama" in joined:
        return (
            "This points at a bad or wrong-architecture Ollama binary. Use Install Runtime "
            "or Fix Issues so nvHive replaces the rootless Ollama binary under NVH_HOME."
        )
    if "node" in joined or "npm" in joined or "fnm" in joined:
        return (
            "This points at the WebUI Node/npm bootstrap. Retry WebUI launch after the "
            "rootless Node install finishes; nvHive keeps Node under the user workspace."
        )
    if "storage" in joined or "nvh_home" in joined or "volume" in joined:
        return (
            "This points at persistent storage or NVH_HOME. Use the storage repair action "
            "so installs stay on the block-backed home volume, not the read-only OS image."
        )
    return "Use the next recommended repair/install button; no sudo or OS package change should be needed."


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
    except Exception as exc:
        logger.debug("Version compare %s vs %s failed: %s", current, latest, exc)
        return current != latest


def setup_helper_report(home_dir: str | Path | None = None) -> dict[str, Any]:
    """Return a local setup diagnosis and ranked action list."""
    storage = storage_status(home_dir=home_dir, min_free_gb=DEFAULT_SETUP_MIN_FREE_GB)
    runtime = runtime_status()
    packs = catalog_with_status()
    models = model_catalog_with_status()
    comfy = detect_comfyui(home_dir=home_dir, check_http=False)
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

    installed_targets = list(models.get("installed_targets", []))
    if not ollama_pack.get("status", {}).get("installed"):
        local_chat = {
            "ready": False,
            "status": "missing-runtime",
            "summary": "The offline setup guide is ready, but local model chat needs the rootless Ollama runtime.",
            "provider": "ollama",
            "model": None,
            "output_chars": 0,
            "latency_ms": None,
            "error": None,
            "next_action_id": "rootless-ollama",
            "checked_at": None,
            "cached": False,
            "rootless": True,
        }
    elif missing_models:
        local_chat = {
            "ready": False,
            "status": "missing-models",
            "summary": f"The offline setup guide is ready, but local model chat needs {len(missing_models)} recommended model(s).",
            "provider": "ollama",
            "model": None,
            "output_chars": 0,
            "latency_ms": None,
            "error": None,
            "next_action_id": "starter-models",
            "checked_at": None,
            "cached": False,
            "rootless": True,
        }
    elif not models.get("ollama_running"):
        local_chat = {
            "ready": False,
            "status": "runtime-offline",
            "summary": "Ollama is installed and models are present, but the local model server is not reachable.",
            "provider": "ollama",
            "model": None,
            "output_chars": 0,
            "latency_ms": None,
            "error": None,
            "next_action_id": "rootless-ollama",
            "checked_at": None,
            "cached": False,
            "rootless": True,
        }
    elif installed_targets:
        local_chat = local_chat_smoke_status(home_dir=home_dir, max_age_s=45, timeout_s=20.0)
    else:
        local_chat = {
            "ready": False,
            "status": "no-models",
            "summary": "The offline setup guide is ready, but no local chat model is installed yet.",
            "provider": "ollama",
            "model": None,
            "output_chars": 0,
            "latency_ms": None,
            "error": None,
            "next_action_id": "starter-models",
            "checked_at": None,
            "cached": False,
            "rootless": True,
        }

    if (
        not local_chat.get("ready")
        and local_chat.get("status") not in {"missing-runtime", "missing-models"}
    ):
        next_action = str(local_chat.get("next_action_id") or "rootless-ollama")
        issues.append(SetupIssue(
            id="local-chat-smoke",
            title="Local chat response test needs attention",
            severity="recommended",
            reason=str(local_chat.get("summary") or "A local model did not return text yet."),
            fix_action_id=next_action,
            affected_item="local-chat",
        ))
        if not any(action.id == next_action for action in actions):
            if next_action == "starter-models":
                actions.append(SetupAction(
                    id="starter-models",
                    title="Download recommended local models",
                    priority=40,
                    status="recommended",
                    command="nvh studio --install-models recommended -y",
                    reason=str(local_chat.get("summary") or "Local chat needs a working installed model."),
                ))
            else:
                actions.append(SetupAction(
                    id="rootless-ollama",
                    title="Repair local model runtime",
                    priority=30,
                    status="recommended",
                    command="nvh studio --install rootless-ollama -y",
                    reason=str(local_chat.get("summary") or "Local chat needs a reachable Ollama runtime."),
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

    music_pack_ids = ("ace-step-music", "music-producer-lab", "music-daw-helper")
    music_missing = [
        pack_id
        for pack_id in music_pack_ids
        if not by_pack.get(pack_id, {}).get("status", {}).get("installed")
    ]
    if music_missing:
        actions.append(SetupAction(
            id="music-tools",
            title="Install music producer tools",
            priority=72,
            status="optional",
            command="nvh studio --install music -y",
            reason=f"Adds ACE-Step music generation, audio AI tools, and a rootless DAW workspace. Missing: {', '.join(music_missing)}.",
        ))

    receipts = _safe_receipt_summary(home_dir=home_dir)
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
                command = repair_plan(receipt["id"], home_dir=home_dir)["commands"][0]
            except Exception as exc:
                logger.debug("repair_plan(%s) failed, using generic command: %s", receipt["id"], exc)
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

    failed_job = _recent_failed_job(home_dir=home_dir)
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

    compatibility = _safe_compatibility_report(
        home_dir=home_dir,
        model_status=models,
        pack_status=packs,
    )
    boot_preflight = _safe_boot_preflight(home_dir=home_dir)
    for app in compatibility.get("apps", []):
        if app.get("status") == "ready":
            continue
        app_id = str(app.get("id", ""))
        severity = "required" if app.get("status") == "blocked" else app.get("severity", "recommended")
        if app_id in OPTIONAL_COMPATIBILITY_IDS and severity != "required":
            continue
        action_id = app.get("recommended_action_id")
        issues.append(SetupIssue(
            id=f"compat:{app_id}",
            title=f"{app.get('title', app_id)} compatibility needs attention",
            severity=severity,
            reason=app.get("summary", "Compatibility check needs attention."),
            fix_action_id=action_id,
            affected_item=app_id,
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
    core_issues = [issue for issue in issues if issue.severity != "optional"]
    optional_issues = [issue for issue in issues if issue.severity == "optional"]
    core_actions = [action for action in actions if action.status != "optional"]
    optional_actions = [action for action in actions if action.status == "optional"]
    ready = not any(action.status == "required" for action in actions)
    agent_helper = boot_preflight.get("agent_helper") or {}
    if core_issues:
        first_issue = core_issues[0]
        more = len(core_issues) - 1
        summary = (
            f"Core setup needs attention: {first_issue.title}"
            f"{f' (+{more} more)' if more else ''}."
        )
    elif optional_issues or optional_actions:
        if local_chat.get("ready"):
            summary = "Core local AI chat is verified; optional add-ons are available"
        else:
            summary = str(local_chat.get("summary") or "Core setup guide is ready; local chat is still being verified")
    else:
        summary = str(local_chat.get("summary") or "Ready for downloads")
    return {
        "ready": ready,
        "summary": summary,
        "storage": storage.as_dict(),
        "runtime": runtime.as_dict(),
        "comfyui": comfy,
        "model_recommendation_count": len(missing_models),
        "local_chat": local_chat,
        "actions": [action.as_dict() for action in actions],
        "issues": [issue.as_dict() for issue in issues],
        "issue_count": len(issues),
        "core_issue_count": len(core_issues),
        "optional_issue_count": len(optional_issues),
        "core_action_count": len(core_actions),
        "optional_action_count": len(optional_actions),
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
        "assistant": _assistant_metadata(agent_helper),
    }


def _commands_for_actions(actions: list[dict[str, Any]], *action_ids: str) -> list[str]:
    wanted = set(action_ids)
    commands = [
        action["command"] for action in actions
        if action.get("id") in wanted and action.get("command")
    ]
    return commands


def _persona_wrap(answer: str) -> str:
    return answer


def _core_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [action for action in actions if action.get("status") != "optional"]


def _reply_actions_for_focus(
    actions: list[dict[str, Any]],
    focus: str,
) -> list[dict[str, Any]]:
    optional_focuses = {
        "comfyui",
        "creator",
        "game",
        "music",
        "agent",
        "mission-choice",
    }
    if focus in optional_focuses:
        return actions[:5]
    return _core_actions(actions)[:5]


def _live_comfyui_status(home_dir: str | Path | None, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return detect_comfyui(home_dir=home_dir, check_http=True)
    except Exception as exc:
        status = dict(fallback or {})
        status.setdefault("installed", False)
        status.setdefault("running", False)
        status.setdefault("ready", False)
        status["error"] = str(exc)
        status.setdefault("service_status", "unknown")
        status.setdefault("next_action", "check_failed")
        return status


def _comfyui_answer_parts(comfy: dict[str, Any]) -> tuple[str, list[str], list[dict[str, Any]]]:
    installed = bool(comfy.get("installed"))
    running = bool(comfy.get("running") or comfy.get("ready"))
    url = str(comfy.get("url") or "").strip()
    port = comfy.get("port")
    status = str(comfy.get("service_status") or comfy.get("next_action") or "unknown")
    app_dir = str(comfy.get("app_dir") or comfy.get("install_root") or "NVH_HOME/comfyui/ComfyUI")
    log_tail = [
        _short_text(line, limit=220)
        for line in (comfy.get("log_tail") or [])
        if str(line or "").strip()
    ][-4:]
    action: SetupAction
    commands: list[str] = []

    if running and url:
        answer = (
            f"ComfyUI is installed and running at {url}. "
            f"nvHive detected the service on localhost port {port or 'auto'} with status {status}. "
            "Use Open ComfyUI or the ComfyUI mission page; outputs and workflows stay under NVH_HOME."
        )
        action = SetupAction(
            id="start-comfyui",
            title="Open ComfyUI",
            priority=5,
            status="recommended",
            command="",
            reason=f"ComfyUI is running at {url}.",
        )
    elif installed:
        conflict = comfy.get("port_conflict")
        port_note = (
            f" Port {comfy.get('requested_port') or 8188} looks busy, so nvHive will pick a free localhost port."
            if conflict
            else f" It will normally start on localhost port {port or 8188} unless that port is busy."
        )
        answer = (
            f"ComfyUI is installed at {app_dir}, but it is not running yet. "
            f"Current service state is {status}.{port_note} Press Start ComfyUI; after it starts, "
            "nvHive should show the exact URL and open it for you."
        )
        action = SetupAction(
            id="start-comfyui",
            title="Start ComfyUI",
            priority=5,
            status="recommended",
            command="",
            reason="ComfyUI is installed but the local service is not running.",
        )
    else:
        answer = (
            "ComfyUI is not installed in this nvHive workspace yet. Install Graphics Creator Studio "
            "or press Install ComfyUI; nvHive will keep the app, examples, logs, and outputs under NVH_HOME."
        )
        commands = ["nvh workstation --with-comfyui -y"]
        action = SetupAction(
            id="comfyui",
            title="Install ComfyUI",
            priority=5,
            status="recommended",
            command="nvh workstation --with-comfyui -y",
            reason="ComfyUI is missing from the persistent rootless workspace.",
        )

    if comfy.get("error"):
        answer += f" Last detection error: {_short_text(comfy['error'], limit=180)}."
    if log_tail:
        answer += f" Latest ComfyUI log clue: {log_tail[-1]}"

    return answer, commands, [action.as_dict()]


def _action_from_service(service: dict[str, Any]) -> dict[str, Any] | None:
    action_id = service.get("next_action_id")
    if not action_id:
        return None
    return SetupAction(
        id=str(action_id),
        title=str(service.get("next_action_label") or "Fix Service"),
        priority=5,
        status="recommended",
        command=str(service.get("command") or ""),
        reason=str(service.get("summary") or "Service needs attention."),
    ).as_dict()


def _service_answer_parts(service: dict[str, Any]) -> tuple[str, list[str], list[dict[str, Any]], list[str], list[str]]:
    name = str(service.get("name") or service.get("id") or "service")
    installed = bool(service.get("installed"))
    running = bool(service.get("running"))
    ready = bool(service.get("ready"))
    status = str(service.get("status") or "unknown")
    summary = str(service.get("summary") or "")
    url = str(service.get("url") or "").strip()
    port = service.get("port")
    path = str(service.get("install_path") or "").strip()
    log_path = str(service.get("log_path") or "").strip()
    log_tail = [
        f"{Path(log_path).name if log_path else name}: {_short_text(line, limit=220)}"
        for line in (service.get("log_tail") or [])[-4:]
        if str(line or "").strip()
    ]
    if ready and url:
        answer = f"{name} is installed and running at {url}. {summary}"
    elif running:
        answer = f"{name} is running on localhost port {port or 'auto'}, but readiness is {status}. {summary}"
    elif installed:
        answer = f"{name} is installed at {path or 'the nvHive workspace'}, but it is not running yet. {summary}"
    else:
        answer = f"{name} is not installed yet. {summary}"
    if path and "installed at" not in answer:
        answer += f" Install path: {path}."
    if service.get("next_action_label"):
        answer += f" Next button: {service['next_action_label']}."
    if log_tail:
        answer += f" Latest log clue: {log_tail[-1]}"
    action = _action_from_service(service)
    commands = [str(service.get("command"))] if service.get("command") and action else []
    debug_findings = [
        f"{name} status: {status}",
        f"{name} installed: {installed}; running: {running}; ready: {ready}",
    ]
    if url:
        debug_findings.append(f"{name} URL: {url}")
    if port:
        debug_findings.append(f"{name} port: {port}")
    if log_path:
        debug_findings.append(f"{name} log: {log_path}")
    return answer, commands, [action] if action else [], debug_findings, log_tail


def _service_summary_answer(registry: dict[str, Any]) -> tuple[str, list[str]]:
    services = registry.get("services", [])
    not_ready = [service for service in services if not service.get("ready")]
    running = [service for service in services if service.get("running")]
    top = ", ".join(
        f"{service.get('name')}: {service.get('status')}"
        for service in services[:6]
    )
    answer = (
        f"I checked {registry.get('service_count', len(services))} rootless service(s): "
        f"{registry.get('ready_count', 0)} ready, {registry.get('running_count', len(running))} running. "
        f"Current map: {top}."
    )
    if not_ready:
        first = not_ready[0]
        answer += (
            f" First service needing attention: {first.get('name')} "
            f"({first.get('status')}). {first.get('summary')}"
        )
    else:
        answer += " All registered services look ready."
    findings = [
        f"{service.get('name')}: installed={service.get('installed')} running={service.get('running')} ready={service.get('ready')} status={service.get('status')}"
        for service in services
    ]
    return answer, findings


def setup_assistant_reply(
    question: str,
    home_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Answer a setup question using local state and deterministic rules."""
    report = setup_helper_report(home_dir=home_dir)
    actions = report["actions"]
    core_actions = _core_actions(actions)
    q = question.strip().lower()
    receipts = report.get("receipts", {})
    failed_job = _recent_failed_job(home_dir=home_dir)
    commands: list[str] = []
    focus = "next-step"
    suppress_command_fallback = False
    diagnostics_report_id: str | None = None
    debug_findings: list[str] = []
    log_highlights: list[str] = []
    troubleshooting: dict[str, Any] | None = None
    local_chat = report.get("local_chat") or {}
    extra_reply_actions: list[dict[str, Any]] = []
    extra_grounding_sources: list[str] = []

    if not q:
        answer = (
            "I can help right now, even while the local model is still downloading. "
            "Ask about what you want to build, storage, ComfyUI, models, Blender, "
            "music, game tools, agents, repair, the latest log clue, or the next setup step."
        )
    elif any(phrase in q for phrase in ["which mission", "what mission", "what should i pick", "what should i build", "start with"]):
        focus = "mission-choice"
        suppress_command_fallback = True
        answer = (
            "Pick AI Starter first if you want the safest first win: local chat, "
            "starter models, coding help, and a path into agents. Pick Graphics "
            "Creator Studio for ComfyUI and Blender, Game Dev Lab for Godot and "
            "engine helpers, or Music Producer Studio for audio generation, stems, "
            "and transcription. The wizard will still check storage, GPU, Python, "
            "and model fit before heavy downloads."
        )
    elif any(
        phrase in q
        for phrase in [
            "what is nv",
            "what are you",
            "what can you do",
            "product",
            "readme",
            "github",
            "repo",
            "repository",
            "internet",
            "deep dive",
            "source code",
        ]
    ):
        focus = "product"
        suppress_command_fallback = True
        answer = (
            "nvHive is a rootless NVIDIA AI lab for students and creators. "
            "AI Wizard is its setup guide: it finds persistent storage, checks the "
            "Linux GPU stack, recommends models that fit VRAM and disk, installs "
            "AI Starter, Graphics Creator, Game Dev, Music Producer, Agent Builder, "
            "ComfyUI, Blender, local LLMs, and safe helper tools under NVH_HOME, "
            "then tracks jobs, receipts, boot drift, and repairs. For deep code or "
            f"product questions, use the official repo {OFFICIAL_REPO_URL} and README "
            f"{OFFICIAL_README_URL} when internet access is available; offline I use "
            "the packaged product brief and local setup state."
        )
    elif _question_wants_debug(q):
        focus = "debugger"
        diagnostics = _safe_diagnostics_report(home_dir=home_dir)
        diagnostics_report_id = diagnostics.get("report_id")
        debug_findings = _diagnostic_job_findings(diagnostics, failed_job)
        log_highlights = _diagnostic_log_highlights(diagnostics)
        troubleshooting = analyze_setup_failure(diagnostics, failed_job, home_dir=home_dir)
        primary_finding = troubleshooting.get("primary_finding") or {}
        primary_action_id = primary_finding.get("action_id")
        if primary_action_id and not any(action.get("id") == primary_action_id for action in actions):
            extra_reply_actions.append({
                "id": primary_action_id,
                "title": primary_finding.get("button_label") or "Run Repair",
                "priority": 5,
                "status": primary_finding.get("severity") or "recommended",
                "command": "",
                "reason": primary_finding.get("summary") or "AI Wizard matched the log clue to this repair.",
                "can_run_without_root": True,
            })
        if failed_job:
            commands = _commands_for_actions(actions, str(primary_action_id or _action_for_job(failed_job)))
        elif primary_action_id:
            commands = _commands_for_actions(actions, str(primary_action_id))
        repair_note = primary_finding.get("summary") or _debug_repair_note(debug_findings, log_highlights)
        if debug_findings or log_highlights:
            evidence = []
            if debug_findings:
                evidence.append(f"Latest job clue: {debug_findings[0]}")
            if log_highlights:
                evidence.append(f"Log clue: {log_highlights[0]}")
            button = primary_finding.get("button_label") or "Fix My Setup"
            rootless_note = primary_finding.get("rootless_note") or "No sudo or OS package change should be needed."
            answer = (
                "I checked local jobs, receipts, the boot/setup report, and redacted log tails"
                f"{f' ({diagnostics_report_id})' if diagnostics_report_id else ''}. "
                + " ".join(evidence)
                + f" Likely issue: {primary_finding.get('title', 'setup needs attention')}. "
                + f"{repair_note} Next button: {button}. {rootless_note} "
                + "Advanced Details can stay closed unless you want the full trail."
            )
        else:
            answer = (
                "I checked local jobs, receipts, the boot/setup report, and redacted log tails"
                f"{f' ({diagnostics_report_id})' if diagnostics_report_id else ''}. "
                "I do not see a recent failed job or high-signal error line yet. Try the action again, "
                "then press Ask AI Wizard so I can read the fresh log trail."
            )
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
    elif any(phrase in q for phrase in [
        "are we online",
        "are we fully online",
        "is chat working",
        "chat working",
        "local chat",
        "can you answer",
        "are you ready",
        "llm ready",
    ]):
        focus = "chat-readiness"
        next_action_id = str(local_chat.get("next_action_id") or "")
        commands = _commands_for_actions(actions, next_action_id) if next_action_id else []
        if local_chat.get("ready"):
            answer = (
                f"Yes: setup is online and local model chat returned text. "
                f"{local_chat.get('summary', 'Local chat is verified.')} "
                "ComfyUI is not required for local chat; it is only needed for visual image/video workflows. "
                "You can use Ask AI for normal questions and this AI Wizard box for setup repair guidance."
            )
        else:
            answer = (
                "The setup guide is online, but local model chat is not verified yet. "
                f"{local_chat.get('summary', report['summary'])} "
                "That means I can still read setup state, jobs, receipts, and redacted logs here, "
                "but Ask AI may fail until the runtime/model smoke test returns text."
            )
    elif any(word in q for word in ["privacy", "data leave", "leave this vm", "send data", "cost", "money", "free", "quota"]):
        focus = "privacy-cost"
        suppress_command_fallback = True
        answer = (
            "Local AI runs inside this VM when you use Ollama and local models, so "
            "prompts stay on the machine unless you choose a cloud provider. Cloud "
            "API keys can add stronger advisors, but those requests may leave the VM "
            "and may have quotas or costs. nvHive itself installs rootlessly; the big "
            "cost to watch is disk space and download time for models and apps."
        )
    elif any(word in q for word in ["root", "sudo", "admin", "apt", "driver", "kernel", "cuda broken", "nvidia-smi"]):
        focus = "admin-boundary"
        suppress_command_fallback = True
        answer = (
            "nvHive is designed to work without admin access: it installs apps, "
            "models, ComfyUI, logs, and configs under NVH_HOME. It can repair "
            "rootless env files, launchers, catalogs, examples, and receipts. If "
            "the base VM does not expose the NVIDIA driver, CUDA, kernel modules, "
            "or nvidia-smi correctly, nvHive can explain the problem but the VM "
            "provider or admin must fix the host image."
        )
    elif any(word in q for word in ["survive", "reconnect", "restart", "reboot", "where are files", "where do files", "outputs", "projects"]):
        focus = "persistence"
        suppress_command_fallback = True
        storage_home = report["storage"]["layout"]["home"]
        answer = (
            f"Files should survive when they live under NVH_HOME: {storage_home}. "
            "That workspace holds models, ComfyUI, apps, projects, outputs, logs, "
            "jobs, receipts, config, and support snapshots. The OS image can refresh; "
            "the persistent block-backed workspace is the part nvHive protects."
        )
    elif any(word in q for word in ["comfy", "image", "video", "workflow"]):
        focus = "comfyui"
        suppress_command_fallback = True
        service = service_status("comfyui", home_dir=home_dir)
        answer, commands, extra_reply_actions, debug_findings, log_highlights = _service_answer_parts(service)
        extra_grounding_sources.extend(["rootless service registry", "live ComfyUI service probe"])
    elif service_for_question(q):
        service_focus = service_for_question(q)
        focus = "service-status"
        suppress_command_fallback = True
        if service_focus and service_focus.get("id") != "all":
            service = service_status(str(service_focus["id"]), home_dir=home_dir)
            focus = str(service_focus["id"])
            answer, commands, extra_reply_actions, debug_findings, log_highlights = _service_answer_parts(service)
        else:
            registry = list_service_statuses(home_dir=home_dir)
            answer, debug_findings = _service_summary_answer(registry)
            commands = []
            log_highlights = []
            extra_reply_actions = [
                action for action in (
                    _action_from_service(service) for service in registry.get("services", [])
                )
                if action
            ][:5]
        extra_grounding_sources.append("rootless service registry")
    elif any(word in q for word in ["model", "models", "llm", "ollama", "local ai", "license"]):
        focus = "models"
        commands = _commands_for_actions(actions, "rootless-ollama", "starter-models") or [
            "nvh studio --install rootless-ollama -y",
            "nvh studio --install-models recommended -y",
        ]
        answer = (
            "Start with the rootless Ollama runtime, then download the recommended models "
            "that fit the detected GPU. The wizard can run both steps and keeps files under "
            "NVH_HOME/models. Some model weights or ComfyUI checkpoints may require upstream "
            "license acceptance, so nvHive should keep those explicit instead of hiding them."
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
    elif any(word in q for word in ["route", "routing", "multi llm", "providers", "cloud", "convene"]):
        focus = "routing"
        suppress_command_fallback = True
        answer = (
            "nvHive is still the multi-LLM router underneath the wizard. Once local "
            "Ollama or cloud keys are configured, chat and Convene mode can route "
            "between local models and providers such as OpenAI, Anthropic, Gemini, "
            "Groq, xAI, and Mistral. The setup wizard's job is to make the local "
            "GPU workspace reliable first, then those advisors become available."
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
    elif any(word in q for word in ["music", "audio", "song", "stem", "whisper", "demucs", "ace-step", "daw"]):
        focus = "music"
        commands = _commands_for_actions(actions, "music-tools") or [
            "nvh studio --install music -y",
        ]
        answer = (
            "Music Producer Studio installs rootless audio and AI music helpers under "
            "NVH_HOME. It is meant for ACE-Step style music generation, Demucs stem "
            "separation, WhisperX transcription, and Audacity/LMMS helper launchers "
            "when the host supports the needed runtime."
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
                commands = repair_plan(first["id"], home_dir=home_dir)["commands"]
            except Exception as exc:
                logger.debug("repair_plan(%s) failed in chat handler: %s", first.get("id"), exc)
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
        commands = [action["command"] for action in core_actions[:3]]
        optional_actions = [action for action in actions if action.get("status") == "optional"]
        if core_actions:
            next_title = core_actions[0]["title"]
            answer = (
                f"Best core setup step: {next_title}. {report['summary']}. "
                f"Receipts tracked: {receipts.get('count', 0)}. I can answer setup and "
                "repair questions immediately; general homework or coding questions use "
                "the local model after the runtime and selected model are ready."
            )
        elif report.get("ready"):
            optional_titles = ", ".join(action["title"] for action in optional_actions[:3])
            optional_note = (
                f" Optional one-click add-ons are available later: {optional_titles}."
                if optional_titles
                else ""
            )
            if local_chat.get("ready"):
                answer = (
                    "Core local AI chat is verified. ComfyUI is not required for local chat, "
                    "coding help, or AI Wizard repair guidance; it is only needed for visual "
                    f"image/video workflows. {report['summary']}.{optional_note} "
                    f"Receipts tracked: {receipts.get('count', 0)}."
                )
            else:
                answer = (
                    "The offline setup guide is ready, but local model chat is not verified yet. "
                    f"{local_chat.get('summary', report['summary'])}.{optional_note} "
                    f"Receipts tracked: {receipts.get('count', 0)}."
                )
        else:
            answer = (
                f"{report['summary']}. I do not see a safe automatic core action yet; "
                "use Copy Support Report or Advanced Details if the VM image changed."
            )

    if not commands and actions and not suppress_command_fallback:
        commands = [core_actions[0]["command"]] if core_actions else []

    assistant_info = report.get("assistant", {})
    reply_actions = _reply_actions_for_focus(actions, focus)
    if extra_reply_actions:
        seen = {action.get("id") for action in extra_reply_actions}
        reply_actions = extra_reply_actions + [action for action in reply_actions if action.get("id") not in seen]
    return {
        "question": question,
        "answer": _persona_wrap(answer),
        "focus": focus,
        "commands": commands,
        "product": assistant_info.get("product", "nvHive / AI Wizard"),
        "mode": assistant_info.get("mode", "offline-deterministic"),
        "available_immediately": bool(assistant_info.get("available_immediately", True)),
        "requires_local_model": bool(assistant_info.get("requires_local_model", False)),
        "official_repo_url": OFFICIAL_REPO_URL,
        "readme_url": OFFICIAL_README_URL,
        "grounding_sources": [*assistant_info.get("grounding_sources", []), *extra_grounding_sources],
        "diagnostics_report_id": diagnostics_report_id,
        "debug_findings": debug_findings,
        "log_highlights": log_highlights,
        "troubleshooting": troubleshooting,
        "official_urls": troubleshooting.get("official_urls", []) if troubleshooting else [],
        "web_search_queries": troubleshooting.get("web_search_queries", []) if troubleshooting else [],
        "observations": {
            "ready": report["ready"],
            "issue_count": report.get("issue_count", 0),
            "receipt_count": receipts.get("count", 0),
            "unhealthy_receipts": receipts.get("unhealthy", 0),
            "catalog_source": report.get("catalog", {}).get("source"),
            "recent_problem": failed_job,
        },
        "actions": reply_actions,
    }
