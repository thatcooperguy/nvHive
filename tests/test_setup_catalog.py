"""Tests for setup catalog fallback and status."""

from __future__ import annotations

from nvh.integrations import catalog


def test_bundled_catalog_has_student_profiles() -> None:
    data = catalog.bundled_catalog()

    assert data["schema_version"] == catalog.SCHEMA_VERSION
    assert {profile["id"] for profile in data["profiles"]} >= {"student", "creator", "full"}
    assert data["models"]
    assert data["comfyui_examples"]


def test_catalog_status_uses_bundled_without_network(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))

    status = catalog.catalog_status(refresh=False)

    assert status["source"] == "bundled"
    assert status["profile_count"] >= 3
    assert status["model_count"] >= 1
