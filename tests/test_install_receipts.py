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


def test_receipts_honor_explicit_home_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "wrong-home"))
    selected_home = tmp_path / "selected-home"
    install_path = selected_home / "studio" / "packs" / "music-producer-lab"
    install_path.mkdir(parents=True)

    receipts.write_receipt(
        kind="studio-pack",
        item_id="music-producer-lab",
        title="Music Producer Lab",
        install_path=install_path,
        home_dir=selected_home,
    )

    assert receipts.receipt_summary()["count"] == 0
    scoped = receipts.receipt_summary(home_dir=selected_home)
    assert scoped["count"] == 1
    assert scoped["receipts"][0]["id"] == "studio-pack:music-producer-lab"
    plan = receipts.repair_plan("studio-pack:music-producer-lab", home_dir=selected_home)
    assert plan["commands"] == ["nvh studio --install music-producer-lab -y"]


def test_receipt_path_is_public_and_matches_the_writer(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    assert not receipts.receipt_path("playbook", "tailscale").exists()
    receipts.write_receipt(kind="playbook", item_id="tailscale", title="Tailscale", install_path=tmp_path)
    path = receipts.receipt_path("playbook", "tailscale")
    assert path.exists() and path.name == "playbook_tailscale.json"
    assert receipts.receipt_path("playbook", "tailscale", home_dir=tmp_path / "other") != path


def test_repair_and_uninstall_plans_derive_the_root_flag_from_the_receipt(tmp_path, monkeypatch) -> None:
    """A Spark playbook whose sudo steps ran says ``no_root: False``; the plans say so too
    instead of hard-coding "safe without root". Rootless kinds are unchanged."""
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    undo = ["sudo tailscale down", "sudo apt remove --purge tailscale", "sudo rm /etc/apt/sources.list.d/tailscale.list"]
    receipts.write_receipt(
        kind="playbook",
        item_id="tailscale",
        title="Set up Tailscale on Your Spark",
        install_path=tmp_path / "nvh" / "playbooks" / "tailscale",
        no_root=False,
        metadata={"undo": undo},
    )
    repair = receipts.repair_plan("playbook:tailscale")
    assert repair["commands"] == ["nvh playbook install tailscale"]
    assert repair["safe_to_run_without_root"] is False and "sudo" in repair["reason"]

    uninstall = receipts.uninstall_plan("playbook:tailscale")
    assert uninstall["commands"] == undo  # the upstream cleanup, preview only
    assert uninstall["safe_to_run_without_root"] is False and uninstall["destructive"] is True
    assert uninstall["target_paths"] == [str(tmp_path / "nvh" / "playbooks" / "tailscale")]
    assert "never runs them" in uninstall["reason"]

    # A user-space playbook (no sudo step ran) and a studio pack keep the rootless answer.
    receipts.write_receipt(kind="playbook", item_id="lm-studio", title="LM Studio", install_path=tmp_path / "lm")
    assert receipts.repair_plan("playbook:lm-studio")["safe_to_run_without_root"] is True
    assert receipts.uninstall_plan("playbook:lm-studio")["commands"] == [f"rm -rf {str(tmp_path / 'lm')!r}"]
    receipts.write_receipt(kind="studio-pack", item_id="agent-lab", title="Agent Lab", install_path=tmp_path / "packs")
    assert receipts.repair_plan("studio-pack:agent-lab")["safe_to_run_without_root"] is True
    assert receipts.uninstall_plan("studio-pack:agent-lab")["safe_to_run_without_root"] is True
