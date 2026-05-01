"""Tests for ComfyUI integration helpers."""

from __future__ import annotations

import json
from pathlib import Path

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


def test_wait_for_comfyui_polls_until_reachable(monkeypatch) -> None:
    reachable = iter([False, False, True])
    sleeps: list[float] = []
    ticks = iter([0.0, 0.1, 0.2, 0.3, 0.4])

    monkeypatch.setattr(comfyui, "_is_http_reachable", lambda host="127.0.0.1", port=8188: next(reachable))
    monkeypatch.setattr(comfyui, "detect_comfyui", lambda host="127.0.0.1", port=8188: {"running": True})
    monkeypatch.setattr(comfyui.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(comfyui.time, "monotonic", lambda: next(ticks))

    status = comfyui.wait_for_comfyui(timeout_s=5, interval_s=0.25)

    assert status["ready"] is True
    assert status["ready_timeout"] is False
    assert sleeps == [0.25, 0.25]


def test_wait_for_comfyui_timeout_returns_not_ready(monkeypatch) -> None:
    ticks = iter([0.0, 0.1, 2.0, 2.1])

    monkeypatch.setattr(comfyui, "_is_http_reachable", lambda host="127.0.0.1", port=8188: False)
    monkeypatch.setattr(comfyui, "detect_comfyui", lambda host="127.0.0.1", port=8188: {"running": False})
    monkeypatch.setattr(comfyui.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(comfyui.time, "monotonic", lambda: next(ticks))

    status = comfyui.wait_for_comfyui(timeout_s=1, interval_s=0.25)

    assert status["ready"] is False
    assert status["ready_timeout"] is True


def test_start_comfyui_returns_readiness_poll_result(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMFYUI_HOME", str(tmp_path))
    app_dir = tmp_path / "ComfyUI"
    python_path = comfyui.comfyui_venv_python(tmp_path)
    app_dir.mkdir(parents=True)
    python_path.parent.mkdir(parents=True)
    (app_dir / "main.py").write_text("print('comfy')\n", encoding="utf-8")
    python_path.write_text("", encoding="utf-8")

    class FakeProcess:
        pid = 1234

    monkeypatch.setattr(comfyui, "_is_http_reachable", lambda host="127.0.0.1", port=8188: False)
    monkeypatch.setattr(comfyui.subprocess, "Popen", lambda cmd, **kwargs: FakeProcess())
    monkeypatch.setattr(
        comfyui,
        "wait_for_comfyui",
        lambda host="127.0.0.1", port=8188: {
            "running": True,
            "ready": True,
            "ready_timeout": False,
            "url": f"http://{host}:{port}",
        },
    )

    status = comfyui.start_comfyui()

    assert status["started"] is True
    assert status["pid"] == 1234
    assert status["ready"] is True
    assert Path(status["log_path"]).name == "comfyui.log"
