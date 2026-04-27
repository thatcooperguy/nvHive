"""Tests for rootless AI Studio pack helpers."""

from __future__ import annotations

import pytest

from nvh.integrations import studio_packs


def test_catalog_is_rootless_and_grouped() -> None:
    catalog = studio_packs.catalog_as_dicts()
    ids = {pack["id"] for pack in catalog}

    assert "rootless-ollama" in ids
    assert "llm-starter" in ids
    assert "agent-lab" in ids
    assert "comfyui-power-nodes" in ids
    assert "game-dev-lab" in ids
    assert all(pack["no_root"] for pack in catalog)


def test_pack_bundles_expand_without_duplicates() -> None:
    starter = studio_packs.expand_pack_ids(["starter"])

    assert starter[0] == "rootless-ollama"
    assert "llm-starter" in starter
    assert "agent-lab" in starter
    assert len(starter) == len(set(starter))


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


def test_catalog_status_uses_configured_studio_home(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_STUDIO_HOME", str(tmp_path))

    catalog = studio_packs.catalog_with_status()

    assert catalog["root"] == str(tmp_path)
    assert catalog["count"] >= 5
    assert all(pack["status"]["root"].startswith(str(tmp_path)) for pack in catalog["packs"])


@pytest.mark.asyncio
async def test_comfy_nodes_skip_without_comfyui(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_STUDIO_HOME", str(tmp_path / "studio"))
    monkeypatch.setenv("COMFYUI_HOME", str(tmp_path / "missing-comfyui"))

    events = [event async for event in studio_packs.install_studio_packs(["comfy"])]

    assert any(event["event"] == "skip" for event in events)
    assert events[-1]["event"] == "complete"
    marker = tmp_path / "studio" / "packs" / "comfyui-power-nodes" / "installed.json"
    assert not marker.exists()

