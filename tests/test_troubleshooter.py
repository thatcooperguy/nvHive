"""Tests for AI Wizard's rootless troubleshooting playbook."""

from __future__ import annotations

from nvh.integrations.wizard.troubleshooter import analyze_setup_failure


def _diagnostics_with_log(line: str) -> dict:
    return {
        "report_id": "diag-test",
        "paths": {"home": "/home/kiosk/nvhive"},
        "checks": {
            "jobs": {
                "ok": True,
                "data": {
                    "failed_event_tails": [
                        {
                            "kind": "studio-pack-install",
                            "status": "failed",
                            "message": line,
                            "events": [{"message": line}],
                        }
                    ]
                },
            },
            "local_ai_runtime": {
                "ok": True,
                "data": {
                    "summary": "Local AI runtime is not installed yet.",
                    "binary_error": line,
                },
            },
        },
        "logs": {
            "recent": [
                {
                    "path": "/home/kiosk/nvhive/logs/ollama-install.log",
                    "lines": [line],
                }
            ]
        },
    }


def test_troubleshooter_classifies_ollama_exec_format() -> None:
    report = analyze_setup_failure(
        _diagnostics_with_log("ERROR Exec format error: /home/kiosk/nvhive/bin/ollama"),
        home_dir="/home/kiosk/nvhive",
    )

    primary = report["primary_finding"]

    assert primary["id"] == "ollama-wrong-binary"
    assert primary["action_id"] == "rootless-ollama"
    assert primary["button_label"] == "Install Runtime"
    assert primary["can_auto_repair"] is True
    assert "sudo" in primary["rootless_note"]


def test_troubleshooter_classifies_ollama_404_download() -> None:
    report = analyze_setup_failure(
        _diagnostics_with_log("Download Ollama Linux amd64 bundle failed with exit code 22 curl: (22) 404"),
        home_dir="/home/kiosk/nvhive",
    )

    primary = report["primary_finding"]

    assert primary["id"] == "ollama-download-url"
    assert "Ollama" in primary["title"]
    assert any("ollama" in url for url in report["official_urls"])


def test_troubleshooter_classifies_python_venv_without_sudo() -> None:
    report = analyze_setup_failure(
        _diagnostics_with_log("The virtual environment was not created because ensurepip is not available. apt install python3.13-venv"),
        home_dir="/home/kiosk/nvhive",
    )

    primary = report["primary_finding"]

    assert primary["id"] == "python-venv-runtime"
    assert primary["action_id"] == "runtime-fallback"
    assert "Do not ask" in primary["rootless_note"]


def test_troubleshooter_classifies_micromamba_archive_race_as_runtime_retry() -> None:
    report = analyze_setup_failure(
        _diagnostics_with_log(
            "Micromamba fallback install failed: [Errno 2] No such file or directory: "
            "'$NVH_HOME/cache/downloads/micromamba-linux-64.tar.bz2'"
        ),
        home_dir="/home/kiosk/nvhive",
    )

    primary = report["primary_finding"]

    assert primary["id"] == "micromamba-runtime-retry"
    assert primary["action_id"] == "runtime-fallback"
    assert primary["button_label"] == "Install Runtime"
    assert primary["can_auto_repair"] is True


def test_troubleshooter_general_case_still_has_safe_action() -> None:
    report = analyze_setup_failure({"paths": {"home": "/home/kiosk/nvhive"}, "checks": {}, "logs": {"recent": []}})

    primary = report["primary_finding"]

    assert primary["id"] == "general-rootless-check"
    assert primary["button_label"] == "Fix My Setup"
    assert report["rootless"] is True
