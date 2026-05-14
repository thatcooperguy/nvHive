"""Production readiness gates for rootless NVIDIA cloud desktop installs.

This report is intentionally conservative. It can prove CI-style and local
rootless invariants anywhere, but it will not claim production readiness until
the real Linux GPU VM image has passed acceptance.
"""

from __future__ import annotations

import os
import platform
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nvh.integrations.diagnostics.boot_preflight import boot_preflight_status
from nvh.integrations.diagnostics.compatibility import compatibility_report
from nvh.integrations.diagnostics.model_fit import model_fit_report
from nvh.integrations.diagnostics.smoke_tests import smoke_test_report
from nvh.integrations.installs.studio_packs import catalog_with_status
from nvh.integrations.services.receipts import receipt_summary
from nvh.integrations.services.runtime import runtime_status
from nvh.integrations.workspace.mount_autopilot import mount_autopilot_report
from nvh.integrations.workspace.storage import storage_status

TARGET_VM_CHECKLIST = [
    "Fresh no-root install from GitHub or PyPI on the NVIDIA Linux VM.",
    "Confirm NVH_HOME lands on the persistent 200 GB+ block-backed mount.",
    "Install AI Starter from the wizard and verify Ollama plus recommended model queue.",
    "Install Graphics Creator Studio and launch ComfyUI with starter examples.",
    "Install Game Dev Lab and verify Blender/Godot helper launchers.",
    "Install Music Producer Studio and verify helper workspaces without sudo.",
    "Reboot or reconnect the VM and confirm boot preflight detects no unexpected drift.",
]


@dataclass(frozen=True)
class ReadinessGate:
    """One productization gate in the rootless setup path."""

    id: str
    title: str
    status: str
    summary: str
    detail: str = ""
    recommendation: str = ""
    source: str = "local"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _gate(
    gate_id: str,
    title: str,
    status: str,
    summary: str,
    *,
    detail: str = "",
    recommendation: str = "",
    source: str = "local",
) -> ReadinessGate:
    return ReadinessGate(
        id=gate_id,
        title=title,
        status=status,
        summary=summary,
        detail=detail,
        recommendation=recommendation,
        source=source,
    )


def _flag_enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "passed", "validated"}


def _target_vm_validated(explicit: bool | None) -> bool:
    if explicit is not None:
        return bool(explicit)
    return _flag_enabled(os.environ.get("NVH_TARGET_VM_VALIDATED"))


def _path_detail(path: Any) -> str:
    if path in (None, ""):
        return ""
    try:
        return str(Path(path))
    except Exception:
        return str(path)


def _storage_gate(storage: dict[str, Any]) -> ReadinessGate:
    configured = storage.get("configured_by") != "default"
    ok = bool(storage.get("ok"))
    home = _path_detail(storage.get("layout", {}).get("home"))
    free_gb = storage.get("free_gb")
    detail = f"{home} ({free_gb if free_gb is not None else '?'} GB free)"
    if ok and configured:
        return _gate(
            "persistent-storage",
            "Persistent storage",
            "pass",
            "NVH_HOME is writable and explicitly configured.",
            detail=detail,
        )
    if ok:
        return _gate(
            "persistent-storage",
            "Persistent storage",
            "warn",
            "Storage is writable, but NVH_HOME is still using the default path.",
            detail=detail,
            recommendation="Let mount autopilot activate the persistent block volume before large downloads.",
        )
    return _gate(
        "persistent-storage",
        "Persistent storage",
        "blocked",
        "Selected storage is not ready for large rootless installs.",
        detail="; ".join(storage.get("warnings", [])) or detail,
        recommendation="Use a writable persistent block-backed mount and rerun storage detection.",
    )


