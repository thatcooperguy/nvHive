"""Tests for rootless AI Studio pack helpers."""

from __future__ import annotations

import inspect
import subprocess
import sys

import pytest

from nvh.integrations import studio_packs


def test_catalog_is_rootless_and_grouped() -> None:
    catalog = studio_packs.catalog_as_dicts()
    ids = {pack["id"] for pack in catalog}

    assert "rootless-ollama" in ids
    assert "llm-starter" in ids
    assert "agent-lab" in ids
    assert "nvidia-omni-agent" in ids
    assert "openclaw-agent" in ids
    assert "nemoclaw-sandbox" in ids
    assert "comfyui-power-nodes" in ids
    assert "game-dev-lab" in ids
    assert "blender-creative" in ids
    assert "godot-engine" in ids
    assert "unity-hub-helper" in ids
    assert "unreal-engine-helper" in ids
    assert "github-login-helper" in ids
    assert "ace-step-music" in ids
    assert "music-producer-lab" in ids
    assert "music-daw-helper" in ids
    assert all(pack["no_root"] for pack in catalog)


def test_pack_bundles_expand_without_duplicates() -> None:
    starter = studio_packs.expand_pack_ids(["starter"])

    assert starter[0] == "rootless-ollama"
    assert "llm-starter" in starter
    assert "agent-lab" in starter
    assert "nvidia-omni-agent" in starter
    assert len(starter) == len(set(starter))
    assert "github-login-helper" in starter

    creative = studio_packs.expand_pack_ids(["creative"])
    assert creative == ["blender-creative", "game-dev-lab", "game-mod-helper", "godot-engine"]

    game = studio_packs.expand_pack_ids(["game"])
    assert "godot-engine" in game
    assert "unity-hub-helper" in game
    assert "unreal-engine-helper" in game
    assert "github-login-helper" in game

    claw = studio_packs.expand_pack_ids(["claw"])
    assert claw == ["openclaw-agent", "nemoclaw-sandbox"]

    agents = studio_packs.expand_pack_ids(["agents"])
    assert "agent-lab" in agents
    assert "nvidia-omni-agent" in agents
    assert "openclaw-agent" in agents

    music = studio_packs.expand_pack_ids(["music"])
    assert music == ["ace-step-music", "music-producer-lab", "music-daw-helper", "github-login-helper"]


def test_model_catalog_marks_vram_recommendations(monkeypatch) -> None:
    monkeypatch.setattr(studio_packs, "_detect_vram_gb", lambda: 8)
    monkeypatch.setattr(studio_packs, "_ollama_models", lambda: {"gemma3:4b"})
    monkeypatch.setattr(studio_packs, "_ollama_binary", lambda: "ollama")
    monkeypatch.setattr(studio_packs, "_ollama_reachable", lambda: True)

    catalog = studio_packs.model_catalog_with_status()
    by_id = {model["id"]: model for model in catalog["models"]}

    assert by_id["gemma3-4b"]["installed"] is True
    assert by_id["qwen3-8b"]["recommended"] is True
    assert by_id["deepseek-r1-8b"]["fits_vram"] is False


def test_ollama_binary_ignores_unusable_local_file(tmp_path, monkeypatch) -> None:
    home = tmp_path / "nvhive"
    local = home / "bin" / "ollama"
    local.parent.mkdir(parents=True)
    local.write_text("not a linux binary", encoding="utf-8")
    local.chmod(local.stat().st_mode | 0o755)

    monkeypatch.setenv("NVH_HOME", str(home))
    monkeypatch.setattr(studio_packs.shutil, "which", lambda _name: None)

    def raise_exec_format(*_args, **_kwargs):
        raise OSError(8, "Exec format error", str(local))

    monkeypatch.setattr(subprocess, "run", raise_exec_format)

    assert studio_packs._ollama_binary() == ""
    assert "Exec format error" in studio_packs._ollama_validation_error(local)


def test_godot_asset_selector_prefers_standard_linux_zip() -> None:
    release = {
        "assets": [
            {"name": "Godot_v4.5-stable_mono_linux_x86_64.zip", "browser_download_url": "https://example.invalid/mono.zip"},
            {"name": "Godot_v4.5-stable_export_templates.tpz", "browser_download_url": "https://example.invalid/templates.tpz"},
            {"name": "Godot_v4.5-stable_linux.x86_64.zip", "browser_download_url": "https://example.invalid/godot.zip"},
        ]
    }

    asset = studio_packs._select_godot_asset(release)

    assert asset["browser_download_url"].endswith("godot.zip")


