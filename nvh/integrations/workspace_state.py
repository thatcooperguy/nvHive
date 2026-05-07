"""Canonical rootless workspace readiness state for nvWizard.

The setup UI has several useful diagnostics, but first-run users need one
clear answer: "am I ready, what is happening, and what should happen next?"
This module aggregates the existing checks without performing downloads,
repairs, or OS changes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nvh.integrations.boot_preflight import boot_preflight_status
from nvh.integrations.jobs import RUNNING_STATUSES, list_jobs
from nvh.integrations.model_fit import model_fit_report
from nvh.integrations.production_readiness import production_readiness_report
from nvh.integrations.runtime import runtime_status
from nvh.integrations.storage import storage_status
from nvh.integrations.studio_packs import ollama_runtime_doctor


def _check(
    check_id: str,
    label: str,
    status: str,
    summary: str,
    *,
    action_id: str | None = None,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "id": check_id,
        "label": label,
        "status": status,
        "summary": summary,
        "detail": detail,
        "action_id": action_id,
    }


def _safe_call(fn, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        result = fn()
    except Exception as exc:
        return {**fallback, "error": str(exc)}
    return result if isinstance(result, dict) else fallback


def _next_action(checks: list[dict[str, Any]], active_jobs: list[dict[str, Any]]) -> dict[str, str] | None:
    if active_jobs:
        return {
            "id": "watch-jobs",
            "label": "Watch progress",
            "description": active_jobs[0].get("message") or active_jobs[0].get("title") or "A setup job is running.",
        }
    for status in ("fail", "warn"):
        for check in checks:
            if check.get("status") == status and check.get("action_id"):
                return {
                    "id": str(check["action_id"]),
                    "label": str(check.get("label") or "Fix setup"),
                    "description": str(check.get("summary") or "Run the recommended rootless action."),
                }
    return None


def _phase(checks: list[dict[str, Any]], active_jobs: list[dict[str, Any]]) -> str:
    if active_jobs:
        return "working"
    if any(check.get("status") == "fail" for check in checks):
        return "blocked"
    if any(check.get("status") == "warn" for check in checks):
        return "needs-action"
    if any(check.get("status") == "checking" for check in checks):
        return "checking"
    return "ready"


def _score(checks: list[dict[str, Any]], active_jobs: list[dict[str, Any]]) -> int:
    score = 100
    score -= 30 * sum(1 for check in checks if check.get("status") == "fail")
    score -= 12 * sum(1 for check in checks if check.get("status") == "warn")
    score -= 6 * len(active_jobs)
    return max(0, min(100, score))


def workspace_state(home_dir: str | Path | None = None) -> dict[str, Any]:
    """Return a compact, UI-friendly health state for the current workspace."""
    storage = _safe_call(
        lambda: storage_status(home_dir=home_dir, min_free_gb=20).as_dict(),
        {"ok": False, "free_gb": None, "layout": {"home": str(home_dir or "")}},
    )
    runtime = _safe_call(lambda: runtime_status().as_dict(), {"strategy": "needs-runtime"})
    doctor = _safe_call(lambda: ollama_runtime_doctor(home_dir=home_dir), {"ready": False, "status": "unknown"})
    model_fit = _safe_call(lambda: model_fit_report(home_dir=home_dir), {"recommended_ids": [], "storage_fits_queue": True})
    boot = _safe_call(
        lambda: boot_preflight_status(home_dir=home_dir, run_if_missing=False),
        {"needs_attention": True, "changed": False, "summary": "Boot check has not completed yet."},
    )
    production = _safe_call(
        lambda: production_readiness_report(home_dir=home_dir),
        {"status": "pilot-ready", "counts": {"passed": 0, "warnings": 1, "blocked": 0, "total": 1}},
    )
    jobs = list_jobs(limit=8, home_dir=home_dir)
    active_jobs = [job for job in jobs if job.get("status") in RUNNING_STATUSES]
    recent_failures = [
        job for job in jobs
        if job.get("status") in {"failed", "interrupted", "canceled"}
    ][:3]

    checks: list[dict[str, Any]] = []
    checks.append(_check(
        "storage",
        "Storage",
        "pass" if storage.get("ok") else "fail",
        (
            f"{storage.get('free_gb')} GB free under {storage.get('layout', {}).get('home', 'NVH_HOME')}"
            if storage.get("ok")
            else "NVH_HOME is not writable or does not have enough free space."
        ),
        action_id=None if storage.get("ok") else "storage",
    ))
    checks.append(_check(
        "runtime",
        "Python runtime",
        "fail" if runtime.get("strategy") == "needs-runtime" else "pass",
        str(runtime.get("strategy") or "runtime unknown"),
        action_id="runtime-fallback" if runtime.get("strategy") == "needs-runtime" else None,
        detail="; ".join(str(note) for note in runtime.get("notes", [])),
    ))
    checks.append(_check(
        "local-ai",
        "Local AI",
        "pass" if doctor.get("ready") else "warn",
        str(doctor.get("summary") or "Local AI status is unknown."),
        action_id=(doctor.get("next_action") or {}).get("id"),
    ))
    checks.append(_check(
        "models",
        "Models",
        "fail" if model_fit.get("storage_fits_queue") is False else ("warn" if doctor.get("missing_recommended_ids") else "pass"),
        str(model_fit.get("summary") or "Model fit check complete."),
        action_id="starter-models" if doctor.get("missing_recommended_ids") else None,
    ))
    checks.append(_check(
        "jobs",
        "Setup jobs",
        "checking" if active_jobs else ("warn" if recent_failures else "pass"),
        (
            f"{len(active_jobs)} job(s) running."
            if active_jobs
            else f"{len(recent_failures)} recent job(s) need review."
            if recent_failures
            else "No active setup jobs."
        ),
        action_id="support-snapshot" if recent_failures else None,
    ))
    checks.append(_check(
        "boot",
        "Boot image",
        "warn" if boot.get("changed") or boot.get("needs_attention") else "pass",
        str(boot.get("summary") or "Boot preflight complete."),
        action_id="boot-preflight" if boot.get("changed") or boot.get("needs_attention") else None,
    ))

    phase = _phase(checks, active_jobs)
    ready = phase == "ready"
    score = _score(checks, active_jobs)
    action = _next_action(checks, active_jobs)
    if ready:
        summary = "Workspace is ready: storage, runtime, local AI, and model checks are green."
    elif active_jobs:
        summary = "Workspace setup is running. Keep this page open for progress."
    else:
        summary = f"Workspace needs attention in {sum(1 for check in checks if check.get('status') in {'warn', 'fail'})} area(s)."

    return {
        "schema_version": 1,
        "checked_at": datetime.now(UTC).isoformat(),
        "phase": phase,
        "ready": ready,
        "summary": summary,
        "health_score": score,
        "next_action": action,
        "checks": checks,
        "storage_home": storage.get("layout", {}).get("home"),
        "free_gb": storage.get("free_gb"),
        "active_jobs": active_jobs,
        "recent_failures": recent_failures,
        "runtime": runtime,
        "local_ai": doctor,
        "model_fit": {
            "summary": model_fit.get("summary"),
            "recommended_ids": model_fit.get("recommended_ids", []),
            "detected_vram_gb": model_fit.get("detected_vram_gb", 0),
            "storage_fits_queue": model_fit.get("storage_fits_queue"),
        },
        "boot": {
            "summary": boot.get("summary"),
            "changed": boot.get("changed", False),
            "needs_attention": boot.get("needs_attention", False),
        },
        "production": {
            "status": production.get("status"),
            "counts": production.get("counts", {}),
            "next_actions": production.get("next_actions", []),
        },
        "rootless": True,
    }
