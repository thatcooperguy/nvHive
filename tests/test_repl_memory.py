"""REPL /remember, /forget and /memories on top of the vault (0.42).

``nvh/core/memory.py`` (``~/.hive/memory/memories.json``) is gone; the REPL
now writes ordinary vault notes under "Wizard Memory" tagged #repl, so the
Wizard's ``rag_ask_vault`` and Obsidian see the same memories.
"""

from __future__ import annotations

import pytest

from nvh.cli import repl


@pytest.fixture()
def vault_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NVH_HOME", str(tmp_path))
    for var in ("NVHIVE_HOME", "NVH_STATE"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


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
    out = capsys.readouterr().out
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
