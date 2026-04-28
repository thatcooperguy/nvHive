"""Boot-time VM image preflight for rootless cloud GPU sessions.

Cloud desktop images can change underneath a persistent user mount.  This
module records a small host fingerprint at nvHive startup, compares it to the
previous boot stored under ``NVH_HOME``, and keeps the wizard focused on what
changed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nvh.integrations.compatibility import compatibility_report
from nvh.integrations.storage import storage_layout

STATE_FILENAME = "boot-preflight.json"
STATE_SCHEMA_VERSION = 1

_FACT_LABELS = {
    "distro": "Base OS",
    "kernel": "Kernel",
    "machine": "Architecture",
    "libc": "glibc",
    "python_version": "Python",
    "python_strategy": "Python runtime",
    "gpu_name": "GPU",
    "gpu_memory_total_mb": "GPU memory",
    "driver_version": "NVIDIA driver",
    "cuda_version": "CUDA driver API",
    "git_available": "Git",
    "curl_available": "curl",
    "tar_available": "tar",
    "node_available": "Node.js",
    "npm_available": "npm",
    "display_available": "Desktop display",
    "storage_home": "NVH_HOME",
}

_CRITICAL_FACTS = {
    "distro",
    "kernel",
    "machine",
    "libc",
    "python_version",
    "python_strategy",
    "driver_version",
    "cuda_version",
    "git_available",
    "curl_available",
    "tar_available",
    "storage_home",
}


def _state_path(home_dir: str | Path | None = None) -> Path:
    return storage_layout(home_dir).config_dir / STATE_FILENAME


def _read_state(home_dir: str | Path | None = None) -> dict[str, Any] | None:
    path = _state_path(home_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _write_state(state: dict[str, Any], home_dir: str | Path | None = None) -> None:
    path = _state_path(home_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except Exception:
        pass


def _available(value: str | None) -> bool:
    return bool(value and str(value).strip())


def host_fingerprint(report: dict[str, Any]) -> dict[str, Any]:
    """Extract stable boot facts from a compatibility report."""
    host = report.get("host", {})
    commands = host.get("commands", {})
    gpu = host.get("gpu", {})
    python = host.get("python", {})
    libc = host.get("libc", {})
    display = host.get("display", {})
    storage = host.get("storage", {})
    storage_layout_data = storage.get("layout", {})
    return {
        "distro": host.get("distro", ""),
        "kernel": host.get("kernel", ""),
        "machine": host.get("machine", ""),
        "libc": f"{libc.get('name', '')} {libc.get('version', '')}".strip(),
        "python_version": python.get("version", ""),
        "python_strategy": python.get("strategy", ""),
        "gpu_name": gpu.get("name", ""),
        "gpu_memory_total_mb": str(gpu.get("memory_total_mb", "")),
        "driver_version": gpu.get("driver_version", ""),
        "cuda_version": gpu.get("cuda_version", ""),
        "git_available": _available(commands.get("git")),
        "curl_available": _available(commands.get("curl")),
        "tar_available": _available(commands.get("tar")),
        "node_available": _available(commands.get("node")),
        "npm_available": _available(commands.get("npm")),
        "display_available": _available(display.get("DISPLAY")) or _available(display.get("WAYLAND_DISPLAY")),
        "storage_home": storage_layout_data.get("home", ""),
    }


def fingerprint_id(fingerprint: dict[str, Any]) -> str:
    payload = json.dumps(fingerprint, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def diff_fingerprints(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[dict[str, Any]]:
    """Return user-facing boot changes between two host fingerprints."""
    if not previous:
        return []
    changes: list[dict[str, Any]] = []
    for key in sorted(set(previous) | set(current)):
        before = previous.get(key)
        after = current.get(key)
        if before == after:
            continue
        severity = "required" if key in _CRITICAL_FACTS else "recommended"
        changes.append(
            {
                "id": key,
                "label": _FACT_LABELS.get(key, key.replace("_", " ").title()),
                "before": str(before) if before not in (None, "") else "missing",
                "after": str(after) if after not in (None, "") else "missing",
                "severity": severity,
                "detail": "Re-run compatibility checks before launching installed apps.",
            }
        )
    return changes


def _result_summary(
    *,
    first_run: bool,
    changes: list[dict[str, Any]],
    compatibility: dict[str, Any],
) -> str:
    issue_count = int(compatibility.get("issue_count", 0) or 0)
    blocked_count = int(compatibility.get("blocked_count", 0) or 0)
    if first_run:
        return f"Boot baseline captured with {issue_count} compatibility item(s)."
    if changes:
        return f"VM image changed in {len(changes)} place(s); {blocked_count} blocked compatibility item(s)."
    if issue_count:
        return f"VM image unchanged; {issue_count} compatibility item(s) still need attention."
    return "VM image unchanged and app compatibility is ready."


def _agent_helper_status(compatibility: dict[str, Any]) -> dict[str, Any]:
    agent_app = next(
        (app for app in compatibility.get("apps", []) if app.get("id") == "agent-lab"),
        {},
    )
    local_ready = agent_app.get("status") == "ready"
    action_id = agent_app.get("recommended_action_id") or "agent-lab"
    return {
        "offline_helper_ready": True,
        "local_agent_ready": local_ready,
        "mode": "local-agent-ready" if local_ready else "offline-deterministic",
        "recommended_action_id": None if local_ready else action_id,
        "summary": (
            "Local agent helper is ready for guided setup."
            if local_ready
            else "Offline setup helper is active; install Local Agent Lab for the fuller AI assistant."
        ),
        "requirements": agent_app.get("requirements", []),
    }


def run_boot_preflight(home_dir: str | Path | None = None) -> dict[str, Any]:
    """Run and persist the boot preflight under the selected NVH_HOME."""
    previous_state = _read_state(home_dir)
    previous_result = previous_state.get("last_result") if previous_state else None
    previous_fingerprint = (
        previous_state.get("last_fingerprint")
        if previous_state
        else None
    )

    compatibility = compatibility_report(home_dir=home_dir)
    current_fingerprint = host_fingerprint(compatibility)
    current_id = fingerprint_id(current_fingerprint)
    previous_id = fingerprint_id(previous_fingerprint) if previous_fingerprint else None
    changes = diff_fingerprints(previous_fingerprint, current_fingerprint)
    first_run = previous_fingerprint is None
    checked_at = datetime.now(UTC).isoformat()
    agent_helper = _agent_helper_status(compatibility)

    result = {
        "schema_version": STATE_SCHEMA_VERSION,
        "checked_at": checked_at,
        "state_file": str(_state_path(home_dir)),
        "first_run": first_run,
        "changed": bool(changes),
        "needs_attention": bool(changes) or not compatibility.get("ready", False),
        "fingerprint_id": current_id,
        "previous_fingerprint_id": previous_id,
        "previous_checked_at": previous_result.get("checked_at") if isinstance(previous_result, dict) else None,
        "summary": _result_summary(first_run=first_run, changes=changes, compatibility=compatibility),
        "changes": changes,
        "agent_helper": agent_helper,
        "compatibility": compatibility,
    }
    _write_state(
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "last_checked_at": checked_at,
            "last_fingerprint": current_fingerprint,
            "last_result": result,
        },
        home_dir=home_dir,
    )
    return result


def boot_preflight_status(
    home_dir: str | Path | None = None,
    *,
    run_if_missing: bool = True,
) -> dict[str, Any]:
    """Return the latest boot preflight, optionally running it once if absent."""
    state = _read_state(home_dir)
    result = state.get("last_result") if state else None
    if isinstance(result, dict):
        return result
    if run_if_missing:
        return run_boot_preflight(home_dir=home_dir)
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "checked_at": None,
        "state_file": str(_state_path(home_dir)),
        "first_run": True,
        "changed": False,
        "needs_attention": False,
        "fingerprint_id": None,
        "previous_fingerprint_id": None,
        "previous_checked_at": None,
        "summary": "Boot preflight has not run yet.",
        "changes": [],
        "agent_helper": {
            "offline_helper_ready": True,
            "local_agent_ready": False,
            "mode": "offline-deterministic",
            "recommended_action_id": "agent-lab",
            "summary": "Offline setup helper is active; boot preflight has not checked Local Agent Lab yet.",
            "requirements": [],
        },
        "compatibility": None,
    }
