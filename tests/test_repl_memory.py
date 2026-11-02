"""REPL /remember, /forget and /memories on top of the vault (0.42).

``nvh/core/memory.py`` (``~/.hive/memory/memories.json``) is gone; the REPL
now writes ordinary vault notes under "Wizard Memory" tagged #repl, so the
Wizard's ``rag_ask_vault`` and Obsidian see the same memories. The orphaned
``memories.json`` is imported into the vault once, at REPL start or via
``nvh rag import-legacy --memories``.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from unittest.mock import AsyncMock, MagicMock

import pytest
from typer.testing import CliRunner

from nvh.cli import repl

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# The module's own best-effort RAG hooks, captured before the autouse fixture
# below replaces them for every test.
_REAL_INDEX = repl._index_memory_note
_REAL_FORGET = repl._forget_memory_index


def _plain(text: str) -> str:
    # Rich colours output on CI (FORCE_COLOR); substring checks need the de-styled text.
    return _ANSI.sub("", text)


@pytest.fixture()
def vault_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NVH_HOME", str(tmp_path))
    for var in ("NVHIVE_HOME", "NVH_STATE"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def rag_calls(monkeypatch):
    """Keep /remember and /forget off the embedder: record the RAG hook calls instead."""
    calls: dict[str, list[str]] = {"index": [], "forget": []}
    monkeypatch.setattr(repl, "_index_memory_note", lambda path: calls["index"].append(str(path)))
    monkeypatch.setattr(repl, "_forget_memory_index", lambda path: calls["forget"].append(str(path)))
    return calls


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
    # 0.46: imported notes are nvh-memory/1 records that name their writer.
    assert all("Source: import" in text and "Schema: nvh-memory/1" in text for text in by_body.values())
    # The REPL's startup context sees them like any /remember note.
    assert "Prefers concise answers" in repl._repl_memory_context()

    marker = vault_home / "state" / "legacy-memories-imported.json"
    assert json.loads(marker.read_text(encoding="utf-8"))["imported"] == 2
    again = repl.import_legacy_memories()
    assert again["imported"] == 0 and again["imported_at"]
    assert len(repl._repl_memory_notes()) == 2


def test_legacy_memory_file_lives_under_the_layout_state_dir(vault_home):
    """0.44: the pre-0.42 memories.json is read from ``$NVH_STATE/memory/`` — where
    the legacy-home migration copies ``~/.hive/memory/memories.json`` — never from $HOME."""
    assert repl.legacy_memory_file() == vault_home / "state" / "memory" / "memories.json"


def test_legacy_memories_import_without_a_file_is_a_noop(vault_home, tmp_path, monkeypatch):
    monkeypatch.setattr(repl, "legacy_memory_file", lambda: tmp_path / "nope.json")
    result = repl.import_legacy_memories()
    assert (result["found"], result["imported"], result["imported_at"]) == (False, 0, None)
    assert not (vault_home / "state" / "legacy-memories-imported.json").exists()


# ---------------------------------------------------------------------------
# nvh-memory/1 records (0.46): Source: repl, redaction, one-note indexing
# ---------------------------------------------------------------------------


def test_remember_writes_a_record_and_indexes_just_that_note(vault_home, rag_calls):
    repl._handle_command("/remember Project uses Python 3.12 and pytest", _session())

    (path, _, _), = repl._repl_memory_notes()
    text = path.read_text(encoding="utf-8")
    assert "Schema: nvh-memory/1" in text and "Source: repl" in text
    assert re.search(r"^Id: [0-9a-f]{32}$", text, re.M)
    assert rag_calls["index"] == [str(path)] and rag_calls["forget"] == []


def test_remember_refuses_a_secret_only_note(vault_home, capsys, rag_calls):
    repl._handle_command("/remember sk-" + "q7" * 16, _session())

    out = _plain(capsys.readouterr().out)
    assert "nothing to remember after redacting secrets" in out
    assert repl._repl_memory_notes() == [] and rag_calls["index"] == []


def test_remember_reports_redacted_secrets(vault_home, capsys):
    # The key sits past the 60-character title cut, so only the body carries it.
    prefix = "Staging provider account for the rotation runbook, kept in config: "
    assert len(prefix) > 60
    repl._handle_command("/remember " + prefix + "sk-" + "q7" * 16, _session())

    out = " ".join(_plain(capsys.readouterr().out).split())  # Rich wraps at the console width
    assert "Memory saved to vault" in out and "(1 secret redacted)" in out
    (path, title, body), = repl._repl_memory_notes()
    assert "[REDACTED:api_key]" in body and "q7q7" not in path.read_text(encoding="utf-8")
    assert "REDACTED" not in title


def test_forget_drops_the_file_and_its_index(vault_home, rag_calls):
    session = _session()
    repl._handle_command("/remember Tests run with pytest -q", session)
    repl._handle_command("/remember Prefers concise answers", session)
    paths = {body: path for path, _, body in repl._repl_memory_notes()}

    assert repl._forget_repl_memories("pytest") == 1

    assert not paths["Tests run with pytest -q"].exists()
    assert paths["Prefers concise answers"].exists()
    assert rag_calls["forget"] == [str(paths["Tests run with pytest -q"])]


def test_memory_hooks_never_raise(vault_home, monkeypatch, tmp_path):
    import nvh.integrations.rag.vault_bridge as bridge

    monkeypatch.setattr(bridge, "index_vault_note", AsyncMock(side_effect=RuntimeError("embedder down")))
    monkeypatch.setattr(bridge, "forget_vault_note", AsyncMock(side_effect=RuntimeError("store locked")))
    _REAL_INDEX(tmp_path / "note.md")
    _REAL_FORGET(tmp_path / "note.md")
    assert bridge.index_vault_note.await_count == 1 and bridge.forget_vault_note.await_count == 1


@pytest.mark.asyncio
async def test_memory_hooks_schedule_on_a_running_loop(vault_home, monkeypatch, tmp_path):
    import nvh.integrations.rag.vault_bridge as bridge

    seen: list[str] = []

    async def fake_index(path, **_kwargs):
        seen.append(str(path))
        return True

    monkeypatch.setattr(bridge, "index_vault_note", fake_index)
    _REAL_INDEX(tmp_path / "note.md")
    assert seen == []  # scheduled, not run inline, so the REPL prompt never waits on the embedder
    await asyncio.gather(*list(repl._BACKGROUND_TASKS))
    assert seen == [str(tmp_path / "note.md")]
    assert not repl._BACKGROUND_TASKS


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


# ---------------------------------------------------------------------------
# 7a review fixes: title redaction, path-based /forget, tolerant import, exit drain
# ---------------------------------------------------------------------------


def test_remember_title_never_keeps_a_truncated_key_fragment(vault_home, capsys):
    """45 characters of prose put the 60-character title cut in the middle of the
    key: the title is derived from the redacted text, so no fragment of the key
    survives in the heading, the file name or the note."""
    prefix = "note for the deploy runbook, prod api key is "
    key = "sk-A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
    repl._handle_command("/remember " + prefix + key, _session())

    (path, title, body), = repl._repl_memory_notes()
    text = path.read_text(encoding="utf-8")
    assert title.startswith(prefix) and title.endswith("...") and "[REDACTED:" in title
    for fragment in ("sk-A1b2", "A1b2C3d4", key):
        assert fragment not in title and fragment not in path.name and fragment not in text
    assert "[REDACTED:api_key]" in body
    assert "(1 secret redacted)" in " ".join(_plain(capsys.readouterr().out).split())
    # The same cut on the import path.
    assert "A1b2" not in repl._memory_title(prefix + key)


def test_forget_unlinks_the_matched_file_even_when_another_note_shares_its_id(vault_home, rag_calls):
    """An Obsidian duplicate or sync-conflict copy keeps the ``Id:`` line. /forget
    unlinks the file it matched, so the copy that sorts first in a vault-wide id
    lookup is not the one deleted in its place."""
    repl._handle_command("/remember Tests run with pytest -q", _session())
    (path, _, _), = repl._repl_memory_notes()
    conflict = path.with_name(path.stem + " (conflict).md")
    shutil.copyfile(path, conflict)
    elsewhere = vault_home / "vault" / "Decisions" / "copied.md"
    shutil.copyfile(path, elsewhere)

    assert repl._forget_repl_memories("pytest") == 2

    assert not path.exists() and not conflict.exists()
    assert elsewhere.exists(), "/forget only touches #repl notes under Wizard Memory"
    assert sorted(rag_calls["forget"]) == sorted([str(path), str(conflict)])
    assert repl._repl_memory_notes() == [] and "pytest" not in repl._repl_memory_context()


def test_legacy_import_skips_a_secret_only_row_and_still_writes_the_marker(vault_home, legacy_memories):
    rows = json.loads(legacy_memories.read_text(encoding="utf-8"))
    rows.insert(1, {"id": "deadbeef", "type": "fact", "content": "sk-" + "q7" * 16})
    legacy_memories.write_text(json.dumps(rows), encoding="utf-8")

    result = repl.import_legacy_memories()

    assert (result["imported"], result["skipped"]) == (2, 1)
    assert sorted(body for _, _, body in repl._repl_memory_notes()) == [
        "Prefers concise answers", "Project uses Python 3.12 and pytest",
    ]
    marker = json.loads((vault_home / "state" / "legacy-memories-imported.json").read_text(encoding="utf-8"))
    assert (marker["imported"], marker["skipped"]) == (2, 1)
    again = repl.import_legacy_memories()
    assert again["imported"] == 0 and again["imported_at"]
    assert len(repl._repl_memory_notes()) == 2, "the marker stops a second import"


@pytest.mark.asyncio
async def test_drain_background_tasks_waits_for_the_index_hook(monkeypatch, tmp_path):
    import nvh.integrations.rag.vault_bridge as bridge

    seen: list[str] = []

    async def slow_index(path, **_kwargs):
        await asyncio.sleep(0.01)  # the embedder's network call
        seen.append(str(path))
        return True

    monkeypatch.setattr(bridge, "index_vault_note", slow_index)
    _REAL_INDEX(tmp_path / "note.md")
    assert seen == []
    await repl._drain_background_tasks()
    assert seen == [str(tmp_path / "note.md")] and not repl._BACKGROUND_TASKS
    await repl._drain_background_tasks()  # nothing pending: a no-op


def test_remember_then_exit_still_indexes_the_note(vault_home, monkeypatch, tmp_path, capsys):
    """``/remember`` then ``/exit``: ``asyncio.run`` used to cancel the index task
    at its first await, so the note was saved but never reached RAG (and
    ``ensure_vault_indexed`` re-ingests only an empty collection). ``run_repl``
    drains the hooks before it returns, on both the ``/exit`` and the EOF path."""
    import nvh.integrations.rag.vault_bridge as bridge

    indexed: list[str] = []

    async def slow_index(path, **_kwargs):
        await asyncio.sleep(0.01)
        indexed.append(str(path))
        return True

    monkeypatch.setattr(bridge, "index_vault_note", slow_index)
    monkeypatch.setattr(repl, "_index_memory_note", _REAL_INDEX)  # past the autouse recorder
    monkeypatch.setattr(repl, "_startup_check_ollama_models", lambda engine: None)
    engine = MagicMock()
    engine.initialize = AsyncMock()

    for exit_line in ("/exit", None):
        indexed.clear()
        before = {path for path, _, _ in repl._repl_memory_notes()}
        lines = iter([f"/remember Tests run with pytest -q {exit_line}", exit_line])
        monkeypatch.setattr(repl, "_get_input", lambda session: next(lines))
        asyncio.run(repl.run_repl(engine))
        (new_note,) = {path for path, _, _ in repl._repl_memory_notes()} - before
        assert indexed == [str(new_note)], exit_line
        assert not repl._BACKGROUND_TASKS
