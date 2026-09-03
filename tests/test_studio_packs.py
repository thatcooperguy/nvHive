"""Tests for rootless AI Studio pack helpers."""

from __future__ import annotations

import inspect
import io
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

from nvh.core import local_models as lm
from nvh.integrations import studio_packs

# Retired or never-published Ollama tags and their old catalog ids: none may
# survive as a picker row, an install target or a pack model.
RETIRED = {
    "nemotron-omni", "nemotron-3-nano-omni", "nemotron-70b", "nemotron", "nemotron:70b",
    "llama31-8b", "llama3.1:8b", "qwen25-coder-7b", "qwen2.5-coder:7b",
    "deepseek-r1-8b", "deepseek-r1:8b", "llava-7b", "llava:7b", "minicpm-v",
}


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


def test_studio_models_mirror_the_local_model_table() -> None:
    by_tag = {model.install_target: model for model in studio_packs.STUDIO_MODELS}
    assert sorted(by_tag) == lm.all_tags()
    assert len(by_tag) == len(studio_packs.STUDIO_MODELS)
    for pick in lm.all_picks():
        model = by_tag[pick.tag]
        assert model.id == pick.catalog_id
        assert model.provider == "ollama"
        assert model.estimated_disk_gb == pick.weights_gb
        first_tier = next(
            tier for tier in lm.LOCAL_MODEL_TIERS
            if pick.tag in {candidate.tag for candidate in tier.picks.values()}
        )
        assert model.recommended_vram_gb == int(first_tier.min_gb)
        assert model.source_url.endswith(f"/library/{pick.name}")
        assert ("vision" in model.capabilities) is pick.vision
        assert ("moe" in model.capabilities) is pick.moe
        assert model.title and model.why_recommended and model.license_note
    # strongest first: distinct priorities that follow the tier a pick first fits
    priorities = [model.priority for model in studio_packs.STUDIO_MODELS]
    assert priorities == sorted(priorities) and len(set(priorities)) == len(priorities)
    assert by_tag[lm.pick(96.0, "chat").tag].priority < by_tag[lm.pick(8.0, "chat").tag].priority
    # the mission builder and model_fit read these category / capability words
    assert by_tag[lm.pick(24.0, "code").tag].category == "code"
    assert "coding" in by_tag[lm.pick(24.0, "code").tag].capabilities
    assert by_tag[lm.pick(0.0, "embed").tag].category == "embedding"
    assert "embedding" in by_tag[lm.pick(0.0, "embed").tag].capabilities


def test_retired_and_phantom_tags_are_gone() -> None:
    targets = {model.install_target for model in studio_packs.STUDIO_MODELS}
    targets |= {model.id for model in studio_packs.STUDIO_MODELS}
    for pack in studio_packs.STUDIO_PACKS:
        targets |= set(pack.models)
        assert set(pack.models) <= set(lm.all_tags()), pack.id
    assert not RETIRED & targets


def test_model_packs_are_cut_from_the_table() -> None:
    starter = studio_packs._find_pack("llm-starter")
    assert starter.models == [p.tag for p in lm.recommended(float(starter.recommended_vram_gb))]
    assert starter.estimated_disk_gb == round(
        sum(lm.pick_for_tag(tag).weights_gb for tag in starter.models), 1,
    )
    assert lm.pick(float(starter.recommended_vram_gb), "embed").tag in starter.models

    coder = studio_packs._find_pack("llm-coder-reasoner")
    gb = float(coder.recommended_vram_gb)
    assert set(coder.models) == {lm.pick(gb, "code").tag, lm.pick(gb, "reasoning").tag}
    assert coder.recommended_vram_gb > starter.recommended_vram_gb  # below it code == the starter's chat
    assert any(lm.pick_for_tag(tag).moe for tag in coder.models)

    omni = studio_packs._find_pack("nvidia-omni-agent")
    assert omni.models
    for tag in omni.models:
        pick = lm.pick_for_tag(tag)
        assert pick is not None and pick.vision and pick.moe, tag
    assert omni.recommended_vram_gb == min(
        model.recommended_vram_gb
        for model in studio_packs.STUDIO_MODELS
        if model.install_target in omni.models
    )
    assert studio_packs._OMNI_MIN_FREE_GB >= max(lm.pick_for_tag(t).weights_gb for t in omni.models)
    assert set(studio_packs._omni_model_sizes_gb().values()) == {
        lm.pick_for_tag(t).weights_gb for t in omni.models
    }


