"""Regression tests for init_db() idempotency and engine disposal.

Several DAO functions call ``await init_db()`` on every invocation, so a
repeat call against the same database path must be a no-op — previously it
rebuilt the engine each time and leaked the old connection pool. A call
against a *different* path (tests repointing NVH_HOME) must still perform a
full re-init, disposing the previous engine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nvh.storage import repository as repo


@pytest.fixture()
async def nvh_home(tmp_path: Path, monkeypatch):
    """A throwaway $NVH_HOME with clean repository module state."""
    monkeypatch.setenv("NVH_HOME", str(tmp_path))
    monkeypatch.delenv("HIVE_DATA_DIR", raising=False)
    monkeypatch.delenv("NVH_STATE", raising=False)
    monkeypatch.delenv("NVHIVE_HOME", raising=False)
    # tests/conftest.py turns the legacy migration off; a fake OS home keeps
    # the dev box's real ~/.council out of the picture even if it were on.
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "no-home")
    await repo.close_db()
    yield tmp_path
    await repo.close_db()


def test_default_db_path_is_the_layout_state_dir(tmp_path: Path, monkeypatch) -> None:
    """0.44: ``_default_db_path`` is ``storage_layout().state_dir / nvhive.db``;
    NVH_STATE relocates it and the pre-0.44 ``HIVE_DATA_DIR`` is ignored."""
    for var in ("HIVE_DATA_DIR", "NVH_STATE", "NVHIVE_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvhive"))
    assert repo._default_db_path() == tmp_path / "nvhive" / "state" / "nvhive.db"

    monkeypatch.setenv("NVH_STATE", str(tmp_path / "state"))
    assert repo._default_db_path() == tmp_path / "state" / "nvhive.db"

    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "data"))
    assert repo._default_db_path() == tmp_path / "state" / "nvhive.db"


def test_repository_no_longer_spells_the_legacy_root() -> None:
    """The ``~/.council`` import belongs to migrate_legacy.py alone (D7): the
    repository has no legacy path of its own and no second copy of the move."""
    source = Path(repo.__file__).read_text(encoding="utf-8")
    assert "_legacy_db_path" not in source and "council" not in source.lower().replace("councilconfig", "")
    assert "migrate_legacy_homes" in source


async def test_init_db_imports_the_legacy_council_db_through_the_one_migration(tmp_path: Path, monkeypatch) -> None:
    """The first default-path ``init_db()`` with no database runs the one-shot
    migration, which brings ``~/.council/council.db`` in and writes the marker.
    Once the marker exists a database the user deletes is created fresh —
    the stale legacy file is never re-read (no silent resurrection)."""
    import json
    import sqlite3

    from nvh.integrations.workspace import migrate_legacy as ml

    for var in ("HIVE_DATA_DIR", "NVH_STATE", "NVHIVE_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvhive"))
    monkeypatch.setenv(ml.LEGACY_MIGRATION_ENV, "1")
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    legacy = home / ".council" / "council.db"
    legacy.parent.mkdir(parents=True)
    conn = sqlite3.connect(legacy)
    try:
        conn.execute("CREATE TABLE legacy_marker (x INTEGER)")
        conn.execute("INSERT INTO legacy_marker VALUES (1)")
        conn.commit()
    finally:
        conn.close()
    stamp = legacy.stat().st_mtime_ns

    def _tables(path: Path) -> set[str]:
        # An explicit close: ``with sqlite3.connect()`` only commits, and an
        # open handle blocks the unlink below on Windows.
        probe = sqlite3.connect(path)
        try:
            return {row[0] for row in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            probe.close()

    await repo.close_db()
    try:
        await repo.init_db()
        db_path = repo._db_path
        assert db_path == tmp_path / "nvhive" / "state" / "nvhive.db"
        assert "legacy_marker" in _tables(db_path)
    finally:
        await repo.close_db()
    assert legacy.stat().st_mtime_ns == stamp  # the legacy file is read, never written
    marker = json.loads((tmp_path / "nvhive" / "state" / ml.MARKER_NAME).read_text(encoding="utf-8"))
    assert "state database" in {entry["label"] for entry in marker["moved"]}

    # The user starts clean: the next init creates an empty database.
    for sidecar in db_path.parent.glob("nvhive.db*"):
        sidecar.unlink()
    try:
        await repo.init_db()
        tables = _tables(db_path)
        assert "conversations" in tables and "legacy_marker" not in tables
    finally:
        await repo.close_db()


async def test_failed_legacy_snapshot_does_not_create_an_empty_database(nvh_home, monkeypatch):
    import sqlite3
    from contextlib import closing

    from nvh.integrations.workspace import migrate_legacy as ml

    home = nvh_home / "fake-home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv(ml.LEGACY_MIGRATION_ENV, "1")
    legacy = home / ".council" / "council.db"
    legacy.parent.mkdir(parents=True)
    destination = repo._default_db_path()
    with closing(sqlite3.connect(legacy)) as writer:
        writer.execute("CREATE TABLE legacy_marker(x INTEGER)")
        writer.execute("INSERT INTO legacy_marker VALUES(1)")
        writer.commit()
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("INSERT INTO legacy_marker VALUES(2)")
        with monkeypatch.context() as clock:
            times = iter((0.0, ml._SQLITE_BACKUP_SECONDS + 1))
            clock.setattr(ml, "monotonic", lambda: next(times))
            with pytest.raises(RuntimeError, match="Legacy state database import failed"):
                await repo.init_db()
        assert not destination.exists()
        assert repo._engine is None
        assert not (destination.parent / ml.MARKER_NAME).exists()
        writer.rollback()

    await repo.init_db()
    with closing(sqlite3.connect(destination)) as imported:
        assert imported.execute("SELECT x FROM legacy_marker").fetchall() == [(1,)]
        assert imported.execute("SELECT name FROM sqlite_master WHERE name='conversations'").fetchone()
    assert (destination.parent / ml.MARKER_NAME).is_file()


async def test_repeat_init_same_path_reuses_engine(nvh_home):
    await repo.init_db()
    first_engine = repo._engine
    conv = await repo.create_conversation(title="before second init")

    await repo.init_db()

    assert repo._engine is first_engine
    fetched = await repo.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.title == "before second init"


async def test_repointed_path_reinitializes_and_disposes(
    nvh_home, tmp_path_factory, monkeypatch
):
    await repo.init_db()
    old_engine = repo._engine
    conv = await repo.create_conversation(title="lives in old db")

    # AsyncEngine instances reject attribute assignment, so spy on the class.
    disposed: list[object] = []
    original_dispose = type(old_engine).dispose

    async def spy_dispose(self, *args, **kwargs):
        disposed.append(self)
        return await original_dispose(self, *args, **kwargs)

    monkeypatch.setattr(type(old_engine), "dispose", spy_dispose)
    monkeypatch.setenv("NVH_HOME", str(tmp_path_factory.mktemp("repointed")))

    await repo.init_db()

    assert repo._engine is not old_engine
    assert old_engine in disposed
    assert repo._engine not in disposed
    assert await repo.get_conversation(conv.id) is None


async def test_wiped_db_file_triggers_reinit(nvh_home):
    # Before the idempotency guard, every DAO-level init_db() re-ran
    # create_all, transparently healing an externally deleted DB file.
    # The guard must preserve that: same path + missing file = full re-init.
    await repo.init_db()
    first_engine = repo._engine
    db_path = repo._db_path
    await repo.create_conversation(title="doomed")

    # Release file handles (Windows blocks unlink on open files) without
    # touching module state, so the idempotency guard still sees a live
    # engine pointed at this path — then delete the file out from under it.
    await first_engine.dispose()
    db_path.unlink()

    await repo.init_db()
    assert repo._engine is not None
    assert repo._engine is not first_engine
    conv = await repo.create_conversation(title="after wipe")
    assert await repo.get_conversation(conv.id) is not None


async def test_failed_reinit_leaves_no_half_initialized_state(
    nvh_home, tmp_path_factory, monkeypatch
):
    # A re-init that fails during table creation must not leave _engine
    # pointing at the new DB while _db_path still names the old one —
    # later init_db() calls would early-return against the wrong database.
    await repo.init_db()
    old_engine = repo._engine
    old_path = repo._db_path

    async def boom(conn):
        raise RuntimeError("create_all failed")

    monkeypatch.setattr(repo, "_ensure_conversation_columns", boom)
    monkeypatch.setenv("NVH_HOME", str(tmp_path_factory.mktemp("broken")))

    with pytest.raises(RuntimeError, match="create_all failed"):
        await repo.init_db()

    assert repo._engine is old_engine
    assert repo._db_path == old_path
    conv = await repo.create_conversation(title="old engine still works")
    assert await repo.get_conversation(conv.id) is not None


async def test_close_db_then_init_yields_fresh_engine(nvh_home):
    await repo.init_db()
    first_engine = repo._engine

    await repo.close_db()
    assert repo._engine is None

    await repo.init_db()
    assert repo._engine is not None
    assert repo._engine is not first_engine
    conv = await repo.create_conversation(title="after reinit")
    fetched = await repo.get_conversation(conv.id)
    assert fetched is not None
