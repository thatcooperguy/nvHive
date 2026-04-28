"""Tests for boot-time VM image preflight state."""

from __future__ import annotations

from nvh.integrations import boot_preflight


def _compatibility_report(*, kernel: str = "6.8.0", cuda: str = "12.4", agent_ready: bool = False) -> dict:
    return {
        "summary": "ready",
        "ready": True,
        "issue_count": 0,
        "blocked_count": 0,
        "rootless_fixable_count": 0,
        "recommended_torch_profile": "nvidia-cu121",
        "host": {
            "distro": "Ubuntu 24.04",
            "kernel": kernel,
            "machine": "x86_64",
            "libc": {"name": "glibc", "version": "2.35"},
            "python": {"version": "3.11.9", "strategy": "python-venv"},
            "gpu": {
                "name": "NVIDIA RTX",
                "memory_total_mb": "24576",
                "driver_version": "570.00",
                "cuda_version": cuda,
            },
            "commands": {
                "git": "/usr/bin/git",
                "curl": "/usr/bin/curl",
                "tar": "/usr/bin/tar",
                "node": "/usr/bin/node",
                "npm": "/usr/bin/npm",
            },
            "display": {"DISPLAY": ":0", "WAYLAND_DISPLAY": ""},
            "storage": {"layout": {"home": "/mnt/nvh"}},
        },
        "apps": [
            {
                "id": "agent-lab",
                "status": "ready" if agent_ready else "fixable",
                "recommended_action_id": None if agent_ready else "agent-lab",
                "requirements": [],
            }
        ],
    }


def test_boot_preflight_captures_baseline_and_agent_helper(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        boot_preflight,
        "compatibility_report",
        lambda home_dir=None: _compatibility_report(agent_ready=False),
    )
    monkeypatch.setattr(boot_preflight, "mount_autopilot_report", lambda: {"recommended": None})
    monkeypatch.setattr(boot_preflight, "auto_repair_plan", lambda home_dir=None: {"actions": []})
    monkeypatch.setattr(boot_preflight, "run_safe_repairs", lambda home_dir=None: {"completed": [], "plan": {"actions": []}})
    monkeypatch.setattr(boot_preflight, "smoke_test_report", lambda home_dir=None: {"summary": "ok"})
    monkeypatch.setattr(boot_preflight, "model_fit_report", lambda home_dir=None: {"summary": "ok", "recommended_ids": [], "detected_vram_gb": 0})

    report = boot_preflight.run_boot_preflight(home_dir=tmp_path / "nvh")

    assert report["first_run"] is True
    assert report["changed"] is False
    assert report["agent_helper"]["mode"] == "offline-deterministic"
    assert report["agent_helper"]["recommended_action_id"] == "agent-lab"


def test_boot_preflight_detects_image_drift(tmp_path, monkeypatch) -> None:
    reports = [
        _compatibility_report(kernel="6.8.0", cuda="12.4", agent_ready=True),
        _compatibility_report(kernel="6.10.0", cuda="13.0", agent_ready=True),
    ]
    monkeypatch.setattr(
        boot_preflight,
        "compatibility_report",
        lambda home_dir=None: reports.pop(0),
    )
    monkeypatch.setattr(boot_preflight, "mount_autopilot_report", lambda: {"recommended": None})
    monkeypatch.setattr(boot_preflight, "auto_repair_plan", lambda home_dir=None: {"actions": []})
    monkeypatch.setattr(boot_preflight, "run_safe_repairs", lambda home_dir=None: {"completed": [], "plan": {"actions": []}})
    monkeypatch.setattr(boot_preflight, "smoke_test_report", lambda home_dir=None: {"summary": "ok"})
    monkeypatch.setattr(boot_preflight, "model_fit_report", lambda home_dir=None: {"summary": "ok", "recommended_ids": [], "detected_vram_gb": 0})

    boot_preflight.run_boot_preflight(home_dir=tmp_path / "nvh")
    changed = boot_preflight.run_boot_preflight(home_dir=tmp_path / "nvh")

    change_ids = {change["id"] for change in changed["changes"]}
    assert changed["changed"] is True
    assert {"kernel", "cuda_version"}.issubset(change_ids)
    assert changed["agent_helper"]["mode"] == "local-agent-ready"
