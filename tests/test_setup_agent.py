"""Tests for the offline student setup helper."""

from __future__ import annotations

from nvh.integrations import setup_agent


def test_setup_helper_prioritizes_storage(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("NVH_HOME", raising=False)
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