def test_model_catalog_marks_vram_recommendations(monkeypatch) -> None:
    fallback = lm.pick(8.0, "cpu_fallback")
    monkeypatch.setattr(studio_packs, "_detect_vram_gb", lambda: 8)
    monkeypatch.setattr(studio_packs, "_ollama_models", lambda: {fallback.tag})
    monkeypatch.setattr(studio_packs, "_ollama_binary", lambda: "ollama")
    monkeypatch.setattr(studio_packs, "_ollama_reachable", lambda: True)

    catalog = studio_packs.model_catalog_with_status()
    by_id = {model["id"]: model for model in catalog["models"]}

    assert catalog["detected_vram_gb"] == 8
    assert by_id[fallback.catalog_id]["installed"] is True
    assert set(catalog["recommended_ids"]) == {p.catalog_id for p in lm.recommended(8.0)}
    assert by_id[lm.pick(8.0, "chat").catalog_id]["recommended"] is True
    for model in catalog["models"]:
        assert model["fits_vram"] is (model["recommended_vram_gb"] == 0 or model["recommended_vram_gb"] <= 8)
    too_big = lm.pick(12.0, "vision")  # first fits the 12 GB tier
    assert by_id[too_big.catalog_id]["fits_vram"] is False
    assert by_id[too_big.catalog_id]["recommended"] is False


def test_model_catalog_prefers_large_and_multimodal_models_on_big_gpus(monkeypatch) -> None:
    monkeypatch.setattr(studio_packs, "_detect_vram_gb", lambda: 47)
    monkeypatch.setattr(studio_packs, "_ollama_models", lambda: set())
    monkeypatch.setattr(studio_packs, "_ollama_binary", lambda: "ollama")
    monkeypatch.setattr(studio_packs, "_ollama_reachable", lambda: True)

    catalog = studio_packs.model_catalog_with_status()
    by_id = {model["id"]: model for model in catalog["models"]}

    chat = lm.pick(47.0, "chat")
    assert chat.vision and chat.moe  # Nemotron 3 Nano Omni leads the 40 GB tier
    assert catalog["recommended_ids"][0] == chat.catalog_id
    assert by_id[chat.catalog_id]["recommended"] is True
    assert by_id[chat.catalog_id]["fits_vram"] is True
    vision = lm.pick(47.0, "vision")
    assert by_id[vision.catalog_id]["recommended"] is True
    assert by_id[vision.catalog_id]["fits_vram"] is True
    assert set(catalog["recommended_ids"]) == {p.catalog_id for p in lm.recommended(47.0)}


def test_detect_vram_gb_plans_against_the_unified_budget(monkeypatch) -> None:
    gb10 = SimpleNamespace(name="NVIDIA GB10", vram_mb=128 * 1024, unified_memory=True)
    monkeypatch.setattr(studio_packs, "detect_gpus", lambda: [gb10])
    monkeypatch.setattr(studio_packs, "detect_system_memory", lambda: None)
    assert studio_packs._detect_tier_budget().unified is True
    assert studio_packs._detect_vram_gb() == 128 - int(lm.UNIFIED_MEMORY_OS_RESERVE_GB)

    rtx = SimpleNamespace(name="NVIDIA GeForce RTX 4090", vram_mb=24 * 1024, unified_memory=False)
    monkeypatch.setattr(studio_packs, "detect_gpus", lambda: [rtx])
    assert studio_packs._detect_vram_gb() == 24

    monkeypatch.setattr(studio_packs, "detect_gpus", lambda: [])
    monkeypatch.setattr(studio_packs, "_nvidia_smi_rows", lambda: [])
    assert studio_packs._detect_vram_gb() == 0
    assert studio_packs._recommended_model_ids(0) == {p.catalog_id for p in lm.recommended(0.0)}


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
    assert "not a Linux ELF binary" in studio_packs._ollama_validation_error(local)