def _mount_gate(mount: dict[str, Any], storage: dict[str, Any]) -> ReadinessGate:
    current_ok = bool(storage.get("ok") and storage.get("configured_by") != "default")
    recommended = mount.get("recommended") or {}
    if current_ok:
        return _gate(
            "mount-autopilot",
            "Mount autopilot",
            "pass",
            "Active NVH_HOME is already explicit and writable.",
            detail=_path_detail(storage.get("layout", {}).get("home")),
        )
    if recommended:
        confidence = mount.get("confidence", "none")
        safe_candidate = bool(
            recommended.get("writable")
            and not recommended.get("read_only")
            and not recommended.get("network_mount")
            and not recommended.get("os_mount")
        )
        status = "warn" if safe_candidate else "blocked"
        return _gate(
            "mount-autopilot",
            "Mount autopilot",
            status,
            f"Recommended persistent home: {recommended.get('recommended_home')}",
            detail=f"confidence={confidence}, score={recommended.get('score')}",
            recommendation=(
                "Activate this mount before installing models."
                if safe_candidate
                else "Pick a writable local block mount; read-only shares and OS disks are unsafe."
            ),
        )
    return _gate(
        "mount-autopilot",
        "Mount autopilot",
        "warn",
        "No persistent mount candidate was found automatically.",
        recommendation="Set NVH_HOME to the mounted block volume before running the wizard.",
    )


def _runtime_gate(runtime: dict[str, Any]) -> ReadinessGate:
    strategy = str(runtime.get("strategy") or "")
    if strategy in {"python-venv", "micromamba-fallback"}:
        return _gate(
            "runtime-toolchain",
            "Rootless runtime",
            "pass",
            f"Runtime strategy is {strategy}.",
            detail=runtime.get("python_version", ""),
        )
    return _gate(
        "runtime-toolchain",
        "Rootless runtime",
        "warn",
        "Python venv/pip is incomplete and the micromamba fallback is not installed yet.",
        detail="; ".join(runtime.get("notes", [])),
        recommendation="Install the rootless runtime fallback before ComfyUI, agents, or music tools.",
    )


def _gpu_gate(compatibility: dict[str, Any]) -> ReadinessGate:
    host = compatibility.get("host", {})
    system = host.get("system") or platform.system()
    gpu = host.get("gpu", {})
    gpu_name = gpu.get("name")
    if system != "Linux":
        return _gate(
            "linux-gpu-session",
            "Linux NVIDIA GPU session",
            "warn",
            "Current machine is not the target Linux GPU VM.",
            detail=str(system),
            recommendation="Run the same readiness report tomorrow on the NVIDIA Linux VM.",
            source="target-vm",
        )
    if gpu_name:
        return _gate(
            "linux-gpu-session",
            "Linux NVIDIA GPU session",
            "pass",
            f"Detected {gpu_name}.",
            detail=f"driver={gpu.get('driver_version') or 'unknown'}, cuda={gpu.get('cuda_version') or 'unknown'}",
            source="target-vm",
        )
    return _gate(
        "linux-gpu-session",
        "Linux NVIDIA GPU session",
        "blocked",
        "Linux is detected, but no NVIDIA GPU is visible to nvHive.",
        detail=str(gpu.get("detection_status") or "not-detected"),
        recommendation="Start a GPU-backed session or ask the provider to expose NVIDIA devices.",
        source="target-vm",
    )


def _compatibility_gate(compatibility: dict[str, Any]) -> ReadinessGate:
    blocked = int(compatibility.get("blocked_count", 0) or 0)
    issues = int(compatibility.get("issue_count", 0) or 0)
    fixable = int(compatibility.get("rootless_fixable_count", 0) or 0)
    if blocked:
        return _gate(
            "app-compatibility",
            "App compatibility",
            "blocked",
            f"{blocked} app/profile item(s) require base image, driver, or OS changes.",
            detail=compatibility.get("summary", ""),
            recommendation="Resolve blocked compatibility items before production release.",
        )
    if issues:
        return _gate(
            "app-compatibility",
            "App compatibility",
            "warn",
            f"{issues} compatibility item(s) need attention; {fixable} are rootless-fixable.",
            detail=compatibility.get("summary", ""),
            recommendation="Run safe repairs and install requested mission dependencies.",
        )
    return _gate(
        "app-compatibility",
        "App compatibility",
        "pass",
        "No app compatibility blockers detected.",
        detail=compatibility.get("summary", ""),
    )


