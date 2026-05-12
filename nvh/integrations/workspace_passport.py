"""Workspace Passport and rootless setup planning for AI Wizard.

The target nvHive environment treats the base Linux VM as replaceable and the
user-writable persistent mount as the durable workstation. This module records
that contract in a small JSON passport under ``NVH_HOME`` and exposes
deterministic plans that never require root/admin access.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nvh import __version__
from nvh.integrations.runtime import runtime_status
from nvh.integrations.storage import ensure_storage, storage_status

PASSPORT_SCHEMA_VERSION = 1
ROOTLESS_MIN_FREE_GB = 200.0


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_call(label: str, fn) -> dict[str, Any]:
    try:
        return {"ok": True, "data": fn()}
    except Exception as exc:
        return {"ok": False, "error": {"label": label, "type": type(exc).__name__, "message": str(exc)}}


def _fingerprint_id(parts: dict[str, Any]) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _host_fingerprint() -> dict[str, Any]:
    parts = {
        "system": platform.system(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "executable": sys.executable,
        "nvhive_version": __version__,
    }
    parts["id"] = _fingerprint_id(parts)
    return parts


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except Exception:
        pass


def _layout_paths(layout) -> dict[str, str]:
    return {key: str(value) for key, value in asdict(layout).items()}


def _receipt_summary() -> dict[str, Any]:
    from nvh.integrations.receipts import receipt_summary

    return receipt_summary()


def _recent_jobs() -> list[dict[str, Any]]:
    from nvh.integrations.jobs import list_jobs

    return list_jobs(limit=10)


def _model_fit(home_dir: str | Path | None = None) -> dict[str, Any]:
    from nvh.integrations.model_fit import model_fit_report

    return model_fit_report(home_dir=home_dir)


def _compatibility(home_dir: str | Path | None = None) -> dict[str, Any]:
    from nvh.integrations.compatibility import compatibility_report

    return compatibility_report(home_dir=home_dir)


@dataclass(frozen=True)
class RootlessGate:
    """One rootless safety gate for setup/install actions."""

    id: str
    title: str
    status: str
    summary: str
    requires_admin: bool = False
    action_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def rootless_policy_report(
    home_dir: str | Path | None = None,
    *,
    min_free_gb: float = ROOTLESS_MIN_FREE_GB,
) -> dict[str, Any]:
    """Return the no-root operating contract for this workstation."""
    storage = storage_status(home_dir=home_dir, min_free_gb=min_free_gb).as_dict()
    runtime = runtime_status().as_dict()
    gates: list[RootlessGate] = []

    storage_explicit = storage.get("configured_by") != "default"
    storage_ok = bool(storage.get("ok"))
    if storage_ok and storage_explicit:
        gates.append(RootlessGate(
            "persistent-storage",
            "Persistent storage",
            "pass",
            "NVH_HOME is explicit, writable, and has enough space for rootless installs.",
        ))
    elif storage_ok:
        gates.append(RootlessGate(
            "persistent-storage",
            "Persistent storage",
            "warn",
            "Storage is writable, but NVH_HOME is still the default path. Confirm it is on the persistent block volume before large downloads.",
            action_id="storage",
        ))
    else:
        gates.append(RootlessGate(
            "persistent-storage",
            "Persistent storage",
            "blocked",
            "; ".join(storage.get("warnings", [])) or "Selected NVH_HOME is not ready.",
            action_id="storage",
        ))

    strategy = str(runtime.get("strategy") or "")
    if strategy in {"python-venv", "micromamba-fallback"}:
        gates.append(RootlessGate(
            "rootless-runtime",
            "Rootless runtime",
            "pass",
            f"Runtime strategy is {strategy}; no system Python mutation is required.",
        ))
    else:
        gates.append(RootlessGate(
            "rootless-runtime",
            "Rootless runtime",
            "warn",
            "Python venv/pip is incomplete; install the managed micromamba fallback under NVH_HOME.",
            action_id="runtime-fallback",
        ))

    gates.append(RootlessGate(
        "admin-boundary",
        "Admin boundary",
        "pass",
        "nvHive will not use sudo, apt, system services, /usr/local, or host driver changes for normal setup.",
    ))

    gates.append(RootlessGate(
        "driver-boundary",
        "GPU driver boundary",
        "info",
        "If the base VM does not expose a working NVIDIA driver/GPU, nvHive can diagnose it but cannot repair the host without admin access.",
        requires_admin=True,
    ))

    blocked = sum(1 for gate in gates if gate.status == "blocked")
    warned = sum(1 for gate in gates if gate.status == "warn")
    return {
        "schema_version": 1,
        "checked_at": _now(),
        "status": "blocked" if blocked else "warn" if warned else "ready",
        "summary": (
            "Rootless setup is blocked until persistent storage is ready."
            if blocked
            else "Rootless setup is ready with recommended follow-up checks."
            if warned
            else "Rootless setup is ready."
        ),
        "no_root_required": True,
        "allowed_write_roots": [storage["layout"]["home"]],
        "blocked_operations": [
            "sudo",
            "apt/yum/dnf system package installs",
            "writes to /usr, /usr/local, /opt, /etc, or system service directories",
            "Docker daemon assumptions",
            "host NVIDIA driver or kernel changes",
        ],
        "preferred_runtimes": ["python-venv", "uv", "managed-micromamba", "rootless-container-if-detected"],
        "storage": storage,
        "runtime": runtime,
        "gates": [gate.as_dict() for gate in gates],
    }


def workspace_passport(
    home_dir: str | Path | None = None,
    *,
    create: bool = True,
    min_free_gb: float = ROOTLESS_MIN_FREE_GB,
) -> dict[str, Any]:
    """Return and refresh the persistent Workspace Passport."""
    storage_obj = ensure_storage(home_dir, min_free_gb=min_free_gb, activate=True) if create else storage_status(home_dir, min_free_gb=min_free_gb)
    layout = storage_obj.layout
    passport_path = layout.config_dir / "workspace-passport.json"
    legacy_passport_path = layout.home / "nvhive.json"
    existing = _read_json(passport_path) or _read_json(legacy_passport_path)
    now = _now()

    if create:
        receipts = _safe_call("receipts", _receipt_summary)
        jobs = _safe_call("jobs", _recent_jobs)
        model_fit = _safe_call("model_fit", lambda: _model_fit(layout.home))
        compatibility = _safe_call("compatibility", lambda: _compatibility(layout.home))
    else:
        receipts = {"ok": False, "data": None, "error": {"message": "Preview mode; receipts not loaded."}}
        jobs = {"ok": True, "data": []}
        model_fit = {"ok": False, "data": None, "error": {"message": "Preview mode; model fit not loaded."}}
        compatibility = {"ok": False, "data": None, "error": {"message": "Preview mode; compatibility not loaded."}}
    policy = rootless_policy_report(home_dir=home_dir, min_free_gb=min_free_gb)

    active_jobs = []
    if jobs.get("ok"):
        active_jobs = [
            job for job in jobs["data"]
            if job.get("status") in {"queued", "running"}
        ]

    passport = {
        "schema_version": PASSPORT_SCHEMA_VERSION,
        "workspace_id": existing.get("workspace_id") or f"nvh-{uuid.uuid4().hex[:12]}",
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
        "product": "nvHive",
        "assistant": "AI Wizard",
        "nvhive_version": __version__,
        "storage_home": str(layout.home),
        "passport_path": str(passport_path),
        "legacy_passport_path": str(legacy_passport_path),
        "rootless": {
            "normal_setup_requires_admin": False,
            "host_driver_requires_admin_if_broken": True,
            "policy_status": policy["status"],
        },
        "paths": _layout_paths(layout),
        "host_fingerprint": _host_fingerprint(),
        "storage": storage_obj.as_dict(),
        "policy": policy,
        "receipts": receipts,
        "jobs": {
            "ok": jobs.get("ok", False),
            "active_count": len(active_jobs),
            "recent_count": len(jobs.get("data", [])) if jobs.get("ok") else 0,
            "active": active_jobs[:5],
            "error": jobs.get("error"),
        },
        "model_fit": model_fit,
        "compatibility": compatibility,
    }
    if create:
        _write_json(passport_path, passport)
    return passport


def workspace_plan(
    profile: str = "student",
    home_dir: str | Path | None = None,
    *,
    min_free_gb: float = ROOTLESS_MIN_FREE_GB,
) -> dict[str, Any]:
    """Return a safe rootless plan for a mission profile."""
    passport = workspace_passport(home_dir=home_dir, create=True, min_free_gb=min_free_gb)
    policy = passport["policy"]
    storage = passport["storage"]
    rootless_blocked = policy["status"] == "blocked"
    runtime_strategy = policy["runtime"].get("strategy")

    profile_titles = {
        "student": "AI Starter",
        "llm": "Local LLM Lab",
        "creator": "Graphics Creator Studio",
        "agent": "Agent Builder",
        "game": "Game Dev Lab",
        "music": "Music Producer Studio",
        "full": "Power User Workstation",
    }
    first_win = {
        "student": "Open private local chat",
        "llm": "Ask your first local model question",
        "creator": "Start the first ComfyUI image workflow",
        "agent": "Run the local agent lab smoke test",
        "game": "Open the game project workspace",
        "music": "Open the music studio workspace",
        "full": "Open the workstation launcher dashboard",
    }

    steps = [
        {
            "id": "storage",
            "title": "Use persistent drive",
            "status": "blocked" if rootless_blocked else "pass",
            "summary": (
                "Pick a writable block-backed NVH_HOME before downloads."
                if rootless_blocked
                else f"Workspace will live at {storage['layout']['home']}."
            ),
            "action_id": "storage",
            "requires_admin": False,
            "risk": "safe",
        },
        {
            "id": "runtime",
            "title": "Prepare rootless runtime",
            "status": "pass" if runtime_strategy in {"python-venv", "micromamba-fallback"} else "warn",
            "summary": f"Runtime strategy: {runtime_strategy}.",
            "action_id": None if runtime_strategy in {"python-venv", "micromamba-fallback"} else "runtime-fallback",
            "requires_admin": False,
            "risk": "safe",
        },
        {
            "id": "mission-packs",
            "title": f"Install {profile_titles.get(profile, profile)} tools",
            "status": "ready" if not rootless_blocked else "blocked",
            "summary": "Install only recipes that declare no-root compatibility and pass host checks.",
            "action_id": "studio-packs",
            "requires_admin": False,
            "risk": "moderate",
        },
        {
            "id": "models",
            "title": "Download compatible models",
            "status": "ready" if not rootless_blocked else "blocked",
            "summary": "Use VRAM and disk fit before downloading. Large or gated models stay explicit.",
            "action_id": "starter-models",
            "requires_admin": False,
            "risk": "moderate",
        },
        {
            "id": "first-win",
            "title": first_win.get(profile, "Open the launcher"),
            "status": "ready" if not rootless_blocked else "blocked",
            "summary": "End setup with a working local action, not another config screen.",
            "action_id": "smoke-tests",
            "requires_admin": False,
            "risk": "safe",
        },
    ]

    return {
        "schema_version": 1,
        "checked_at": _now(),
        "profile": profile,
        "title": profile_titles.get(profile, profile.replace("-", " ").title()),
        "summary": (
            "Persistent storage must be fixed before this mission can run."
            if rootless_blocked
            else "This mission can be built without root/admin access."
        ),
        "rootless_safe": not rootless_blocked,
        "passport": {
            "workspace_id": passport["workspace_id"],
            "storage_home": passport["storage_home"],
            "policy_status": passport["policy"]["status"],
            "active_jobs": passport["jobs"]["active_count"],
        },
        "steps": steps,
    }


def support_snapshot(
    home_dir: str | Path | None = None,
    *,
    include_logs: bool = True,
    min_free_gb: float = ROOTLESS_MIN_FREE_GB,
) -> dict[str, Any]:
    """Write a redacted support snapshot under NVH_HOME/support."""
    from nvh.integrations.diagnostics import diagnostics_report

    passport = workspace_passport(home_dir=home_dir, create=True, min_free_gb=min_free_gb)
    layout_home = Path(passport["storage_home"])
    support_dir = Path(passport["paths"]["support_dir"])
    support_dir.mkdir(parents=True, exist_ok=True)
    diagnostics = diagnostics_report(home_dir=layout_home, include_logs=include_logs)
    snapshot = {
        "schema_version": 1,
        "created_at": _now(),
        "summary": "Redacted nvHive support snapshot. Review before sharing.",
        "passport": {
            "workspace_id": passport["workspace_id"],
            "storage_home": passport["storage_home"],
            "rootless": passport["rootless"],
            "host_fingerprint": passport["host_fingerprint"],
        },
        "policy": passport["policy"],
        "diagnostics": diagnostics,
        "excludes": [
            "API keys and bearer tokens",
            "model weights",
            "generated media",
            "private prompts",
            "SSH keys",
            "browser cookies",
        ],
    }
    path = support_dir / f"support-snapshot-{snapshot['created_at'].replace(':', '').replace('.', '-')[:19]}.json"
    _write_json(path, snapshot)
    snapshot["path"] = str(path)
    return snapshot
