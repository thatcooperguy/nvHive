"""REPL /remember, /forget and /memories on top of the vault (0.42).

``nvh/core/memory.py`` (``~/.hive/memory/memories.json``) is gone; the REPL
now writes ordinary vault notes under "Wizard Memory" tagged #repl, so the
Wizard's ``rag_ask_vault`` and Obsidian see the same memories. The orphaned
``memories.json`` is imported into the vault once, at REPL start or via
``nvh rag import-legacy --memories``.
"""

from __future__ import annotations

import json
import re

import pytest
from typer.testing import CliRunner

from nvh.cli import repl

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    # Rich colours output on CI (FORCE_COLOR); substring checks need the de-styled text.
    return _ANSI.sub("", text)


@pytest.fixture()
def vault_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NVH_HOME", str(tmp_path))
    for var in ("NVHIVE_HOME", "NVH_STATE"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@pytest.fixture()
def legacy_memories(tmp_path, monkeypatch):
    """A pre-0.42 ``memories.json`` (the deleted MemoryStore's on-disk shape)."""
    path = tmp_path / "old-hive" / "memory" / "memories.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([
        {
            "id": "a1b2c3d4", "type": "project", "content": "Project uses Python 3.12 and pytest",
            "created_at": "2026-01-01T00:00:00+00:00", "source": "user", "relevance": 1.0,
            "access_count": 3, "last_accessed": "2026-02-01T00:00:00+00:00",
        },
        {"id": "e5f6a7b8", "type": "user", "content": "Prefers concise answers"},
        {"id": "c9d0e1f2", "type": "fact", "content": "   "},
        "not a row",
    ]), encoding="utf-8")
    monkeypatch.setattr(repl, "legacy_memory_file", lambda: path)
    return path


def _session() -> repl.ReplSession:
    return repl.ReplSession(
        provider=None, model=None, council_mode=False,
        auto_agents=False, preset=None, system_prompt=None,
    )


def test_remember_writes_tagged_vault_note(vault_home):
    repl._handle_command("/remember Project uses Python 3.12 and pytest", _session())

    notes = repl._repl_memory_notes()
    assert len(notes) == 1
    path, title, body = notes[0]
    assert path.parent == vault_home / "vault" / "Wizard Memory"
    assert title == "Project uses Python 3.12 and pytest"
    assert body == "Project uses Python 3.12 and pytest"
    assert "#repl" in path.read_text(encoding="utf-8")
    # The seeded product note shares the folder but is not a REPL memory.
    assert (path.parent / "AI Wizard.md").exists()


def test_startup_context_and_forget_only_touch_repl_notes(vault_home):
    session = _session()
    repl._handle_command("/remember Prefers concise answers", session)
    repl._handle_command("/remember Tests run with pytest -q", session)

    context = repl._repl_memory_context()
    assert context.startswith("<memory>")
    assert "Prefers concise answers" in context
    assert "Tests run with pytest -q" in context

    assert repl._forget_repl_memories("pytest") == 1
    assert repl._forget_repl_memories("nothing-matches") == 0
    remaining = repl._repl_memory_notes()
    assert [body for _, _, body in remaining] == ["Prefers concise answers"]
    assert (vault_home / "vault" / "Wizard Memory" / "AI Wizard.md").exists()


def test_memories_lists_notes_or_defers_query_to_vault_search(vault_home, capsys):
    session = _session()
    # True = "keep the REPL running" (nothing to await)
    assert repl._handle_command("/memories", session) is True
    repl._handle_command("/remember Long title " + "x" * 80, session)
    repl._handle_command("/memories", session)
    out = _plain(capsys.readouterr().out)
    assert "Memory notes (1)" in out
    assert "Long title" in out
    # With a query the REPL loop awaits the RAG search over the whole vault.
    assert repl._handle_command("/memories pytest", session) == ("memories", "pytest")


def test_memory_title_truncates_first_line():
    assert repl._memory_title("short") == "short"
    long = "a" * 100 + "\nsecond line"
    title = repl._memory_title(long)
    assert len(title) == 60 and title.endswith("...")


def test_parse_memory_note_round_trips_vault_format():
    note = "# Title here\n\nCreated: now\nCategory: Wizard Memory\nTags: #repl #other\n\nbody line 1\nbody line 2\n"
    title, tags, body = repl._parse_memory_note(note)
    assert title == "Title here"
    assert tags == ["repl", "other"]
    assert body == "body line 1\nbody line 2"


def test_legacy_memories_import_once_as_tagged_notes(vault_home, legacy_memories):
    result = repl.import_legacy_memories()
    assert result["found"] is True and result["imported"] == 2 and result["imported_at"] is None

    notes = repl._repl_memory_notes()
    assert sorted(body for _, _, body in notes) == ["Prefers concise answers", "Project uses Python 3.12 and pytest"]
    by_body = {body: path.read_text(encoding="utf-8") for path, _, body in notes}
    assert "#repl #project" in by_body["Project uses Python 3.12 and pytest"]
    assert "#repl #user" in by_body["Prefers concise answers"]
    # The REPL's startup context sees them like any /remember note.
    assert "Prefers concise answers" in repl._repl_memory_context()

    marker = vault_home / "state" / "legacy-memories-imported.json"
    assert json.loads(marker.read_text(encoding="utf-8"))["imported"] == 2
    again = repl.import_legacy_memories()
    assert again["imported"] == 0 and again["imported_at"]
    assert len(repl._repl_memory_notes()) == 2


def test_legacy_memories_import_without_a_file_is_a_noop(vault_home, tmp_path, monkeypatch):
    monkeypatch.setattr(repl, "legacy_memory_file", lambda: tmp_path / "nope.json")
    result = repl.import_legacy_memories()
    assert (result["found"], result["imported"], result["imported_at"]) == (False, 0, None)
    assert not (vault_home / "state" / "legacy-memories-imported.json").exists()


def test_rag_import_legacy_memories_command(vault_home, legacy_memories):
    import nvh.cli.main as cli_main

    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["rag", "import-legacy", "--memories"])
    assert result.exit_code == 0, result.output
    assert "Imported 2 legacy memories into the vault" in _plain(result.output)
    assert len(repl._repl_memory_notes()) == 2

    again = runner.invoke(cli_main.app, ["rag", "import-legacy", "--memories"])
    assert again.exit_code == 0 and "Already imported" in _plain(again.output)
    assert len(repl._repl_memory_notes()) == 2
