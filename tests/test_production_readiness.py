"""Tests for conservative production readiness gates."""

from __future__ import annotations

from types import SimpleNamespace

from nvh.integrations import production_readiness


def _storage_status(*, ok: bool = True, configured_by: str = "argument", free_gb: float = 480.0):
    return SimpleNamespace(
        as_dict=lambda: {
            "ok": ok,
            "configured_by": configured_by,
            "free_gb": free_gb,
            "warnings": [] if ok else ["write probe failed"],
            "layout": {"home": "/mnt/persist/nvhive"},
        }
    )


def _runtime_status(strategy: str = "python-venv"):
    return SimpleNamespace(
        as_dict=lambda: {
            "strategy": strategy,
            "python_version": "3.11.9",
            "notes": ["ready"],
        }
    )


def _compatibility(system: str = "Linux", *, blocked_count: int = 0, issue_count: int = 0):
    return {
        "summary": "Host is ready" if not issue_count else "needs attention",
        "ready": issue_count == 0,
        "issue_count": issue_count,
        "blocked_count": blocked_count,
        "rootless_fixable_count": max(0, issue_count - blocked_count),
        "recommended_torch_profile": "nvidia-cu121",
        "host": {
            "system": system,
            "gpu": {
                "name": "NVIDIA RTX 4090" if system == "Linux" else "",
                "driver_version": "570.00",
                "cuda_version": "12.4",
            },
        },
        "facts": [],
        "apps": [],
    }


def _patch_common(monkeypatch, *, system: str = "Linux", storage_ok: bool = True, target_clean: bool = True) -> None:
    monkeypatch.setattr(
        production_readiness,
        "storage_status",
        lambda home_dir=None, min_free_gb=20: _storage_status(ok=storage_ok),
    )
    monkeypatch.setattr(
        production_readiness,
        "mount_autopilot_report",
        lambda min_free_gb=20, extra_roots=None, home_dir=None: {
            "confidence": "high",
            "current": {"ok": storage_ok, "configured_by": "argument"},
            "recommended": {
                "recommended_home": "/mnt/persist/nvhive",
                "writable": True,
                "read_only": False,
                "network_mount": False,
                "os_mount": False,
                "score": 120,
            },
        },
    )
    monkeypatch.setattr(production_readiness, "runtime_status", lambda: _runtime_status())
    monkeypatch.setattr(production_readiness, "compatibility_report", lambda home_dir=None: _compatibility(system))
    monkeypatch.setattr(
        production_readiness,
        "boot_preflight_status",
        lambda home_dir=None, run_if_missing=False: {
            "checked_at": "2026-04-28T00:00:00Z",
            "changed": False,
            "needs_attention": not target_clean,
            "summary": "Boot baseline steady",
        },
    )
    monkeypatch.setattr(
        production_readiness,
        "smoke_test_report",
        lambda home_dir=None: {"failed": 0, "warnings": 0, "summary": "8 passed"},
    )
    monkeypatch.setattr(
        production_readiness,
        "model_fit_report",
        lambda home_dir=None: {
            "storage_fits_queue": True,
            "summary": "3 model(s) queued",
        },
    )
    monkeypatch.setattr(production_readiness, "receipt_summary", lambda home_dir=None: {"count": 2, "unhealthy": 0})
    monkeypatch.setattr(
        production_readiness,
        "catalog_with_status",
        lambda: {"packs": [{"id": "rootless-ollama", "no_root": True}, {"id": "agent-lab", "no_root": True}]},
    )


def test_production_readiness_requires_target_vm_acceptance(monkeypatch) -> None:
    _patch_common(monkeypatch, system="Linux")

    report = production_readiness.production_readiness_report(target_vm_validated=False)
    gate_by_id = {gate["id"]: gate for gate in report["gates"]}

    assert report["pilot_ready"] is True
    assert report["production_ready"] is False
    assert report["status"] == "pilot-ready"
    assert gate_by_id["target-vm-acceptance"]["status"] == "warn"


def test_production_readiness_can_pass_after_target_validation(monkeypatch) -> None:
    _patch_common(monkeypatch, system="Linux")

    report = production_readiness.production_readiness_report(target_vm_validated=True)

    assert report["production_ready"] is True
    assert report["status"] == "production-ready"
    assert report["counts"]["blocked"] == 0
    assert report["counts"]["warnings"] == 0


def test_production_readiness_blocks_when_storage_or_model_queue_fails(monkeypatch) -> None:
    _patch_common(monkeypatch, system="Linux", storage_ok=False)
    monkeypatch.setattr(
        production_readiness,
        "model_fit_report",
        lambda home_dir=None: {
            "storage_fits_queue": False,
            "summary": "Queue needs more storage",
        },
    )

    report = production_readiness.production_readiness_report(target_vm_validated=True)
    blocked = {gate["id"] for gate in report["gates"] if gate["status"] == "blocked"}

    assert report["pilot_ready"] is False
    assert {"persistent-storage", "model-fit"}.issubset(blocked)


def test_production_readiness_on_non_target_host_is_pilot_only(monkeypatch) -> None:
    _patch_common(monkeypatch, system="Windows")

    report = production_readiness.production_readiness_report(target_vm_validated=False)
    gate_by_id = {gate["id"]: gate for gate in report["gates"]}

    assert report["pilot_ready"] is True
    assert report["production_ready"] is False
    assert gate_by_id["linux-gpu-session"]["status"] == "warn"
    assert gate_by_id["linux-gpu-session"]["source"] == "target-vm"
