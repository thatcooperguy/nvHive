"""The ``nvh vault`` group (0.46): status, init, list, forget, upgrade — the CLI contract.

The library functions are pinned in tests/test_vault_integration.py; this file drives the Typer
commands in-process so the exit codes, the ``forget_vault_note`` call after a delete and the
``--memory-only`` / ``--add-ids`` / ``--dry-run`` wiring are exercised too. Hermetic: the home is
tests/conftest.py's throwaway ``NVH_HOME``; the RAG forgetter is doubled, so nothing touches an
embedder or a store.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import nvh.cli.main as cli_main
from nvh.integrations.workspace import vault
from nvh.integrations.workspace.storage import storage_layout


@pytest.fixture()
def home(monkeypatch) -> Path:
    """The process home every ``nvh vault`` command reads (conftest's hermetic ``NVH_HOME``).

    Rich wraps long paths at 80 columns; a wide terminal keeps each line whole.
    """
    monkeypatch.setenv("COLUMNS", "300")
    return storage_layout().home


def _legacy_note(home: Path, name: str = "2026-01-01T000000Z0000-Old-note.md") -> Path:
    vault.init_vault(home)
    path = home / "vault" / "Wizard Memory" / name
    path.write_text(
        "# Old note\n\nCreated: 2026-01-01T00:00:00+00:00\nCategory: Wizard Memory\nTags: #repl #old\n\nbody\n",
        encoding="utf-8",
    )
    return path


def test_status_then_init_then_status(home: Path) -> None:
    cli = CliRunner()
    before = cli.invoke(cli_main.app, ["vault", "status"])
    assert before.exit_code == 0, before.output
    assert "not initialised" in before.output and "nvh vault init" in before.output
    init = cli.invoke(cli_main.app, ["vault", "init"])
    assert init.exit_code == 0, init.output
    assert "Vault ready" in init.output and (home / "vault" / "Wizard Memory").is_dir()
    after = cli.invoke(cli_main.app, ["vault", "status"])
    assert after.exit_code == 0 and "initialised" in after.output and "not initialised" not in after.output


def test_upgrade_without_add_ids_exits_1_and_writes_nothing(home: Path) -> None:
    path = _legacy_note(home)
    before = path.read_bytes()
    result = CliRunner().invoke(cli_main.app, ["vault", "upgrade"])
    assert result.exit_code == 1, result.output
    assert "Nothing selected" in result.output and "--add-ids" in result.output
    assert path.read_bytes() == before


def test_upgrade_dry_run_writes_nothing_then_the_real_run_upgrades_once(home: Path) -> None:
    path = _legacy_note(home)
    before = path.read_bytes()
    cli = CliRunner()
    dry = cli.invoke(cli_main.app, ["vault", "upgrade", "--add-ids", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "Would upgrade 1 note(s)" in dry.output and path.read_bytes() == before
    real = cli.invoke(cli_main.app, ["vault", "upgrade", "--add-ids"])
    assert real.exit_code == 0, real.output
    assert "Upgraded 1 note(s)" in real.output
    text = path.read_text(encoding="utf-8")
    assert f"Id: {vault.derived_note_id(path)}" in text and "Schema: nvh-memory/1" in text and "Source: repl" in text
    again = cli.invoke(cli_main.app, ["vault", "upgrade", "--add-ids"])
    assert again.exit_code == 0 and "Upgraded 0 note(s)" in again.output


def test_upgrade_exits_1_when_a_note_cannot_be_read(home: Path) -> None:
    vault.init_vault(home)
    bad = home / "vault" / "Wizard Memory" / "bad.md"
    bad.write_bytes(b"# Bad\n\nCreated: x\nCategory: Wizard Memory\nTags: none\n\n\xff\xfe not utf-8\n")
    result = CliRunner().invoke(cli_main.app, ["vault", "upgrade", "--add-ids"])
    assert result.exit_code == 1, result.output
    assert "1 error(s)" in result.output and "could not read" in result.output


def test_forget_deletes_the_file_then_drops_its_chunks(home: Path, monkeypatch) -> None:
    import nvh.integrations.rag as rag

    vault.init_vault(home)
    record = vault.append_vault_memory("Remember me", "a 0.46 record", home_dir=home)
    seen: list[tuple[str, object]] = []

    async def _forget(path, *, home_dir=None):
        seen.append((str(path), home_dir))
        return 3

    monkeypatch.setattr(rag, "forget_vault_note", _forget)
    result = CliRunner().invoke(cli_main.app, ["vault", "forget", record["id"]])
    assert result.exit_code == 0, result.output
    assert not Path(record["path"]).exists()
    assert seen == [(record["path"], None)], "the forgetter gets the deleted note's path, process home"
    assert "Forgot:" in result.output and "3 chunk(s) removed" in result.output


def test_forget_still_removes_the_file_when_the_index_drop_fails(home: Path, monkeypatch) -> None:
    import nvh.integrations.rag as rag

    vault.init_vault(home)
    record = vault.append_vault_memory("Remember me", "a 0.46 record", home_dir=home)

    async def _boom(path, *, home_dir=None):
        raise RuntimeError("no store")

    monkeypatch.setattr(rag, "forget_vault_note", _boom)
    result = CliRunner().invoke(cli_main.app, ["vault", "forget", record["id"]])
    assert result.exit_code == 0, result.output
    assert not Path(record["path"]).exists()
    assert "RAG chunks not dropped" in result.output and "0 chunk(s) removed" in result.output


def test_forget_unknown_id_exits_1(home: Path, monkeypatch) -> None:
    import nvh.integrations.rag as rag

    called = []
    monkeypatch.setattr(rag, "forget_vault_note", lambda *a, **k: called.append(1))
    vault.init_vault(home)
    result = CliRunner().invoke(cli_main.app, ["vault", "forget", "0123456789abcdef0123456789abcdef"])
    assert result.exit_code == 1, result.output
    assert "unknown note id" in result.output and called == []


def test_list_filters_memory_only_tag_and_category(home: Path, monkeypatch) -> None:
    monkeypatch.setenv("COLUMNS", "220")
    vault.init_vault(home)
    record = vault.append_vault_memory("Remember me", "a 0.46 record", tags=["gpu"], home_dir=home)
    cli = CliRunner()
    full = cli.invoke(cli_main.app, ["vault", "list", "--limit", "500"])
    assert full.exit_code == 0, full.output
    assert record["id"] in full.output and "AI Wizard" in full.output, "seeded doc notes show without the flag"
    memory = cli.invoke(cli_main.app, ["vault", "list", "--memory-only", "--limit", "500"])
    assert memory.exit_code == 0, memory.output
    assert record["id"] in memory.output and "AI Wizard" not in memory.output and "1 note(s)" in memory.output
    tagged = cli.invoke(cli_main.app, ["vault", "list", "--tag", "gpu", "--limit", "500"])
    assert tagged.exit_code == 0 and record["id"] in tagged.output and "1 note(s)" in tagged.output
    none = cli.invoke(cli_main.app, ["vault", "list", "--category", "Nowhere"])
    assert none.exit_code == 0 and "No notes match" in none.output
