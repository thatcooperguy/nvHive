"""Tests for ComfyUI integration helpers."""

from __future__ import annotations

import json

from nvh.integrations import comfyui


def test_examples_manifest_contains_current_starter_workflows() -> None:
    examples = comfyui.examples_as_dicts()
    ids = {example["id"] for example in examples}

    assert "z-image-turbo-text-to-image" in ids
    assert "wan22-5b-video-generation" in ids
    assert "flux-controlnet-canny-depth" in ids
    assert all(example["source_url"].startswith("https://") for example in examples)


def test_write_example_pack_creates_readable_manifest(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMFYUI_HOME", str(tmp_path))
    app_dir = tmp_path / "ComfyUI"
    app_dir.mkdir()

    examples_dir = comfyui.write_example_pack()
    manifest_path = examples_dir / "examples.json"
    readme_path = examples_dir / "README.md"

    assert manifest_path.exists()
    assert readme_path.exists()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["name"] == "nvHive ComfyUI starter examples"
    assert len(manifest["examples"]) >= 6
    assert "Wan 2.2 5B Video Generation" in readme_path.read_text(encoding="utf-8")


def test_detect_comfyui_reports_absent_install(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMFYUI_HOME", str(tmp_path))
    monkeypatch.setattr(
        comfyui,
        "_is_http_reachable",
        lambda host="127.0.0.1", port=8188: False,
    )

    status = comfyui.detect_comfyui()

    assert status["installed"] is False
    assert status["examples_installed"] is False
    assert status["install_root"] == str(tmp_path)
    assert status["url"] == "http://127.0.0.1:8188"
