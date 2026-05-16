"""Tests for student workstation helper utilities."""

from __future__ import annotations

from nvh.integrations import workstation
from nvh.integrations.workspace.storage import ensure_storage


def test_model_recommendations_scale_with_vram() -> None:
    assert workstation._recommend_chat_models(0) == []
    assert workstation._recommend_chat_models(6) == ["gemma3:4b", "moondream"]
    assert "gemma3:4b" in workstation._recommend_chat_models(8)
    assert "llama3.1:8b" in workstation._recommend_chat_models(8)
    assert workstation._recommend_chat_models(24)[0] == "llama3.2-vision"
    assert workstation._recommend_chat_models(47)[0] == "nemotron"


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