def test_appimage_selector_prefers_linux_x64_assets() -> None:
    release = {
        "assets": [
            {"name": "audacity-linux-3.7.7-x64-20.04.AppImage", "browser_download_url": "https://example.invalid/audacity-20.AppImage"},
            {"name": "audacity-linux-3.7.7-x64-22.04.AppImage", "browser_download_url": "https://example.invalid/audacity-22.AppImage"},
            {"name": "audacity-linux-3.7.7-aarch64.AppImage", "browser_download_url": "https://example.invalid/audacity-arm.AppImage"},
        ]
    }

    asset = studio_packs._select_appimage_asset(
        release,
        app_name="Audacity",
        required_tokens=("linux",),
        preferred_tokens=("22.04", "x64"),
    )

    assert asset["browser_download_url"].endswith("audacity-22.AppImage")


def test_run_command_supports_long_install_timeout() -> None:
    signature = inspect.signature(studio_packs._run_command)

    assert "timeout" in signature.parameters


@pytest.mark.asyncio
async def test_ace_step_music_blocks_non_linux(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_STUDIO_HOME", str(tmp_path / "studio"))
    monkeypatch.setattr(studio_packs.platform, "system", lambda: "Darwin")

    pack = studio_packs._find_pack("ace-step-music")
    events = [event async for event in studio_packs._install_ace_step_music(pack, force_update=False)]

    assert events[0]["event"] == "error"
    assert not (tmp_path / "studio" / "packs" / "ace-step-music" / "installed.json").exists()


@pytest.mark.asyncio
async def test_music_daw_helper_does_not_mark_installed_without_downloads(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_STUDIO_HOME", str(tmp_path / "studio"))
    monkeypatch.setattr(studio_packs.platform, "system", lambda: "Linux")
    monkeypatch.setitem(sys.modules, "httpx", None)

    pack = studio_packs._find_pack("music-daw-helper")
    events = [event async for event in studio_packs._install_music_daw_helper(pack, force_update=False)]

    assert any(event["event"] == "error" for event in events)
    assert not (tmp_path / "studio" / "packs" / "music-daw-helper" / "installed.json").exists()


def test_catalog_status_uses_configured_studio_home(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_STUDIO_HOME", str(tmp_path))

    catalog = studio_packs.catalog_with_status()

    assert catalog["root"] == str(tmp_path)
    assert catalog["count"] >= 5
    assert all(pack["status"]["root"].startswith(str(tmp_path)) for pack in catalog["packs"])


def test_blender_pack_status_uses_persistent_apps_home(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("NVH_APPS_HOME", raising=False)
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))

    pack = studio_packs._find_pack("blender-creative")
    status = studio_packs.pack_status(pack)

    assert status["installed"] is False
    assert status["details"]["app_dir"].startswith(str(tmp_path / "nvh" / "apps" / "blender"))
    assert status["details"]["version"] == studio_packs.BLENDER_VERSION


def test_claw_status_marks_nemoclaw_blocked_without_docker(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    monkeypatch.setattr(studio_packs, "_node_runtime_status", lambda env=None: {
        "node": "/tmp/node",
        "npm": "/tmp/npm",
        "node_version": "v22.16.0",
        "npm_version": "10.9.0",
        "node_ok": True,
        "npm_ok": True,
        "ready": True,
        "can_auto_install": True,
        "minimum_node": "22.16.0",
        "minimum_npm": "10.0.0",
    })
    monkeypatch.setattr(studio_packs, "_docker_status", lambda: {
        "binary": "",
        "ready": False,
        "detail": "Docker was not found on PATH.",
        "rootless_hint": "Ask the provider to enable rootless Docker.",
    })
    monkeypatch.setattr(studio_packs, "_nemoclaw_binary_from_env", lambda env=None: "")

    openclaw = studio_packs.pack_status(studio_packs._find_pack("openclaw-agent"))
    nemoclaw = studio_packs.pack_status(studio_packs._find_pack("nemoclaw-sandbox"))

    assert openclaw["details"]["installable"] is True
    assert nemoclaw["installed"] is False
    assert nemoclaw["details"]["installable"] is False
    assert "Docker" in nemoclaw["details"]["blocked_reason"]


@pytest.mark.asyncio
async def test_comfy_nodes_skip_without_comfyui(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_STUDIO_HOME", str(tmp_path / "studio"))
    monkeypatch.setenv("COMFYUI_HOME", str(tmp_path / "missing-comfyui"))

    events = [event async for event in studio_packs.install_studio_packs(["comfy"])]

    assert any(event["event"] == "skip" for event in events)
    assert events[-1]["event"] == "complete"
    marker = tmp_path / "studio" / "packs" / "comfyui-power-nodes" / "installed.json"
    assert not marker.exists()