def test_ollama_validation_reports_html_download(tmp_path) -> None:
    binary = tmp_path / "ollama"
    binary.write_bytes(b"<!doctype html><title>404</title>")
    binary.chmod(binary.stat().st_mode | 0o755)

    error = studio_packs._ollama_validation_error(binary)

    assert "HTML/error page" in error
    assert studio_packs._binary_file_probe(binary)["format"] == "html"


def test_ollama_validation_reports_wrong_elf_arch(tmp_path, monkeypatch) -> None:
    binary = tmp_path / "ollama"
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[18:20] = (0xB7).to_bytes(2, "little")
    binary.write_bytes(bytes(header))
    binary.chmod(binary.stat().st_mode | 0o755)
    monkeypatch.setattr(studio_packs, "_platform_arch", lambda: "amd64")

    error = studio_packs._ollama_validation_error(binary)
    probe = studio_packs._binary_file_probe(binary)

    assert "wrong CPU architecture" in error
    assert probe["elf_machine"] == "arm64"
    assert probe["arch_match"] is False


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


def test_ollama_download_candidates_prefer_latest_tar_zst(monkeypatch) -> None:
    monkeypatch.delenv("NVH_OLLAMA_URL", raising=False)
    monkeypatch.delenv("NVH_OLLAMA_VERSION", raising=False)
    monkeypatch.setenv("OLLAMA_VERSION", "not-an-nvhive-runtime-pin")

    candidates = studio_packs._ollama_download_candidates("amd64")

    assert candidates[0] == ("https://ollama.com/download/ollama-linux-amd64.tar.zst", "tar.zst")
    assert candidates[1] == (
        "https://github.com/ollama/ollama/releases/latest/download/ollama-linux-amd64.tar.zst",
        "tar.zst",
    )
    assert candidates[2] == ("https://ollama.com/download/ollama-linux-amd64.tgz", "tgz")
    assert candidates[3] == (
        "https://github.com/ollama/ollama/releases/latest/download/ollama-linux-amd64.tgz",
        "tgz",
    )


def test_ollama_download_candidates_support_version_pin(monkeypatch) -> None:
    monkeypatch.delenv("NVH_OLLAMA_URL", raising=False)
    monkeypatch.setenv("NVH_OLLAMA_VERSION", "v1.2.3")

    candidates = studio_packs._ollama_download_candidates("amd64")

    assert candidates[0][0].endswith("ollama-linux-amd64.tar.zst?version=v1.2.3")
    assert candidates[1][0].endswith("/releases/download/v1.2.3/ollama-linux-amd64.tar.zst")
    assert candidates[2][0].endswith("ollama-linux-amd64.tgz?version=v1.2.3")
    assert candidates[3][0].endswith("/releases/download/v1.2.3/ollama-linux-amd64.tgz")


def test_extract_ollama_tar_zst_into_rootless_home(tmp_path) -> None:
    zstd = pytest.importorskip("zstandard")
    home = tmp_path / "nvhive"
    archive = tmp_path / "ollama-linux-amd64.tar.zst"
    raw_tar = io.BytesIO()
    with tarfile.open(fileobj=raw_tar, mode="w") as tar:
        data = b"#!/usr/bin/env bash\n"
        info = tarfile.TarInfo("bin/ollama")
        info.size = len(data)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(data))
        lib = b"runtime"
        lib_info = tarfile.TarInfo("lib/ollama/runner")
        lib_info.size = len(lib)
        lib_info.mode = 0o644
        tar.addfile(lib_info, io.BytesIO(lib))
    archive.write_bytes(zstd.ZstdCompressor().compress(raw_tar.getvalue()))

    studio_packs._extract_ollama_archive(archive, "tar.zst", home)

    assert (home / "bin" / "ollama").exists()
    assert (home / "lib" / "ollama" / "runner").read_text(encoding="utf-8") == "runtime"


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
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
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

