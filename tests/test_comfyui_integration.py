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


def test_write_model_plan_creates_selected_manifest(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMFYUI_HOME", str(tmp_path))
    app_dir = tmp_path / "ComfyUI"
    app_dir.mkdir()

    plan_path = comfyui.write_model_plan(["wan22-5b-video-generation"])
    helper_path = tmp_path / "ComfyUI" / "nvhive_examples" / "download-comfy-models.sh"

    assert plan_path.exists()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["download_helper"] == "download-comfy-models.sh"
    assert plan["models"][0]["target_folder"] in {"diffusion_models", "vae"}
    assert helper_path.exists()
    assert "wan2.2_ti2v_5B_fp16.safetensors" in helper_path.read_text(encoding="utf-8")


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
