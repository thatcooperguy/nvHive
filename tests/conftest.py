"""Shared fixtures."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock

import pytest

# The API lifespan's platform warm-up runs the cloud-metadata curls, ``sudo -n``
# and ``hostname -f``. Every ``with TestClient(app)`` suite would otherwise pay
# for (and spawn) them on CI; a test that wants the warm-up sets the variable
# to "1" itself. setdefault so an operator's explicit choice still wins.
os.environ.setdefault("NVH_PLATFORM_WARMUP", "0")

import nvh.storage.repository as repo  # noqa: E402
from nvh.utils import platform_facts as _platform_facts  # noqa: E402

# What every test sees unless it clears the platform cache itself: a plain
# Linux x86_64 workstation with no privileges and no cloud. Fixed values (not
# the real OS) so the suites behave identically on the dev box and on CI.
NEUTRAL_PLATFORM_FACTS = _platform_facts.PlatformFacts(
    os="linux",
    arch="x86_64",
    machine="x86_64",
    device_class="workstation",
    device_label="Workstation (linux/x86_64; no NVIDIA GPU)",
    has_root=False,
    can_sudo=False,
    in_sudo_group=False,
    is_cloud=False,
)


@pytest.fixture(autouse=True)
def _neutral_platform_facts():
    """Pre-fill the platform_facts process cache so no test spawns ``sudo -n``,
    ``whoami /groups``, ``nvidia-smi``-backed platform probes, ``hostname`` or
    the metadata curls by accident.

    Suites that exercise the real probes (tests/test_platform_facts.py) clear
    the cache in their own fixture, which runs after this one.
    """
    _platform_facts.seed_platform_facts(NEUTRAL_PLATFORM_FACTS)
    yield
    _platform_facts.clear_platform_facts_cache()


@pytest.fixture(autouse=True)
def _hermetic_local_probe(request, monkeypatch):
    """Nothing reaches the local daemon (127.0.0.1:11434) by accident.

    Every chat turn whose router picks ``ollama`` awaits
    ``chat._probe_local_provider`` — an async ``GET /api/tags`` — so any
    suite that builds a MagicMock engine (stream, cost ceiling, meter,
    iteration cap, context) used to issue one real request per process.
    This double answers "up", and the reachability cache is empty before
    and after every test.

    Composes with a suite's own probe fixture: module-level autouse
    fixtures set up *after* this one, so theirs is the double installed
    (and the first torn down); it finds the genuine coroutine on ``.real``
    of the double it replaces. When a double is already installed this
    fixture leaves it alone. A module that must run the unpatched probe on
    every test opts out with ``HERMETIC_LOCAL_PROBE = False`` at module
    level; the cache is still reset around each of its tests.
    """
    from nvh.integrations.wizard import chat as chat_mod

    chat_mod._reset_local_probe_cache()
    current = chat_mod._probe_local_provider
    hermetic = getattr(request.module, "HERMETIC_LOCAL_PROBE", True)
    if hermetic and not isinstance(current, AsyncMock):
        probe = AsyncMock(return_value=True)
        probe.real = current
        monkeypatch.setattr(chat_mod, "_probe_local_provider", probe)
    yield
    chat_mod._reset_local_probe_cache()


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
