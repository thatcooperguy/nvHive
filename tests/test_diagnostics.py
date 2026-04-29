"""Tests for redacted setup diagnostics reports."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from nvh.integrations import diagnostics


def test_diagnostics_report_redacts_log_secrets(monkeypatch) -> None:
    monkeypatch.setattr(
        diagnostics,
        "storage_layout",
        lambda home_dir=None: SimpleNamespace(
            home=Path("/persist/nvhive"),
            logs_dir=Path("/persist/nvhive/logs"),
            config_dir=Path("/persist/nvhive/config"),
            models_dir=Path("/persist/nvhive/models"),
            apps_dir=Path("/persist/nvhive/apps"),
        ),
    )
    monkeypatch.setattr(
        diagnostics,
        "storage_status",
        lambda home_dir=None: SimpleNamespace(as_dict=lambda: {"ok": True, "layout": {"home": "/persist/nvhive"}}),
    )
    monkeypatch.setattr(diagnostics, "_candidate_log_files", lambda logs_dir: [Path("/persist/nvhive/logs/nvhive.log")])
    monkeypatch.setattr(
        diagnostics,
        "_tail_log_file",
        lambda path, max_lines: [
            "WARNING failed request Authorization: Bearer abcdefghijklmnop",
            "ERROR provider returned sk-testsecret1234567890",
        ],
    )

    report = diagnostics.diagnostics_report(
        home_dir="/persist/nvhive",
        request_id="req-123",
        include_logs=True,
    )
    rendered = json.dumps(report)

    assert report["request_id"] == "req-123"
    assert report["paths"]["home"].replace("\\", "/") == "/persist/nvhive"
    assert "abcdefghijklmnop" not in rendered
    assert "sk-testsecret1234567890" not in rendered
    assert "[redacted]" in rendered


def test_diagnostics_report_survives_missing_logs(monkeypatch) -> None:
    monkeypatch.setattr(
        diagnostics,
        "storage_layout",
        lambda home_dir=None: SimpleNamespace(
            home=Path("/persist/nvhive"),
            logs_dir=Path("/persist/nvhive/logs"),
            config_dir=Path("/persist/nvhive/config"),
            models_dir=Path("/persist/nvhive/models"),
            apps_dir=Path("/persist/nvhive/apps"),
        ),
    )
    monkeypatch.setattr(
        diagnostics,
        "storage_status",
        lambda home_dir=None: SimpleNamespace(as_dict=lambda: {"ok": True, "layout": {"home": "/persist/nvhive"}}),
    )
    monkeypatch.setattr(diagnostics, "_candidate_log_files", lambda logs_dir: [])

    report = diagnostics.diagnostics_report(home_dir="/persist/nvhive", include_logs=True)

    assert report["report_id"].startswith("diag-")
    assert report["logs"]["included"] is True
    assert isinstance(report["checks"]["storage"], dict)
