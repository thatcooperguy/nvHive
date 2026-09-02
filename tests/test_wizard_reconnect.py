"""Tests for the wizard reconnect routine."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nvh.integrations.wizard import reconnect as reconnect_module
from nvh.integrations.wizard.reconnect import (
    _greeting,
    _label_for,
    _shape_auto_repair,
    _shape_changes,
    _survived_facts,
    wizard_reconnect,
)


def test_label_for_known_fact() -> None:
    assert _label_for("gpu_name") == "GPU"
    assert _label_for("cuda_version") == "CUDA driver API"


def test_label_for_unknown_fact_falls_back_to_title_case() -> None:
    assert _label_for("some_new_metric") == "Some New Metric"


def test_shape_changes_translates_facts_to_labels() -> None:
    raw = [
        {"fact": "driver_version", "previous": "550.x", "current": "555.x", "severity": "info"},
        {"fact": "cuda_version", "previous": None, "current": "12.6", "severity": "warning"},
    ]
    shaped = _shape_changes(raw)
    assert shaped[0]["label"] == "NVIDIA driver"
    assert shaped[0]["previous"] == "550.x"
    assert shaped[1]["label"] == "CUDA driver API"
    assert shaped[1]["previous"] == "—"
    assert shaped[1]["current"] == "12.6"


def test_shape_auto_repair_splits_completed_and_needs_user() -> None:
    repair = {
        "completed": [
            {"id": "storage-env-file", "title": "Rebuild env", "summary": "wrote"},
            {"id": "config-validate", "title": "Validate config", "summary": "ok"},
        ],
        "plan": {
            "actions": [
                {"id": "receipts", "title": "Receipts", "summary": "needs you", "status": "needs-user", "button_action_id": "repair-receipts"},
                {"id": "storage-env-file", "title": "Already done", "status": "completed"},
            ],
        },
    }
    auto, needs = _shape_auto_repair(repair)
    assert {a["id"] for a in auto} == {"storage-env-file", "config-validate"}
    assert len(needs) == 1
    assert needs[0]["id"] == "receipts"
    assert needs[0]["button_action_id"] == "repair-receipts"


def test_shape_auto_repair_handles_none() -> None:
    auto, needs = _shape_auto_repair(None)
    assert auto == []
    assert needs == []


def test_survived_facts_filters_out_changed_keys() -> None:
    fingerprint = {
        "gpu_name": "RTX 5090",
        "gpu_memory_total_mb": 32768,
        "cuda_version": "12.6",
        "driver_version": "555.x",
    }
    changes = [{"fact": "cuda_version", "previous": "12.4", "current": "12.6"}]
    survived = _survived_facts(fingerprint, changes)
    keys = {entry["label"] for entry in survived}
    assert "GPU" in keys
    assert "NVIDIA driver" in keys
    assert "CUDA driver API" not in keys


def test_shape_changes_accepts_boot_preflight_diff_shape() -> None:
    """``diff_fingerprints`` emits id/before/after — the panel must read that shape too."""
    raw = [
        {"id": "machine", "label": "Architecture", "before": "x86_64", "after": "aarch64", "severity": "required"},
        {"id": "cuda_version", "before": "missing", "after": "13.0", "severity": "required"},
    ]
    shaped = _shape_changes(raw)
    assert shaped[0] == {
        "fact": "machine", "label": "Architecture", "previous": "x86_64", "current": "aarch64", "severity": "required",
    }
    assert shaped[1]["label"] == "CUDA driver API"
    assert shaped[1]["previous"] == "—"
    assert shaped[1]["current"] == "13.0"


def test_survived_facts_excludes_facts_changed_in_preflight_shape() -> None:
    """A fact that drifted (preflight shape) must not also be listed as survived."""
    fingerprint = {"machine": "aarch64", "gpu_name": "NVIDIA GB10"}
    survived = _survived_facts(fingerprint, [{"id": "machine", "before": "x86_64", "after": "aarch64"}])
    labels = {row["label"] for row in survived}
    assert "GPU" in labels
    assert "Architecture" not in labels


def test_wizard_reconnect_renders_architecture_from_preflight_fingerprint() -> None:
    """W4: run_boot_preflight returns ``fingerprint`` at top level; ``machine`` reaches the panel."""
    fake_preflight = {
        "first_run": False,
        "changed": False,
        "changes": [],
        "auto_repair": {"completed": [], "plan": {"actions": []}},
        "fingerprint": {"machine": "aarch64", "gpu_name": "NVIDIA GB10", "storage_home": "/home/nvidia/.nvh"},
        "compatibility": {"ready": True},
    }
    with patch.object(reconnect_module, "run_boot_preflight", return_value=fake_preflight):
        result = wizard_reconnect()

    survived = {item["label"]: item["value"] for item in result["what_survived"]}
    assert survived["Architecture"] == "aarch64"
    assert survived["GPU"] == "NVIDIA GB10"
    assert survived["Workspace path"] == "/home/nvidia/.nvh"


def test_wizard_reconnect_derives_fingerprint_from_compatibility_host() -> None:
    """No fingerprint anywhere in the result: derive it from ``compatibility.host`` with the
    same helper boot_preflight uses, so the panel never silently goes empty."""
    fake_preflight = {
        "first_run": False,
        "changed": False,
        "changes": [],
        "auto_repair": None,
        "compatibility": {
            "host": {
                "machine": "aarch64",
                "gpu": {"name": "NVIDIA GB10", "memory_total_mb": "131072"},
                "storage": {"layout": {"home": "/home/nvidia/.nvh"}},
            },
        },
    }
    with patch.object(reconnect_module, "run_boot_preflight", return_value=fake_preflight):
        result = wizard_reconnect()

    survived = {item["label"]: item["value"] for item in result["what_survived"]}
    assert survived["Architecture"] == "aarch64"
    assert survived["GPU"] == "NVIDIA GB10"
    assert survived["Workspace path"] == "/home/nvidia/.nvh"


def test_run_boot_preflight_result_carries_fingerprint_with_machine(tmp_path, monkeypatch) -> None:
    """End to end through the real preflight (heavy helpers stubbed): Architecture renders."""
    from nvh.integrations.diagnostics import boot_preflight

    report = {
        "summary": "ready", "ready": True, "issue_count": 0, "blocked_count": 0,
        "recommended_torch_profile": "nvidia-cu130",
        "host": {
            "distro": "Ubuntu 24.04.2 LTS (DGX OS 7)",
            "kernel": "6.11.0-1004-nvidia",
            "machine": "aarch64",
            "libc": {"name": "glibc", "version": "2.39"},
            "python": {"version": "3.12.3", "strategy": "python-venv"},
            "gpu": {
                "name": "NVIDIA GB10", "memory_total_mb": "131072", "driver_version": "580.65",
                "cuda_version": "13.0", "compute_capability": "12.1", "architecture": "Blackwell",
                "detection_status": "ready",
            },
            "commands": {"git": "/usr/bin/git", "curl": "/usr/bin/curl", "tar": "/usr/bin/tar", "node": "", "npm": ""},
            "command_versions": {},
            "display": {"DISPLAY": ":0", "WAYLAND_DISPLAY": ""},
            "storage": {"configured_by": "argument", "total_gb": 1000.0, "write_probe_ok": True,
                        "layout": {"home": "/home/nvidia/.nvh"}},
        },
        "apps": [],
    }
    monkeypatch.setattr(boot_preflight, "compatibility_report", lambda home_dir=None: report)
    monkeypatch.setattr(boot_preflight, "mount_autopilot_report", lambda home_dir=None: {"recommended": None})
    monkeypatch.setattr(boot_preflight, "auto_repair_plan", lambda home_dir=None: {"actions": []})
    monkeypatch.setattr(boot_preflight, "run_safe_repairs", lambda home_dir=None: {"completed": [], "plan": {"actions": []}})
    monkeypatch.setattr(boot_preflight, "smoke_test_report", lambda home_dir=None: {"summary": "ok"})
    monkeypatch.setattr(
        boot_preflight, "model_fit_report",
        lambda home_dir=None: {"summary": "ok", "recommended_ids": [], "detected_vram_gb": 0},
    )

    result = boot_preflight.run_boot_preflight(home_dir=tmp_path / "nvh")
    assert result["fingerprint"]["machine"] == "aarch64"
    assert result["fingerprint_id"] == boot_preflight.fingerprint_id(result["fingerprint"])

    with patch.object(reconnect_module, "run_boot_preflight", return_value=result):
        panel = wizard_reconnect()
    survived = {item["label"]: item["value"] for item in panel["what_survived"]}
    assert survived["Architecture"] == "aarch64"
    assert survived["GPU"] == "NVIDIA GB10"


def test_greeting_first_run() -> None:
    msg = _greeting(first_run=True, change_count=0, auto_count=0, needs_user_count=0)
    assert "Welcome" in msg and "initializing" in msg


def test_greeting_first_run_with_attention() -> None:
    msg = _greeting(first_run=True, change_count=0, auto_count=0, needs_user_count=2)
    assert "2 item" in msg


def test_greeting_happy_reconnect() -> None:
    msg = _greeting(first_run=False, change_count=0, auto_count=0, needs_user_count=0)
    assert "survived" in msg and "healthy" in msg


def test_greeting_reconnect_with_auto_repairs() -> None:
    msg = _greeting(first_run=False, change_count=0, auto_count=2, needs_user_count=0)
    assert "2 safe repair" in msg


def test_greeting_reconnect_with_changes() -> None:
    msg = _greeting(first_run=False, change_count=3, auto_count=1, needs_user_count=1)
    assert "3 change" in msg and "1 safe repair" in msg and "1 item" in msg


def test_wizard_reconnect_first_run_shape() -> None:
    fake_preflight = {
        "first_run": True,
        "changed": False,
        "changes": [],
        "auto_repair": {
            "completed": [],
            "plan": {"actions": []},
        },
        "compatibility": {"fingerprint": {"gpu_name": "RTX 5090", "storage_total_gb": 500}},
    }
    with patch.object(reconnect_module, "run_boot_preflight", return_value=fake_preflight):
        result = wizard_reconnect()

    assert result["first_run"] is True
    assert result["changed"] is False
    assert result["needs_attention"] == []
    assert result["auto_repaired"] == []
    assert result["what_changed"] == []
    assert "Welcome" in result["summary"]
    assert "elapsed_ms" in result
    # Survived facts should pick up the high-signal keys from the fingerprint.
    survived_labels = {item["label"] for item in result["what_survived"]}
    assert "GPU" in survived_labels


@pytest.mark.parametrize("change_count, auto_count, needs_count", [
    (0, 0, 0),
    (2, 1, 0),
    (0, 1, 1),
    (3, 0, 2),
])
def test_wizard_reconnect_changed_run_includes_counts(
    change_count: int, auto_count: int, needs_count: int,
) -> None:
    changes = [
        {"fact": f"fact_{i}", "previous": "a", "current": "b", "severity": "info"}
        for i in range(change_count)
    ]
    fake_preflight = {
        "first_run": False,
        "changed": change_count > 0,
        "changes": changes,
        "auto_repair": {
            "completed": [
                {"id": f"r_{i}", "title": f"Repair {i}", "summary": "ok"}
                for i in range(auto_count)
            ],
            "plan": {
                "actions": [
                    {
                        "id": f"u_{i}",
                        "title": f"User {i}",
                        "summary": "needs you",
                        "status": "needs-user",
                        "button_action_id": "btn",
                    }
                    for i in range(needs_count)
                ],
            },
        },
        "compatibility": {"fingerprint": {}},
    }
    with patch.object(reconnect_module, "run_boot_preflight", return_value=fake_preflight):
        result = wizard_reconnect()

    assert len(result["what_changed"]) == change_count
    assert len(result["auto_repaired"]) == auto_count
    assert len(result["needs_attention"]) == needs_count
    if change_count == 0 and needs_count == 0 and auto_count == 0:
        assert "survived" in result["summary"]


# ---------------------------------------------------------------------------
# S6: the label boot_preflight.diff_fingerprints supplies is preferred
# ---------------------------------------------------------------------------


def test_shape_changes_prefers_the_preflight_label() -> None:
    """boot_preflight's _FACT_LABELS knows preflight-only facts; reconnect's smaller
    table does not, so re-deriving the label produced "Gpu Detection Status"."""
    raw = [
        {"id": "gpu_detection_status", "label": "GPU detection", "before": "ready", "after": "blocked", "severity": "required"},
        {"id": "gpu_name", "before": "RTX 4090", "after": "RTX 5090", "severity": "recommended"},  # no label → own table
        {"id": "brand_new_fact", "before": "a", "after": "b"},                                    # unknown → title case
        {"id": "libc", "label": "   ", "before": "glibc 2.35", "after": "glibc 2.39"},             # blank label → fallback
        {"fact": "driver_version", "label": "NVIDIA driver (preflight)", "previous": "550", "current": "555"},
    ]
    shaped = _shape_changes(raw)
    assert [row["label"] for row in shaped] == [
        "GPU detection", "GPU", "Brand New Fact", "Libc", "NVIDIA driver (preflight)",
    ]
    assert (shaped[0]["previous"], shaped[0]["current"], shaped[0]["severity"]) == ("ready", "blocked", "required")


def test_wizard_reconnect_carries_diff_fingerprints_labels_end_to_end() -> None:
    from nvh.integrations.diagnostics.boot_preflight import diff_fingerprints

    changes = diff_fingerprints(
        {"gpu_detection_status": "ready", "python_strategy": "python-venv", "gpu_name": "NVIDIA GB10"},
        {"gpu_detection_status": "blocked", "python_strategy": "uv", "gpu_name": "NVIDIA GB10"},
    )
    fake_preflight = {
        "first_run": False,
        "changed": True,
        "changes": changes,
        "auto_repair": {"completed": [], "plan": {"actions": []}},
        "compatibility": {"fingerprint": {"gpu_name": "NVIDIA GB10"}},
    }
    with patch.object(reconnect_module, "run_boot_preflight", return_value=fake_preflight):
        result = wizard_reconnect()

    labels = {row["fact"]: row["label"] for row in result["what_changed"]}
    assert labels == {"gpu_detection_status": "GPU detection", "python_strategy": "Python runtime"}
    assert {row["label"] for row in result["what_survived"]} == {"GPU"}
