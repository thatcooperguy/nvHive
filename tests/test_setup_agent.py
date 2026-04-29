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
        lambda: {"installed": False, "examples_installed": False},
    )

    report = setup_agent.setup_helper_report(home_dir=tmp_path / "nvh")
    actions = report["actions"]

    assert report["ready"] is True
    assert actions
    assert actions[0]["id"] in {"runtime-fallback", "rootless-ollama", "comfyui", "creative-tools"}


def test_setup_helper_flags_default_storage(monkeypatch) -> None:
    monkeypatch.delenv("NVH_HOME", raising=False)

    report = setup_agent.setup_helper_report()

    assert report["ready"] is False
    assert report["actions"][0]["id"] == "storage"
    assert report["assistant"]["mode"] == "offline-deterministic"


def test_setup_assistant_answers_comfyui_question(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    monkeypatch.setattr(
        setup_agent,
        "detect_comfyui",
        lambda: {"installed": False, "examples_installed": False},
    )

    reply = setup_agent.setup_assistant_reply("How do I install ComfyUI?", tmp_path / "nvh")

    assert reply["focus"] == "comfyui"
    assert "ComfyUI" in reply["answer"]
    assert reply["commands"]


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
        lambda: {"installed": True, "examples_installed": True},
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