def _boot_gate(boot: dict[str, Any]) -> ReadinessGate:
    if not boot.get("checked_at"):
        return _gate(
            "boot-preflight",
            "Boot preflight",
            "warn",
            "Boot preflight has not captured a baseline yet.",
            recommendation="Run boot preflight once on app startup or with the setup recheck button.",
        )
    if boot.get("changed"):
        return _gate(
            "boot-preflight",
            "Boot preflight",
            "warn",
            "The base VM image changed since the last check.",
            detail=boot.get("summary", ""),
            recommendation="Review driver, CUDA, Python, storage, and model recommendations before launch.",
        )
    if boot.get("needs_attention"):
        return _gate(
            "boot-preflight",
            "Boot preflight",
            "warn",
            "Boot preflight still has items needing attention.",
            detail=boot.get("summary", ""),
            recommendation="Run safe repairs or follow the recommended action.",
        )
    return _gate(
        "boot-preflight",
        "Boot preflight",
        "pass",
        "Boot baseline is captured and unchanged.",
        detail=boot.get("summary", ""),
    )


def _smoke_gate(smoke: dict[str, Any]) -> ReadinessGate:
    failed = int(smoke.get("failed", 0) or 0)
    warnings = int(smoke.get("warnings", 0) or 0)
    if failed:
        return _gate(
            "smoke-tests",
            "Smoke tests",
            "blocked",
            f"{failed} smoke test(s) failed.",
            detail=smoke.get("summary", ""),
            recommendation="Fix failed app health checks before production release.",
        )
    if warnings:
        return _gate(
            "smoke-tests",
            "Smoke tests",
            "warn",
            f"{warnings} smoke test warning(s) remain.",
            detail=smoke.get("summary", ""),
            recommendation="Warnings are acceptable for beta, but should be explained in release notes.",
        )
    return _gate("smoke-tests", "Smoke tests", "pass", "All lightweight smoke checks passed.", detail=smoke.get("summary", ""))


def _model_gate(model_fit: dict[str, Any]) -> ReadinessGate:
    if model_fit.get("storage_fits_queue") is False:
        return _gate(
            "model-fit",
            "Model fit",
            "blocked",
            "Recommended model queue does not fit the detected persistent storage.",
            detail=model_fit.get("summary", ""),
            recommendation="Reduce the default model queue or use a larger persistent volume.",
        )
    return _gate(
        "model-fit",
        "Model fit",
        "pass",
        "Recommended model queue fits current storage assumptions.",
        detail=model_fit.get("summary", ""),
    )


def _receipts_gate(receipts: dict[str, Any]) -> ReadinessGate:
    unhealthy = int(receipts.get("unhealthy", 0) or 0)
    count = int(receipts.get("count", 0) or 0)
    if unhealthy:
        return _gate(
            "install-receipts",
            "Install receipts",
            "warn",
            f"{unhealthy} install receipt(s) need repair.",
            detail=f"{count} total receipt(s)",
            recommendation="Repair or reinstall unhealthy app packs before calling the VM production-ready.",
        )
    return _gate(
        "install-receipts",
        "Install receipts",
        "pass",
        "No unhealthy install receipts detected.",
        detail=f"{count} total receipt(s)",
    )


def _pack_safety_gate(catalog: dict[str, Any]) -> ReadinessGate:
    packs = catalog.get("packs", [])
    non_rootless = [pack.get("id") for pack in packs if not pack.get("no_root")]
    if non_rootless:
        return _gate(
            "rootless-pack-safety",
            "Rootless pack safety",
            "blocked",
            "Some setup packs are not marked no-root.",
            detail=", ".join(str(item) for item in non_rootless),
            recommendation="Every one-click wizard pack must install into NVH_HOME or explain its external requirement.",
        )
    if not packs:
        return _gate(
            "rootless-pack-safety",
            "Rootless pack safety",
            "warn",
            "Studio pack catalog is empty.",
            recommendation="Restore the bundled studio pack catalog before release.",
        )
    return _gate(
        "rootless-pack-safety",
        "Rootless pack safety",
        "pass",
        f"{len(packs)} studio pack(s) are marked no-root.",
    )


