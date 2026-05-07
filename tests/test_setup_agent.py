"""Tests for the offline student setup helper."""

from __future__ import annotations

from types import SimpleNamespace

from nvh.integrations import receipts, setup_agent


def test_setup_helper_prioritizes_storage(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("NVH_HOME", raising=False)
    storage = SimpleNamespace(
        ok=True,
        configured_by="argument",
        as_dict=lambda: {
            "ok": True,
            "configured_by": "argument",
            "layout": {"home": str(tmp_path / "nvh")},
        },
    )
    monkeypatch.setattr(setup_agent, "storage_status", lambda **_: storage)
    monkeypatch.setattr(
        setup_agent,
        "model_catalog_with_status",
        lambda: {"models": []},
    )
    monkeypatch.setattr(
        setup_agent,
        "detect_comfyui",
        lambda **_: {"installed": False, "examples_installed": False},
    )

    report = setup_agent.setup_helper_report(home_dir=tmp_path / "nvh")
    actions = report["actions"]

    assert report["ready"] is True
    assert actions
    assert actions[0]["id"] in {"runtime-fallback", "rootless-ollama", "comfyui", "creative-tools"}


def test_setup_helper_flags_default_storage(monkeypatch) -> None:
    monkeypatch.delenv("NVH_HOME", raising=False)
    monkeypatch.delenv("NVHIVE_HOME", raising=False)

    report = setup_agent.setup_helper_report()

    assert report["ready"] is False
    assert report["actions"][0]["id"] == "storage"
    assert report["assistant"]["mode"] == "offline-deterministic"
    assert report["assistant"]["product"] == "nvHive / nvWizard"
    assert report["assistant"]["official_repo_url"].endswith("/thatcooperguy/nvHive")
    assert "rootless NVIDIA AI lab" in report["assistant"]["system_prompt"]


def test_setup_assistant_answers_comfyui_question(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    monkeypatch.setattr(
        setup_agent,
        "detect_comfyui",
        lambda **_: {"installed": False, "examples_installed": False},
    )

    reply = setup_agent.setup_assistant_reply("How do I install ComfyUI?", tmp_path / "nvh")

    assert reply["focus"] == "comfyui"
    assert "ComfyUI" in reply["answer"]
    assert reply["commands"]


def test_setup_assistant_explains_product_and_repo(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))

    reply = setup_agent.setup_assistant_reply("What is nvHive and where is the README?", tmp_path / "nvh")

    assert reply["focus"] == "product"
    assert "rootless NVIDIA AI lab" in reply["answer"]
    assert reply["official_repo_url"].endswith("/thatcooperguy/nvHive")
    assert "README.md" in reply["readme_url"]
    assert reply["commands"] == []
    assert any("product brief" in source for source in reply["grounding_sources"])


def test_setup_assistant_answers_student_boundary_questions(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))

    mission = setup_agent.setup_assistant_reply("Which mission should I pick?", tmp_path / "nvh")
    privacy = setup_agent.setup_assistant_reply("Will my data leave this VM or cost money?", tmp_path / "nvh")
    admin = setup_agent.setup_assistant_reply("Can you fix nvidia-smi without sudo?", tmp_path / "nvh")
    persistence = setup_agent.setup_assistant_reply("What survives after reconnect?", tmp_path / "nvh")

    assert mission["focus"] == "mission-choice"
    assert "AI Starter" in mission["answer"]
    assert privacy["focus"] == "privacy-cost"
    assert "Cloud API keys" in privacy["answer"]
    assert admin["focus"] == "admin-boundary"
    assert "provider or admin" in admin["answer"]
    assert persistence["focus"] == "persistence"
    assert str(tmp_path / "nvh") in persistence["answer"]


