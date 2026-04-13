"""Tests for rollback checkpoints and drift detection."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from nvh.core.drift_detector import DriftAlert, check_for_drift, format_drift_alerts
from nvh.core.rollback import (
    create_checkpoint,
    list_checkpoints,
    load_checkpoint,
    rollback_to_checkpoint,
    save_checkpoint,
)


def test_checkpoint_creation(tmp_path: Path) -> None:
    f1 = tmp_path / "a.txt"
    f1.write_text("hello", encoding="utf-8")
    cp = create_checkpoint(tmp_path, [str(f1), str(tmp_path / "missing.txt")])
    assert cp.files_snapshot[str(f1)] == "hello"
    assert cp.files_snapshot[str(tmp_path / "missing.txt")] is None
    assert cp.checkpoint_id


def test_rollback_restores_files(tmp_path: Path) -> None:
    f1 = tmp_path / "a.txt"
    f1.write_text("original", encoding="utf-8")
    cp = create_checkpoint(tmp_path, [str(f1)])
    f1.write_text("modified", encoding="utf-8")
    result = rollback_to_checkpoint(cp)
    assert str(f1) in result.restored
    assert f1.read_text(encoding="utf-8") == "original"


def test_rollback_deletes_new_files(tmp_path: Path) -> None:
    new_file = tmp_path / "new.txt"
    cp = create_checkpoint(tmp_path, [str(new_file)])
    new_file.write_text("should be deleted", encoding="utf-8")
    result = rollback_to_checkpoint(cp)
    assert str(new_file) in result.deleted
    assert not new_file.exists()


def test_drift_alert_construction() -> None:
    alert = DriftAlert(
        provider="openai",
        task_type="code",
        previous_score=0.9,
        current_score=0.6,
        drop_pct=33.3,
        recommendation="Consider routing code tasks away from openai",
    )
    assert alert.provider == "openai"
    assert alert.drop_pct == 33.3


def test_format_drift_alerts_non_empty() -> None:
    alert = DriftAlert(
        provider="anthropic",
        task_type="analysis",
        previous_score=0.85,
        current_score=0.60,
        drop_pct=29.4,
        recommendation="Consider routing analysis tasks away from anthropic",
    )
    output = format_drift_alerts([alert])
    assert "Drift Alerts" in output
    assert "anthropic" in output
    assert "29.4%" in output


def test_checkpoint_save_load_roundtrip(tmp_path: Path) -> None:
    f1 = tmp_path / "data.txt"
    f1.write_text("content", encoding="utf-8")
    cp = create_checkpoint(tmp_path, [str(f1)], description="test save")
    save_checkpoint(cp, tmp_path)
    loaded = load_checkpoint(
        tmp_path / ".nvhive" / "checkpoints" / f"{cp.checkpoint_id}.json",
    )
    assert loaded.checkpoint_id == cp.checkpoint_id
    assert loaded.files_snapshot == cp.files_snapshot
    assert loaded.description == "test save"


def test_list_checkpoints_order(tmp_path: Path) -> None:
    cp1 = create_checkpoint(tmp_path, [], description="first")
    cp1.timestamp = 1.0
    save_checkpoint(cp1, tmp_path)
    cp2 = create_checkpoint(tmp_path, [], description="second")
    cp2.timestamp = 2.0
    save_checkpoint(cp2, tmp_path)
    result = list_checkpoints(tmp_path)
    assert len(result) == 2
    assert result[0].description == "second"


def test_check_for_drift_detects_drop() -> None:
    engine = SimpleNamespace(score_history={
        ("openai", "code"): [0.9] * 20 + [0.5] * 10,
    })
    alerts = check_for_drift(engine)
    assert len(alerts) == 1
    assert alerts[0].provider == "openai"
    assert alerts[0].drop_pct > 20