def _target_acceptance_gate(validated: bool) -> ReadinessGate:
    if validated:
        return _gate(
            "target-vm-acceptance",
            "Target VM acceptance",
            "pass",
            "Real NVIDIA Linux VM acceptance has been marked complete.",
            source="target-vm",
        )
    return _gate(
        "target-vm-acceptance",
        "Target VM acceptance",
        "warn",
        "Real NVIDIA Linux VM acceptance is still required.",
        recommendation="Run the checklist on the target VM and set NVH_TARGET_VM_VALIDATED=1 for the final report.",
        source="target-vm",
    )


def production_readiness_report(
    home_dir: str | Path | None = None,
    *,
    target_vm_validated: bool | None = None,
) -> dict[str, Any]:
    """Return a conservative production readiness report for AI Wizard."""
    storage = storage_status(home_dir=home_dir, min_free_gb=200).as_dict()
    mount = mount_autopilot_report(
        min_free_gb=200,
        extra_roots=[home_dir] if home_dir else None,
        home_dir=home_dir,
    )
    runtime = runtime_status().as_dict()
    compatibility = compatibility_report(home_dir=home_dir)
    boot = boot_preflight_status(home_dir=home_dir, run_if_missing=False)
    smoke = smoke_test_report(home_dir=home_dir)
    model_fit = model_fit_report(home_dir=home_dir)
    receipts = receipt_summary(home_dir=home_dir)
    catalog = catalog_with_status()
    validated = _target_vm_validated(target_vm_validated)

    gates = [
        _storage_gate(storage),
        _mount_gate(mount, storage),
        _runtime_gate(runtime),
        _gpu_gate(compatibility),
        _compatibility_gate(compatibility),
        _boot_gate(boot),
        _smoke_gate(smoke),
        _model_gate(model_fit),
        _receipts_gate(receipts),
        _pack_safety_gate(catalog),
        _target_acceptance_gate(validated),
    ]
    blocked = [gate for gate in gates if gate.status == "blocked"]
    warnings = [gate for gate in gates if gate.status == "warn"]
    passed = [gate for gate in gates if gate.status == "pass"]
    pilot_ready = not blocked
    production_ready = pilot_ready and not warnings
    status = "production-ready" if production_ready else "pilot-ready" if pilot_ready else "blocked"

    if production_ready:
        summary = "Ready for production release on the validated target VM."
    elif pilot_ready:
        summary = "Ready for a controlled beta or target-VM acceptance run."
    else:
        summary = f"{len(blocked)} blocker(s) must be resolved before beta or production."

    next_actions = [
        gate.recommendation or gate.summary
        for gate in [*blocked, *warnings]
        if gate.recommendation or gate.summary
    ][:6]
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "status": status,
        "summary": summary,
        "pilot_ready": pilot_ready,
        "production_ready": production_ready,
        "target_vm_validated": validated,
        "counts": {
            "passed": len(passed),
            "warnings": len(warnings),
            "blocked": len(blocked),
            "total": len(gates),
        },
        "gates": [gate.as_dict() for gate in gates],
        "next_actions": next_actions,
        "target_vm_checklist": TARGET_VM_CHECKLIST,
        "inputs": {
            "home_dir": str(home_dir) if home_dir else None,
            "storage_home": storage.get("layout", {}).get("home"),
            "storage_configured_by": storage.get("configured_by"),
            "runtime_strategy": runtime.get("strategy"),
            "boot_checked_at": boot.get("checked_at"),
        },
    }
