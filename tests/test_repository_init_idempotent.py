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
    # A real ~/.council/council.db on the dev box would be auto-copied into
    # the wiped path by init_db's legacy migration, breaking hermeticity
    monkeypatch.setattr(
        repo, "_legacy_db_path", lambda: tmp_path / "no-legacy" / "council.db"
    )
    await repo.close_db()
    yield tmp_path
    await repo.close_db()


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
