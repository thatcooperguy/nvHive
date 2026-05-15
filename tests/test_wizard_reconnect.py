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
