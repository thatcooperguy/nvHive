"""Tests for rootless nvHive Vault memory helpers."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from nvh.api.server import app
from nvh.integrations import vault


def test_init_vault_creates_markdown_memory_tree(tmp_path) -> None:
    home = tmp_path / "nvhive"

    result = vault.init_vault(home)

    vault_dir = Path(result["vault_dir"])
    assert result["initialized"] is True
    assert vault_dir == home / "vault"
    assert (vault_dir / "README.md").exists()
    assert (vault_dir / "Wizard Memory" / "nvWizard.md").exists()
    assert (vault_dir / "Conflicts" / "Product Conflicts.md").exists()
    assert (vault_dir / "Process Playbooks" / "Pilot Test Checklist.md").exists()
    assert (vault_dir / ".obsidian" / "app.json").exists()
    assert result["markdown_files"] >= 5
    assert any(item["title"] == "OS disk vs persistent mount" for item in result["conflicts"])


def test_append_vault_memory_writes_safe_markdown(tmp_path) -> None:
    home = tmp_path / "nvhive"

    result = vault.append_vault_memory(
        "GPU VM test: ready?",
        "The 48 GB RTX profile installed the local model and passed the quick test.",
        category="Troubleshooting",
        tags=["pilot", "gpu"],
        home_dir=home,
    )

    path = Path(result["path"])
    assert result["saved"] is True
    assert result["category"] == "Troubleshooting"
    assert path.exists()
    assert path.parent == home / "vault" / "Troubleshooting"
    text = path.read_text(encoding="utf-8")
    assert "# GPU VM test: ready?" in text
    assert "#pilot #gpu" in text


def test_unknown_memory_category_falls_back_to_wizard_memory(tmp_path) -> None:
    home = tmp_path / "nvhive"

    result = vault.append_vault_memory(
        "Do not create arbitrary folders",
        "Unknown categories should land in the safe default.",
        category="../Secrets",
        home_dir=home,
    )

    assert result["category"] == "Wizard Memory"
    assert Path(result["path"]).parent == home / "vault" / "Wizard Memory"


def test_obsidian_status_reports_optional_rootless_app(tmp_path, monkeypatch) -> None:
    home = tmp_path / "nvhive"
    monkeypatch.setattr(vault.shutil, "which", lambda _name: None)

    status = vault.obsidian_status(home)

    assert status["installed"] is False
    assert status["rootless_app"] is False
    assert status["vault_dir"] == str(home / "vault")
    assert status["launcher_path"] == str(home / "bin" / "nvhive-vault")
    assert status["download_url"] == vault.OBSIDIAN_DOWNLOAD_PAGE


def test_vault_api_routes_create_rootless_vault(tmp_path) -> None:
    home = tmp_path / "nvhive"
    client = TestClient(app)

    init_response = client.post("/v1/vault/init", json={"home_dir": str(home)})
    assert init_response.status_code == 200
    init_data = init_response.json()["data"]
    assert init_data["initialized"] is True
    assert init_data["vault_dir"] == str(home / "vault")

    memory_response = client.post(
        "/v1/vault/memory",
        json={
            "home_dir": str(home),
            "title": "API memory",
            "body": "The API writes to the vault without root.",
            "category": "Decisions",
        },
    )
    assert memory_response.status_code == 200
    memory_data = memory_response.json()["data"]
    assert memory_data["saved"] is True
    assert Path(memory_data["path"]).exists()
