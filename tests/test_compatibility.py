"""Tests for nvWizard host/app compatibility checks."""

from __future__ import annotations

from types import SimpleNamespace

from nvh.integrations import compatibility


def test_recommended_torch_profile_tracks_cuda_driver() -> None:
    assert compatibility.recommended_torch_profile("13.0") == "nvidia-cu130"
    assert compatibility.recommended_torch_profile("12.4") == "nvidia-cu121"
    assert compatibility.recommended_torch_profile("11.8") == "cpu"
    assert compatibility.recommended_torch_profile("") == "cpu"


def test_compatibility_report_marks_rootless_fixable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(compatibility.sys, "platform", "linux")
    monkeypatch.setattr(compatibility.platform, "system", lambda: "Linux")
    monkeypatch.setattr(compatibility.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(compatibility.platform, "release", lambda: "6.8.0")
    monkeypatch.setattr(compatibility.platform, "platform", lambda: "Linux")
    monkeypatch.setattr(compatibility.platform, "libc_ver", lambda: ("glibc", "2.35"))
    monkeypatch.setattr(compatibility, "_read_os_release", lambda: {"PRETTY_NAME": "Ubuntu 24.04"})
    monkeypatch.setattr(
        compatibility,
        "_nvidia_smi_query",
        lambda **_: {
            "name": "NVIDIA RTX",
            "memory_total_mb": "24576",
            "driver_version": "570.00",
            "cuda_version": "12.4",
        },
    )
    monkeypatch.setattr(compatibility, "_which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr(compatibility, "_command_version", lambda *_, **__: "ok")
    monkeypatch.setattr(compatibility, "_port_open", lambda *_: False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(
        compatibility,
        "runtime_status",
        lambda: SimpleNamespace(
            venv_available=True,
            pip_available=True,
            strategy="python-venv",
        ),
    )
    monkeypatch.setattr(
        compatibility,
        "storage_status",
        lambda **_: SimpleNamespace(
            as_dict=lambda: {
                "ok": True,
                "configured_by": "argument",
                "layout": {"home": str(tmp_path / "nvh")},
            },
        ),
    )
    monkeypatch.setattr(
        compatibility,
        "model_catalog_with_status",
        lambda **_: {
            "recommended_ids": ["gemma3-4b"],
            "models": [{"id": "gemma3-4b", "recommended": True, "installed": False}],
            "ollama_available": False,
            "ollama_running": False,
        },
    )
    monkeypatch.setattr(
        compatibility,
        "catalog_with_status",
        lambda **_: {
            "packs": [
                {"id": "agent-lab", "status": {"installed": False}},
            ],
        },
    )

    report = compatibility.compatibility_report(home_dir=tmp_path / "nvh")
    by_id = {app["id"]: app for app in report["apps"]}

    assert report["recommended_torch_profile"] == "nvidia-cu121"
    assert by_id["rootless-ollama"]["status"] == "ready"
    assert by_id["local-models"]["status"] == "fixable"
    assert by_id["local-models"]["recommended_action_id"] == "starter-models"
    assert by_id["agent-lab"]["recommended_action_id"] == "agent-lab"


def test_compatibility_report_blocks_missing_git_for_comfyui(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(compatibility.sys, "platform", "linux")
    monkeypatch.setattr(compatibility.platform, "system", lambda: "Linux")
    monkeypatch.setattr(compatibility.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(compatibility.platform, "release", lambda: "6.8.0")
    monkeypatch.setattr(compatibility.platform, "platform", lambda: "Linux")
    monkeypatch.setattr(compatibility.platform, "libc_ver", lambda: ("glibc", "2.35"))
    monkeypatch.setattr(compatibility, "_read_os_release", lambda: {"PRETTY_NAME": "Ubuntu 24.04"})
    monkeypatch.setattr(compatibility, "_nvidia_smi_query", lambda: {})
    monkeypatch.setattr(compatibility, "_which", lambda cmd: None if cmd == "git" else f"/usr/bin/{cmd}")
    monkeypatch.setattr(compatibility, "_command_version", lambda *_, **__: "ok")
    monkeypatch.setattr(compatibility, "_port_open", lambda *_: False)
    monkeypatch.setattr(
        compatibility,
        "runtime_status",
        lambda: SimpleNamespace(
            venv_available=True,
            pip_available=True,
            strategy="python-venv",
        ),
    )
    monkeypatch.setattr(
        compatibility,
        "storage_status",
        lambda **_: SimpleNamespace(
            as_dict=lambda: {
                "ok": True,
                "configured_by": "argument",
                "layout": {"home": str(tmp_path / "nvh")},
            },
        ),
    )
    monkeypatch.setattr(
        compatibility,
        "model_catalog_with_status",
        lambda **_: {
            "recommended_ids": [],
            "models": [],
            "ollama_available": True,
            "ollama_running": True,
        },
    )
    monkeypatch.setattr(
        compatibility,
        "catalog_with_status",
        lambda: {"packs": [{"id": "agent-lab", "status": {"installed": True}}]},
    )

    report = compatibility.compatibility_report(home_dir=tmp_path / "nvh")
    comfy = {app["id"]: app for app in report["apps"]}["comfyui"]

    assert comfy["status"] == "blocked"
    assert any(req["id"] == "git" and req["status"] == "blocked" for req in comfy["requirements"])


def test_compatibility_report_does_not_reinstall_installed_comfyui(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(compatibility.sys, "platform", "linux")
    monkeypatch.setattr(compatibility.platform, "system", lambda: "Linux")
    monkeypatch.setattr(compatibility.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(compatibility.platform, "release", lambda: "6.8.0")
    monkeypatch.setattr(compatibility.platform, "platform", lambda: "Linux")
    monkeypatch.setattr(compatibility.platform, "libc_ver", lambda: ("glibc", "2.35"))
    monkeypatch.setattr(compatibility, "_read_os_release", lambda: {"PRETTY_NAME": "Ubuntu 24.04"})
    monkeypatch.setattr(compatibility, "_nvidia_smi_query", lambda: {})
    monkeypatch.setattr(compatibility, "_which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr(compatibility, "_command_version", lambda *_, **__: "ok")
    monkeypatch.setattr(compatibility, "_port_open", lambda *_: False)
    monkeypatch.setattr(
        compatibility,
        "runtime_status",
        lambda: SimpleNamespace(
            venv_available=True,
            pip_available=True,
            strategy="python-venv",
        ),
    )
    monkeypatch.setattr(
        compatibility,
        "storage_status",
        lambda **_: SimpleNamespace(
            as_dict=lambda: {
                "ok": True,
                "configured_by": "argument",
                "layout": {"home": str(tmp_path / "nvh")},
            },
        ),
    )
    monkeypatch.setattr(
        compatibility,
        "model_catalog_with_status",
        lambda **_: {"recommended_ids": [], "models": [], "ollama_available": True, "ollama_running": True},
    )
    monkeypatch.setattr(
        compatibility,
        "catalog_with_status",
        lambda: {"packs": [{"id": "agent-lab", "status": {"installed": True}}]},
    )

    home = tmp_path / "nvh"
    app_dir = home / "comfyui" / "ComfyUI"
    examples = app_dir / "nvhive_examples"
    examples.mkdir(parents=True)
    (app_dir / "main.py").write_text("print('comfy')\n", encoding="utf-8")
    (examples / "examples.json").write_text("{}", encoding="utf-8")

    report = compatibility.compatibility_report(home_dir=home)
    comfy = {app["id"]: app for app in report["apps"]}["comfyui"]

    assert comfy["status"] == "ready"
    assert comfy["recommended_action_id"] is None
