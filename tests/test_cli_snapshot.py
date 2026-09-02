"""`nvh snapshot save|restore|list` over the NVH_HOME workspace bundle.

0.41.1 re-pointed the command from ``nvh.core.snapshot`` (which tarred
``~/.hive`` and ``~/.council``) at ``nvh.integrations.workspace.snapshot`` so
the README's "moves your whole state to a brand-new VM" promise holds.
"""

from __future__ import annotations

import importlib
import io
import sqlite3
import tarfile
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

import nvh.cli.main as cli_main


def _sqlite_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "nvh-home"
    (home / "vault").mkdir(parents=True)
    (home / "vault" / "notes.md").write_text("remember the migration plan")
    _sqlite_file(home / "rag" / "index.sqlite")
    (home / "receipts").mkdir()
    (home / "receipts" / "ollama.json").write_text("{}")
    _sqlite_file(home / "state" / "nvhive.db")
    (home / ".env").write_text("GROQ_API_KEY=gsk-secret\n")
    monkeypatch.setenv("NVH_HOME", str(home))
    for var in ("NVHIVE_HOME", "NVH_STATE", "HIVE_DATA_DIR", "HIVE_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    return home


def test_legacy_core_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("nvh.core.snapshot")


def test_save_list_restore_roundtrip(runner: CliRunner, home: Path, tmp_path: Path):
    out = tmp_path / "backups" / "state.tar.gz"

    result = runner.invoke(cli_main.app, ["snapshot", "save", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "Snapshot saved" in result.output
    assert "API keys are not bundled" in result.output

    result = runner.invoke(cli_main.app, ["snapshot", "list", str(out)])
    assert result.exit_code == 0, result.output
    assert "vault/notes.md" in result.output
    assert "state/nvhive.db" in result.output
    assert "receipts/ollama.json" in result.output
    assert ".env" not in result.output

    dest = tmp_path / "fresh-vm"
    result = runner.invoke(
        cli_main.app, ["snapshot", "restore", str(out), "--home-dir", str(dest)],
    )
    assert result.exit_code == 0, result.output
    assert "Restored" in result.output
    assert (dest / "vault" / "notes.md").read_text() == "remember the migration plan"
    assert (dest / "state" / "nvhive.db").exists()
    assert (dest / "rag" / "index.sqlite").exists()
    assert not (dest / ".env").exists()

    # Second restore without --overwrite leaves files alone and says so.
    (dest / "vault" / "notes.md").write_text("edited on the new VM")
    result = runner.invoke(
        cli_main.app, ["snapshot", "restore", "-o", str(out), "--home-dir", str(dest)],
    )
    assert result.exit_code == 0, result.output
    assert "skipped" in result.output
    assert (dest / "vault" / "notes.md").read_text() == "edited on the new VM"

    result = runner.invoke(
        cli_main.app,
        ["snapshot", "restore", str(out), "--home-dir", str(dest), "--overwrite"],
    )
    assert result.exit_code == 0, result.output
    assert (dest / "vault" / "notes.md").read_text() == "remember the migration plan"


def test_save_defaults_to_nvh_home_snapshots_dir(runner: CliRunner, home: Path):
    result = runner.invoke(cli_main.app, ["snapshot", "save"])
    assert result.exit_code == 0, result.output
    bundles = list((home / "snapshots").glob("snapshot-*.tar.gz"))
    assert len(bundles) == 1


def test_save_warns_when_home_is_empty(runner: CliRunner, tmp_path: Path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("NVH_HOME", str(empty))
    for var in ("NVH_STATE", "HIVE_DATA_DIR"):
        monkeypatch.delenv(var, raising=False)
    result = runner.invoke(cli_main.app, ["snapshot", "save", "-o", str(tmp_path / "s.tar.gz")])
    assert result.exit_code == 0, result.output
    assert "Nothing to bundle" in result.output


@pytest.mark.parametrize("action", ["restore", "list"])
def test_restore_and_list_require_a_file(runner: CliRunner, home: Path, action: str):
    result = runner.invoke(cli_main.app, ["snapshot", action])
    assert result.exit_code == 1
    assert "Usage: nvh snapshot" in result.output


def test_restore_missing_file_fails(runner: CliRunner, home: Path, tmp_path: Path):
    result = runner.invoke(cli_main.app, ["snapshot", "restore", str(tmp_path / "nope.tar.gz")])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_restore_non_tarball_fails_cleanly(runner: CliRunner, home: Path, tmp_path: Path):
    bad = tmp_path / "bad.tar.gz"
    bad.write_text("not a tarball")
    result = runner.invoke(cli_main.app, ["snapshot", "restore", str(bad)])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "unreadable snapshot" in result.output


def test_restore_refuses_legacy_0_41_0_archive(runner: CliRunner, home: Path, tmp_path: Path):
    legacy = tmp_path / "legacy.tar.gz"
    with tarfile.open(legacy, mode="w:gz") as tar:
        payload = b"providers:\n  openai:\n    api_key: sk-raw\n"
        info = tarfile.TarInfo(name=".hive/config.yaml")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    result = runner.invoke(cli_main.app, ["snapshot", "restore", str(legacy)])
    assert result.exit_code == 1
    assert "snapshot.json" in result.output
    assert not (home / ".hive").exists()


def test_unknown_action_fails(runner: CliRunner, home: Path):
    result = runner.invoke(cli_main.app, ["snapshot", "explode"])
    assert result.exit_code == 1
    assert "Unknown action" in result.output
