"""Tests for rootless persistent storage helpers."""

from __future__ import annotations

import os

from nvh.integrations import storage


def test_ensure_storage_creates_canonical_layout(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("NVH_HOME", raising=False)

    status = storage.ensure_storage(tmp_path / "persist", min_free_gb=0)

    assert status.ok is True
    assert status.configured_by == "argument"
    assert status.layout.home == tmp_path / "persist"
    assert status.layout.bin_dir.is_dir()
    assert status.layout.models_dir.is_dir()
    assert status.layout.ollama_models_dir.is_dir()
    assert status.layout.runtime_dir.is_dir()
    assert status.layout.apps_dir.is_dir()
    assert status.layout.webui_dir.is_dir()
    assert status.layout.comfyui_dir.is_dir()
    assert status.layout.config_dir.is_dir()
    assert status.env_file.exists()
    assert status.write_probe_ok is True
    assert status.write_probe_error == ""
    assert "NVH_HOME" in status.layout.env()
    assert "NVH_RUNTIME_HOME" in status.layout.env()
    assert "NVH_APPS_HOME" in status.layout.env()
    assert "NVH_WEB_HOME" in status.layout.env()
    assert "PIP_CACHE_DIR" in status.layout.env()
    assert "HIVE_CONFIG_HOME" in status.layout.env()


def test_storage_status_warns_when_home_is_implicit(monkeypatch) -> None:
    monkeypatch.delenv("NVH_HOME", raising=False)
    monkeypatch.delenv("NVHIVE_HOME", raising=False)

    status = storage.storage_status(min_free_gb=0)

    assert status.configured_by == "default"
    assert any("NVH_HOME is not set" in warning for warning in status.warnings)


def test_ensure_storage_preserves_implicit_default_source(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("NVH_HOME", raising=False)
    monkeypatch.delenv("NVHIVE_HOME", raising=False)
    monkeypatch.setattr(storage.Path, "home", lambda: tmp_path)

    status = storage.ensure_storage(min_free_gb=0)

    assert status.configured_by == "default"
    assert status.layout.home == tmp_path / ".nvh"
    assert "NVH_HOME" not in os.environ
    assert any("NVH_HOME is not set" in warning for warning in status.warnings)


def test_storage_layout_respects_component_envs(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COMFYUI_HOME", str(tmp_path / "custom-comfy"))
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "custom-models"))

    layout = storage.storage_layout()

    assert layout.home == tmp_path / "home"
    assert layout.comfyui_dir == tmp_path / "custom-comfy"
    assert layout.ollama_models_dir == tmp_path / "custom-models"
