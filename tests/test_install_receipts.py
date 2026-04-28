"""Tests for rootless install receipts."""

from __future__ import annotations

from pathlib import Path

from nvh.integrations import receipts


def test_write_list_and_repair_receipt(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    install_path = tmp_path / "nvh" / "studio" / "packs" / "agent-lab"
    install_path.mkdir(parents=True)
    launcher = tmp_path / "nvh" / "bin" / "nvhive-agent-lab"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    receipt = receipts.write_receipt(
        kind="studio-pack",
        item_id="agent-lab",
        title="Agent Lab",
        install_path=install_path,
        launchers=[str(launcher)],
        source_urls=["https://example.test/agent-lab"],
    )

    assert receipt["id"] == "studio-pack:agent-lab"
    assert receipt["health"]["healthy"] is True

    listed = receipts.list_receipts()
    assert [item["id"] for item in listed] == ["studio-pack:agent-lab"]

    plan = receipts.repair_plan("studio-pack:agent-lab")
    assert plan["safe_to_run_without_root"] is True
    assert plan["commands"] == ["nvh studio --install agent-lab -y"]


def test_uninstall_plan_is_preview_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    target = Path(tmp_path / "nvh" / "comfyui" / "ComfyUI")
    target.mkdir(parents=True)

    receipts.write_receipt(
        kind="comfyui",
        item_id="workspace",
        title="ComfyUI Workspace",
        install_path=target,
    )
    plan = receipts.uninstall_plan("comfyui:workspace")

    assert plan["destructive"] is True
    assert str(target) in plan["target_paths"]
    assert target.exists()
