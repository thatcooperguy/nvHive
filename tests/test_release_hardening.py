"""Regression tests for release, packaging, and rootless deployment hardening."""

from __future__ import annotations

import tomllib
from pathlib import Path

from nvh.storage import repository

ROOT = Path(__file__).resolve().parents[1]


def test_all_extra_contains_runtime_extras() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = data["project"]["optional-dependencies"]
    all_extra = set(extras["all"])

    for group in ("serve", "nvidia", "mcp", "vision", "browser"):
        assert set(extras[group]).issubset(all_extra)


def test_release_workflow_has_tag_version_parity_gate() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "Verify release version matches tag" in workflow
    assert "RELEASE_TAG" in workflow
    assert "pyproject.toml" in workflow
    assert "nvh/__init__.py" in workflow


def test_repository_default_db_path_prefers_rootless_state(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("HIVE_DATA_DIR", raising=False)
    monkeypatch.delenv("NVH_STATE", raising=False)
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvhive"))

    assert repository._default_db_path() == tmp_path / "nvhive" / "state" / "nvhive.db"

    monkeypatch.setenv("NVH_STATE", str(tmp_path / "state"))
    assert repository._default_db_path() == tmp_path / "state" / "nvhive.db"

    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "data"))
    assert repository._default_db_path() == tmp_path / "data" / "state" / "nvhive.db"


def test_docker_compose_api_is_not_blocked_by_ollama_health() -> None:
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    hive_api_block = compose.split("hive-api:", 1)[1].split("hive-web:", 1)[0]

    assert "condition: service_healthy" not in hive_api_block


def test_cloud_compose_requires_api_key_before_public_bind() -> None:
    cloud = (ROOT / "docker-compose.cloud.yaml").read_text(encoding="utf-8")

    assert "HIVE_API_KEY: \"${HIVE_API_KEY:?" in cloud


def test_linux_installer_handles_missing_ensurepip_without_root() -> None:
    install = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert "create_rootless_venv" in install
    assert "--without-pip" in install
    assert "bootstrap.pypa.io/get-pip.py" in install
    assert "create_managed_python_env" in install
    assert "$HOME/miniforge3/bin/python" in install
    assert "apt install" not in install


def test_linux_installer_autodetects_persistent_home_and_installs_reset_helper() -> None:
    install = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert "home_is_persistent_candidate" in install
    assert 'roots+=("$HOME"' in install
    assert 'home="$base/nvhive"' in install
    assert "install_uninstall_script" in install
    assert "$NVH_HOME/uninstall.sh" in install
    assert "nvh-uninstall" in install
    assert "# >>> nvhive rootless env >>>" in install


def test_linux_uninstaller_is_rootless_and_supports_purge_reset() -> None:
    uninstall = (ROOT / "uninstall.sh").read_text(encoding="utf-8")

    assert "--purge" in uninstall
    assert "--dry-run" in uninstall
    assert "sudo" not in uninstall
    assert "apt " not in uninstall
    assert "safe_to_remove_home" in uninstall
    assert 'remove_path "$NVH_HOME"' in uninstall
    assert "keep models/config/projects" in uninstall
