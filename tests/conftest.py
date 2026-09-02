"""Shared fixtures."""

from __future__ import annotations

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