def test_setup_assistant_debugs_failed_jobs_and_logs(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    monkeypatch.setattr(
        setup_agent,
        "setup_helper_report",
        lambda home_dir=None: {
            "ready": False,
            "summary": "1 setup item needs attention",
            "actions": [
                {
                    "id": "rootless-ollama",
                    "title": "Install rootless Ollama",
                    "priority": 10,
                    "status": "recommended",
                    "command": "nvh studio --install rootless-ollama -y",
                    "reason": "Local model runtime is missing.",
                    "can_run_without_root": True,
                },
            ],
            "receipts": {"count": 0, "unhealthy": 0, "receipts": []},
            "assistant": {
                "product": "nvHive / nvWizard",
                "grounding_sources": ["local setup helper report", "install job logs"],
            },
        },
    )
    monkeypatch.setattr(
        setup_agent,
        "_recent_failed_job",
        lambda home_dir=None: {
            "kind": "studio-pack-install",
            "title": "Build AI Starter",
            "status": "failed",
            "message": "Exec format error: /home/kiosk/nvhive/bin/ollama",
        },
    )
    monkeypatch.setattr(
        setup_agent,
        "_safe_diagnostics_report",
        lambda home_dir=None: {
            "report_id": "diag-test",
            "checks": {
                "jobs": {
                    "ok": True,
                    "data": {
                        "failed_event_tails": [
                            {
                                "kind": "studio-pack-install",
                                "status": "failed",
                                "message": "Exec format error: /home/kiosk/nvhive/bin/ollama",
                                "events": [{"message": "Exec format error: /home/kiosk/nvhive/bin/ollama"}],
                            },
                        ],
                    },
                },
            },
            "logs": {
                "recent": [
                    {
                        "path": "/tmp/api.log",
                        "lines": ["ERROR Exec format error: /home/kiosk/nvhive/bin/ollama"],
                    },
                ],
            },
        },
    )

    reply = setup_agent.setup_assistant_reply("What broke in the logs?", tmp_path / "nvh")

    assert reply["focus"] == "debugger"
    assert reply["diagnostics_report_id"] == "diag-test"
    assert "bad or wrong-architecture Ollama binary" in reply["answer"]
    assert reply["debug_findings"]
    assert reply["log_highlights"]
    assert reply["commands"] == ["nvh studio --install rootless-ollama -y"]


def test_setup_helper_surfaces_unhealthy_receipt(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    storage = SimpleNamespace(
        ok=True,
        configured_by="argument",
        as_dict=lambda: {
            "ok": True,
            "configured_by": "argument",
            "layout": {"home": str(tmp_path / "nvh")},
        },
    )
    runtime = SimpleNamespace(
        strategy="python-venv",
        as_dict=lambda: {"strategy": "python-venv"},
    )
    monkeypatch.setattr(setup_agent, "storage_status", lambda **_: storage)
    monkeypatch.setattr(setup_agent, "runtime_status", lambda: runtime)
    monkeypatch.setattr(
        setup_agent,
        "catalog_with_status",
        lambda: {
            "packs": [
                {"id": "rootless-ollama", "status": {"installed": True}},
                {"id": "blender-creative", "status": {"installed": True}},
            ],
        },
    )
    monkeypatch.setattr(setup_agent, "model_catalog_with_status", lambda: {"models": []})
    monkeypatch.setattr(
        setup_agent,
        "detect_comfyui",
        lambda **_: {"installed": True, "examples_installed": True},
    )
    receipts.write_receipt(
        kind="studio-pack",
        item_id="agent-lab",
        title="Agent Lab",
        install_path=tmp_path / "missing-agent-lab",
        launchers=[str(tmp_path / "missing-agent-lab" / "nvhive-agent-lab")],
    )

    report = setup_agent.setup_helper_report(home_dir=tmp_path / "nvh")

    assert report["issue_count"] >= 1
    assert any(issue["id"] == "receipt:studio-pack:agent-lab" for issue in report["issues"])
    assert any(action["id"] == "repair-receipt:studio-pack:agent-lab" for action in report["actions"])
