"""Tests for nvWizard setup experience helpers."""

from __future__ import annotations

from types import SimpleNamespace

from nvh.integrations import auto_repair, model_fit, mount_autopilot, smoke_tests


def test_mount_autopilot_prefers_existing_nvh_home(tmp_path, monkeypatch) -> None:
    mount = tmp_path / "persistent"
    home = mount / "nvhive"
    (home / "receipts").mkdir(parents=True)
    (home / "models").mkdir()

    monkeypatch.setattr(mount_autopilot, "_common_roots", lambda: [])
    report = mount_autopilot.mount_autopilot_report(extra_roots=[mount])

    assert report["recommended"]["recommended_home"] == str(home)
    assert "receipts" in report["recommended"]["evidence"]


def test_auto_repair_writes_env_file_without_downloads(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    monkeypatch.setattr(auto_repair, "detect_comfyui", lambda **_: {"installed": False})
    monkeypatch.setattr(auto_repair, "receipt_summary", lambda **_: {"unhealthy": 0})
    monkeypatch.setattr(auto_repair, "load_setup_catalog", lambda refresh=False: {"source": "bundled"})
    monkeypatch.setattr(
        auto_repair,
        "storage_status",
        lambda home_dir=None: SimpleNamespace(
            ok=True,
            configured_by="argument",
            layout=SimpleNamespace(home=tmp_path / "nvh"),
        ),
    )

    from nvh.integrations.storage import ensure_storage

    ensure_storage(tmp_path / "nvh")
    result = auto_repair.run_safe_repairs(home_dir=tmp_path / "nvh")

    assert result["errors"] == []
    assert any(item["id"] == "storage-env-file" for item in result["completed"])


def test_model_fit_scores_recommended_models(monkeypatch) -> None:
    monkeypatch.setattr(
        model_fit,
        "storage_status",
        lambda home_dir=None: SimpleNamespace(as_dict=lambda: {"free_gb": 100}),
    )
    monkeypatch.setattr(
        model_fit,
        "model_catalog_with_status",
        lambda: {
            "detected_vram_gb": 12,
            "ollama_available": True,
            "ollama_running": True,
            "models": [
                {
                    "id": "fast-chat",
                    "priority": 10,
                    "recommended": True,
                    "fits_vram": True,
                    "installed": False,
                    "estimated_disk_gb": 4,
                    "capabilities": ["chat", "fast"],
                }
            ],
        },
    )

    report = model_fit.model_fit_report()

    assert report["recommended_ids"] == ["fast-chat"]
    assert report["models"][0]["fit_score"] > 100


def test_smoke_tests_surface_comfyui_example_repair(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        smoke_tests,
        "storage_status",
        lambda home_dir=None: SimpleNamespace(
            ok=True,
            configured_by="argument",
            env_file=tmp_path / "nvh-env.sh",
            layout=SimpleNamespace(home=tmp_path),
        ),
    )
    monkeypatch.setattr(
        smoke_tests,
        "catalog_with_status",
        lambda: {"packs": [{"id": "agent-lab", "status": {"installed": True}}]},
    )
    monkeypatch.setattr(
        smoke_tests,
        "detect_comfyui",
        lambda **_: {"installed": True, "running": False, "examples_installed": False, "app_dir": str(tmp_path), "examples_dir": str(tmp_path / "examples")},
    )

    report = smoke_tests.smoke_test_report(home_dir=str(tmp_path))
    by_id = {item["id"]: item for item in report["tests"]}

    assert by_id["comfyui-examples"]["status"] == "warn"
    assert by_id["comfyui-examples"]["action_id"] == "comfyui-examples"
