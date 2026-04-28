"""Tests for student workstation helper utilities."""

from __future__ import annotations

from nvh.integrations import workstation
from nvh.integrations.storage import ensure_storage


def test_model_recommendations_scale_with_vram() -> None:
    assert workstation._recommend_chat_models(0) == []
    assert workstation._recommend_chat_models(6) == ["nemotron-mini"]
    assert "llama3.1:8b" in workstation._recommend_chat_models(8)
    assert "nemotron" in workstation._recommend_chat_models(24)


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
