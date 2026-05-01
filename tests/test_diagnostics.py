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
    assert report["paths"]["home"] == "$NVH_HOME"
    assert "abcdefghijklmnop" not in rendered
    assert "sk-testsecret1234567890" not in rendered
    assert "/persist/nvhive" not in rendered
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


def test_diagnostics_report_uses_requested_home_for_jobs_receipts_and_logs(tmp_path, monkeypatch) -> None:
    home = tmp_path / "student-volume" / "nvhive"
    logs = home / "logs"
    jobs = home / "jobs"
    receipts = home / "receipts"
    logs.mkdir(parents=True)
    jobs.mkdir()
    receipts.mkdir()

    (logs / "api.log").write_text(
        f"ERROR failed with hf_secretToken123 at {home / 'models'}\n",
        encoding="utf-8",
    )
    job_id = "job-failed"
    (jobs / f"{job_id}.json").write_text(
        json.dumps(
            {
                "id": job_id,
                "kind": "comfyui-install",
                "title": "Install ComfyUI",
                "status": "failed",
                "message": f"Failed at {home}",
                "progress": 20,
                "request": {},
                "storage_home": str(home),
                "created_at": "2026-05-01T00:00:00Z",
                "updated_at": "2026-05-01T00:00:01Z",
                "started_at": "2026-05-01T00:00:00Z",
                "completed_at": "2026-05-01T00:00:01Z",
                "event_count": 1,
                "cancel_requested": False,
                "events_path": str(jobs / f"{job_id}.jsonl"),
            }
        ),
        encoding="utf-8",
    )
    (jobs / f"{job_id}.jsonl").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "sequence": 1,
                "timestamp": "2026-05-01T00:00:01Z",
                "event": "error",
                "status": "failed",
                "message": f"Bearer abcdefghijklmnop failed under {home}",
                "payload": {"token": "hf_nestedSecret1234", "path": str(home / "apps")},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (receipts / "studio-pack_rootless-ollama.json").write_text(
        json.dumps(
            {
                "id": "studio-pack:rootless-ollama",
                "kind": "studio-pack",
                "item_id": "rootless-ollama",
                "title": "Rootless Ollama",
                "status": "installed",
                "installed_at": "2026-05-01T00:00:00Z",
                "updated_at": "2026-05-01T00:00:00Z",
                "install_path": str(home / "studio" / "rootless-ollama"),
                "source_urls": [],
                "launchers": [],
                "models": [],
                "files": [],
                "no_root": True,
                "metadata": {},
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        diagnostics,
        "storage_status",
        lambda home_dir=None: SimpleNamespace(
            as_dict=lambda: {"ok": True, "layout": {"home": str(home)}}
        ),
    )
    from nvh.integrations import production_readiness

    monkeypatch.setattr(
        production_readiness,
        "production_readiness_report",
        lambda home_dir=None: {"status": "pilot-ready", "pilot_ready": True},
    )

    report = diagnostics.diagnostics_report(home_dir=home, include_logs=True)
    rendered = json.dumps(report)

    assert str(home) not in rendered
    assert "$NVH_HOME" in rendered
    assert "abcdefghijklmnop" not in rendered
    assert "hf_secretToken123" not in rendered
    jobs_data = report["checks"]["jobs"]["data"]
    assert jobs_data["failed_or_interrupted"] == 1
    assert jobs_data["failed_event_tails"][0]["job_id"] == job_id
    assert report["checks"]["receipts"]["data"]["count"] == 1
