"""Tests for student workstation helper utilities."""

from __future__ import annotations

from types import SimpleNamespace

from nvh.core import local_models as lm
from nvh.integrations import workstation
from nvh.integrations.workspace.storage import ensure_storage


def _gb10() -> SimpleNamespace:
    return SimpleNamespace(name="NVIDIA GB10", vram_mb=128 * 1024, unified_memory=True)


def test_model_recommendations_follow_the_local_model_table() -> None:
    assert workstation._recommend_chat_models(0) == []
    for gb in (6, 8, 24, 47, 96):
        recs = workstation._recommend_chat_models(gb)
        assert recs == [p.tag for p in lm.recommended(float(gb))], gb
        assert recs[0] == lm.pick(float(gb), "chat").tag
        assert lm.pick(float(gb), "embed").tag in recs
        assert any(lm.pick_for_tag(tag).vision for tag in recs), gb
    # the 40 GB tier leads with the multimodal Nemotron 3 Nano Omni MoE
    lead = lm.pick_for_tag(workstation._recommend_chat_models(47)[0])
    assert lead is not None and lead.vision and lead.moe
    retired = {"nemotron", "llama3.1:8b", "minicpm-v", "llava:7b", "nemotron-omni"}
    for gb in (6, 8, 24, 47, 96):
        assert not retired & set(workstation._recommend_chat_models(gb))


def test_model_recommendations_are_unified_aware() -> None:
    budget = lm.tier_budget([_gb10()], None)
    assert budget.budget_gb == 128 - lm.UNIFIED_MEMORY_OS_RESERVE_GB
    recs = workstation._recommend_chat_models(budget)
    assert recs == [p.tag for p in lm.recommended(budget)]
    assert lm.pick_for_tag(recs[0]).moe  # MoE leads on a bandwidth-bound pool
    unsized = lm.tier_budget([SimpleNamespace(name="NVIDIA Ghost", vram_mb=0)], None)
    assert workstation._recommend_chat_models(unsized) == []


