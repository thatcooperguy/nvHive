"""Shared fixtures."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# The API lifespan's platform warm-up runs the cloud-metadata curls, ``sudo -n``
# and ``hostname -f``. Every ``with TestClient(app)`` suite would otherwise pay
# for (and spawn) them on CI; a test that wants the warm-up sets the variable
# to "1" itself. setdefault so an operator's explicit choice still wins.
os.environ.setdefault("NVH_PLATFORM_WARMUP", "0")
# The one-shot pre-0.44 home import (nvh/integrations/workspace/migrate_legacy.py)
# reads the developer's real ~/.hive / ~/.council whenever a suite sets NVH_HOME
# without patching Path.home — and exports the keys it finds. Off for every
# test; a test of the migration itself sets NVH_LEGACY_MIGRATION=1 (and a fake
# Path.home). setdefault so an operator's explicit choice still wins.
os.environ.setdefault("NVH_LEGACY_MIGRATION", "0")

# LiteLLM fetches its live model-price map from GitHub at import when the
# network is up; tests (and every nvh subprocess they spawn) use the bundled
# map instead so a run never depends on raw.githubusercontent.com.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

# The API lifespan starts the schedule runner (nvh/integrations/services/
# schedule_runner.run_loop) unless NVH_SCHEDULER=0. Off at import so the
# subprocesses tests spawn (tests/test_live_api.py's real uvicorn) inherit it
# too; the autouse ``_no_schedule_runner`` fixture below pins it per test.
os.environ.setdefault("NVH_SCHEDULER", "0")

# The RAG embedder pulls its Ollama model on a 404 unless NVH_RAG_AUTO_PULL=0
# (nvh/integrations/rag/embedder.py). Any route or tool that indexes a note
# for real — ``POST /v1/vault/memory`` since 0.46 — would otherwise be one
# unpatched test away from a model download on a box with Ollama up; tests
# that want the pull set the variable themselves. setdefault so an operator's
# explicit choice still wins.
os.environ.setdefault("NVH_RAG_AUTO_PULL", "0")

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
def _hermetic_home(request, tmp_path_factory):
    """Every test runs against a throwaway home, never the developer's.

    ``NVH_HOME`` points at a fresh, not-yet-created directory (a first run,
    which is what CI sees) and ``Path.home()`` — with ``HOME`` and
    ``USERPROFILE``, for ``expanduser`` and for the subprocesses a test
    spawns — at a fresh empty one. Both live under pytest's basetemp beside
    the test's own ``tmp_path``, never inside it. The per-directory knobs
    ``storage_layout()`` honours (``NVH_STATE``, ``NVH_CONFIG``,
    ``NVHIVE_HOME``, ``HIVE_CONFIG_HOME``, ...) are cleared so an operator's
    shell cannot point a test at a real directory, and
    ``nvh.config.settings.reset_default_paths()`` re-derives
    ``DEFAULT_CONFIG_DIR`` / ``DEFAULT_CONFIG_PATH`` on the way in and again
    on the way out.

    Without this the API's ``require_auth`` opened ``~/.nvh/state/nvhive.db``
    on every ``TestClient`` request, ``Engine()`` read ``~/.nvh/config``, and
    the platform detector read ``~/.cursor`` and friends, so a suite passed
    or failed with whatever happened to be installed on the box.

    A test that wants its own layout still sets ``NVH_HOME`` or patches
    ``Path.home`` itself: module and function fixtures run after this one,
    so theirs win and are undone first. A module that must see the real home
    on every test opts out with ``HERMETIC_HOME = False`` at module level
    and gets exactly the pre-fixture behaviour (nothing patched, nothing
    reset).
    """
    module = getattr(request.node, "module", None)
    if not getattr(module, "HERMETIC_HOME", True):
        yield
        return

    from nvh.config import settings as _settings
    from nvh.integrations.workspace.storage import storage_layout as _storage_layout

    base = tmp_path_factory.mktemp("hermetic-home")
    user_home = base / "home"
    user_home.mkdir()
    nvh_home = base / "nvh"

    mp = pytest.MonkeyPatch()
    # The nvHive knobs storage_layout() reads, taken from the layout's own
    # env() so the list cannot drift; the third-party cache knobs it also
    # exports (TMPDIR, HF_HOME, PIP_CACHE_DIR, ...) are not nvHive's to clear.
    # setenv-then-delenv rather than a bare delenv, so a variable the code
    # under test exports during the test is removed again at teardown.
    for name in _storage_layout(nvh_home).env():
        if name.startswith(("NVH_", "NVHIVE_", "HIVE_", "COMFYUI_")):
            mp.setenv(name, os.environ.get(name, ""))
            mp.delenv(name)
    mp.setenv("NVH_HOME", str(nvh_home))
    mp.setenv("HOME", str(user_home))
    mp.setenv("USERPROFILE", str(user_home))
    mp.setattr(Path, "home", classmethod(lambda cls: user_home))
    _settings.reset_default_paths()
    try:
        yield
    finally:
        mp.undo()
        _settings.reset_default_paths()


@pytest.fixture(autouse=True)
def _no_schedule_runner(monkeypatch):
    """No test starts the schedule runner by accident: ``NVH_SCHEDULER=0``.

    The API lifespan reads the variable and skips the ``run_loop`` task when
    it is ``0``, so every ``with TestClient(app)`` suite starts no poll loop
    and writes no ``state/scheduler.lock`` into the hermetic home (a loop
    left running would also keep the loop's thread alive across tests in the
    one-process CI run). The one lifespan test that exercises the runner sets
    ``NVH_SCHEDULER=1`` itself — a test's own ``monkeypatch.setenv`` runs
    after this fixture and wins — and patches ``run_loop``.
    """
    monkeypatch.setenv("NVH_SCHEDULER", "0")
    yield


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
