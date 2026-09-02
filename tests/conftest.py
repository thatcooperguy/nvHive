"""Shared fixtures."""

from __future__ import annotations

import asyncio

import pytest

import nvh.storage.repository as repo


@pytest.fixture()
async def db(tmp_path):
    """A fresh SQLite database bound to the repository module for one test."""
    repo._engine = None
    repo._session_factory = None
    await repo.init_db(db_path=tmp_path / "test.db")
    yield
    await repo.close_db()


@pytest.fixture()
def sync_db(tmp_path):
    """``db`` for synchronous (TestClient-driven) tests.

    One private loop runs both init and dispose, so the engine is torn down
    properly instead of leaking its aiosqlite threads across the session.
    """
    repo._engine = None
    repo._session_factory = None
    loop = asyncio.new_event_loop()
    loop.run_until_complete(repo.init_db(db_path=tmp_path / "test.db"))
    yield
    try:
        loop.run_until_complete(repo.close_db())
    finally:
        loop.close()