def test_workstation_profile_plans_against_the_budget(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(workstation, "_detect_gpu_rows", lambda: [_gb10()])
    monkeypatch.setattr(workstation, "_system_memory", lambda: None)

    profile = workstation.detect_workstation_profile(home_dir=tmp_path / "persist")

    assert profile.has_gpu and profile.gpu_name == "NVIDIA GB10"
    assert profile.vram_gb == 128            # what the card reports
    assert profile.budget_gb == 112          # what the ladders plan against
    assert profile.unified_memory is True
    budget = lm.tier_budget([_gb10()], None)
    assert profile.recommended_chat_models == workstation._recommend_chat_models(budget)
    assert profile.recommended_comfy_profiles == workstation._recommend_comfy_profiles(112)
    assert any("Unified memory" in note for note in profile.notes)
    assert workstation.asdict(profile)["budget_gb"] == 112


def test_unified_note_names_the_pools_own_reserve(tmp_path, monkeypatch) -> None:
    """The note prints the reserve the budget took (lm.unified_os_reserve_gb, an eighth of the
    pool between 4 and 16 GB): 8 GB on a 64 GB pool, 16 GB on the GB10 -- not 16 GB for every
    unified machine."""
    monkeypatch.setattr(workstation, "_system_memory", lambda: None)

    mac = SimpleNamespace(name="Apple M4 Max", vram_mb=64 * 1024, unified_memory=True)
    monkeypatch.setattr(workstation, "_detect_gpu_rows", lambda: [mac])
    profile = workstation.detect_workstation_profile(home_dir=tmp_path / "persist")
    assert profile.unified_memory and profile.vram_gb == 64 and profile.budget_gb == 56
    (note,) = [n for n in profile.notes if n.startswith("Unified memory")]
    assert note == (
        "Unified memory: 64 GB shared by CPU and GPU; "
        "56 GB is planned for models after the 8 GB OS reserve."
    )

    monkeypatch.setattr(workstation, "_detect_gpu_rows", lambda: [_gb10()])
    profile = workstation.detect_workstation_profile(home_dir=tmp_path / "persist")
    (note,) = [n for n in profile.notes if n.startswith("Unified memory")]
    reserve = lm.unified_os_reserve_gb(128)
    assert reserve == lm.UNIFIED_MEMORY_OS_RESERVE_GB == 16.0
    assert note.endswith(f"112 GB is planned for models after the {reserve:.0f} GB OS reserve.")


def test_workstation_profile_without_gpu_recommends_nothing_local(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(workstation, "_detect_gpu_rows", lambda: [])
    monkeypatch.setattr(workstation.shutil, "which", lambda _name: None)
    monkeypatch.setattr(workstation, "_system_memory", lambda: None)

    profile = workstation.detect_workstation_profile(home_dir=tmp_path / "persist")

    assert profile.has_gpu is False and profile.vram_gb == 0 and profile.budget_gb == 0
    assert profile.recommended_chat_models == []
    assert profile.recommended_comfy_profiles == ["starter"]


def test_comfy_profiles_scale_with_vram() -> None:
    assert workstation._recommend_comfy_profiles(0) == ["starter"]
    assert workstation._recommend_comfy_profiles(8) == ["starter", "video"]
    assert "control" in workstation._recommend_comfy_profiles(16)
    assert "video-pro" in workstation._recommend_comfy_profiles(24)


def test_write_launch_script_uses_workstation_command(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(workstation.Path, "home", lambda: tmp_path)
    storage = ensure_storage(tmp_path / "persist", min_free_gb=0, activate=False)

    script = workstation.write_launch_script(
        port=3100,
        api_port=8100,
        install_comfyui=True,
        storage=storage,
    )

    content = script.read_text(encoding="utf-8")
    assert f'export NVH_HOME="{storage.layout.home}"' in content
    assert f'--home-dir "{storage.layout.home}"' in content
    assert "nvh workstation" in content
    assert "--launch" in content
    assert "--port 3100" in content
    assert "--api-port 8100" in content
    assert "--with-comfyui" in content


def test_desktop_launcher_opens_browser_without_terminal(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(workstation.Path, "home", lambda: tmp_path)
    (tmp_path / "Desktop").mkdir()
    storage = ensure_storage(tmp_path / "persist", min_free_gb=0, activate=False)

    desktop_file = workstation.write_desktop_launcher(
        port=3100,
        api_port=8100,
        storage=storage,
    )

    desktop_content = desktop_file.read_text(encoding="utf-8")
    assert "Terminal=false" in desktop_content
    assert "nvhive-ai-studio-desktop" in desktop_content

    desktop_script = storage.layout.bin_dir / "nvhive-ai-studio-desktop"
    script_content = desktop_script.read_text(encoding="utf-8")
    assert "server_ready" in script_content
    assert "ROOTLESS_FIREFOX" in script_content
    assert "FIREFOX_PROFILE" in script_content
    assert "--profile \"$FIREFOX_PROFILE\" --new-window" in script_content
    assert "xdg-open" in script_content
    assert "nohup nvh webui" in script_content
    assert "--port \"$PORT\"" in script_content
    assert "--api-port \"$API_PORT\"" in script_content
    assert "desktop-launcher.log" in script_content

    desktop_copy = tmp_path / "Desktop" / "NVHive AI Studio.desktop"
    assert desktop_copy.exists()
    assert "Terminal=false" in desktop_copy.read_text(encoding="utf-8")


def test_desktop_launcher_polls_api_health_not_just_webui(tmp_path, monkeypatch) -> None:
    """Regression test for the silent "nothing ever loaded" bug.

    Prior behavior: the launcher's ``server_ready`` only polled the WebUI
    on port 3000. So on cold cloud-VM boots where the API on 8000 was
    still importing FastAPI + 100+ deps, Firefox would open onto a
    functional WebUI shell whose every fetch silently failed. The user
    saw blank cards with no explanation.

    Fix: the launcher now requires BOTH the WebUI AND the API to be
    healthy before opening the browser, and logs which side stalled
    so users can grep the log.
    """
    monkeypatch.setattr(workstation.Path, "home", lambda: tmp_path)
    storage = ensure_storage(tmp_path / "persist", min_free_gb=0, activate=False)

    script = workstation.write_desktop_launch_script(
        port=3100, api_port=8100, storage=storage,
    )
    body = script.read_text(encoding="utf-8")

    # Both probes exist as separate functions so the diagnostic can
    # distinguish which one stalled.
    assert "webui_ready()" in body
    assert "api_ready()" in body
    # Combined gate: server_ready === webui_ready && api_ready
    assert "webui_ready && api_ready" in body
    # API probe must hit the canonical health endpoint, not the WebUI.
    assert "/v1/health" in body
    # When the timeout elapses, the log must name which side failed so
    # the user has a place to look (the api-server.log we now produce).
    assert "WebUI is up on $PORT but API on $API_PORT did not respond" in body
    assert "api-server.log" in body
