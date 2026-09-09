"""StorageLayout as the single root (0.44) and the one-shot pre-0.44 home import.

``storage_layout()`` grows ``plugins_dir`` and exports ``NVH_CONFIG`` as the
config override (``HIVE_CONFIG_HOME`` stays a legacy alias);
``nvh/integrations/workspace/migrate_legacy.py`` copies ``~/.hive``,
``~/.council`` and ``~/nvh`` into the layout exactly once and never writes
back. Everything here runs against a fake OS home (``Path.home`` patched) and a
throwaway ``NVH_HOME``; the reset hook for the settings cache is
``nvh.config.settings.reset_default_paths()``.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import nvh.config.settings as settings
from nvh.integrations.workspace import migrate_legacy as ml
from nvh.integrations.workspace.storage import ensure_storage, storage_layout

_ENV_KNOBS = (
    "NVHIVE_HOME", "NVH_CONFIG", "HIVE_CONFIG_HOME", "NVH_STATE", "NVH_PLUGINS", "NVH_OUTPUTS",
    "NVH_SUPPORT", "HIVE_DATA_DIR",
)


@pytest.fixture()
def homes(tmp_path: Path, monkeypatch) -> SimpleNamespace:
    os_home = tmp_path / "home"
    os_home.mkdir()
    nvh_home = tmp_path / "nvh"
    monkeypatch.setattr(Path, "home", lambda: os_home)
    for var in _ENV_KNOBS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NVH_HOME", str(nvh_home))
    # tests/conftest.py turns the migration off for every suite; this one tests it.
    monkeypatch.setenv(ml.LEGACY_MIGRATION_ENV, "1")
    # cwd under the *fake* home: the project-config walk stops at Path.home(),
    # and on Windows tmp_path itself sits under the real one.
    (os_home / "work").mkdir()
    monkeypatch.chdir(os_home / "work")
    settings.reset_default_paths()
    yield SimpleNamespace(os_home=os_home, nvh_home=nvh_home, layout=storage_layout())
    settings.reset_default_paths()


def _seed_legacy(os_home: Path) -> dict[str, bytes]:
    """A full pre-0.44 spread; returns ``{relative path: bytes}`` for every file."""
    hive = os_home / ".hive"
    files: dict[str, bytes] = {
        ".hive/config.yaml": b"defaults:\n  timeout: 77\n",
        ".hive/.env": b"LEGACY_KEY=from_legacy\n",
        ".hive/user.json": b'{"email": "a@b.c"}',
        ".hive/global_context.md": b"Global rules.",
        ".hive/last_query.json": b'{"task": "x"}',
        ".hive/schedules.json": json.dumps([{
            "id": "t1", "prompt": "p", "interval_seconds": 60, "advisor": "", "mode": "ask",
            "last_run": "", "next_run": 0.0, "enabled": True, "created_at": "",
        }]).encode(),
        ".hive/benchmark_results.md": b"# bench",
        ".hive/workflows/review.yaml": b"name: review\nsteps: []\n",
        ".hive/plugins/hello.py": b"def register(reg):\n    pass\n",
        ".hive/memory/memories.json": b"[]",
        ".hive/knowledge/documents.json": b"[]",
        ".hive/knowledge/chunks/a_0000.json": b"{}",
        "nvh/nvidia-bug-report.log.gz": b"gz",
    }
    for rel, data in files.items():
        path = os_home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    council = os_home / ".council"
    council.mkdir()
    conn = sqlite3.connect(council / "council.db")
    try:
        conn.execute("CREATE TABLE legacy_marker (x INTEGER)")
        conn.execute("INSERT INTO legacy_marker VALUES (1)")
        conn.commit()
    finally:
        conn.close()  # closed for real, so nothing checkpoints the file behind the test's back
    # Stand-ins for the -wal / -shm sidecars a live WAL-mode database leaves behind.
    (council / "council.db-wal").write_bytes(b"wal-sidecar")
    (council / "council.db-shm").write_bytes(b"shm-sidecar")
    for name in ("council.db", "council.db-wal", "council.db-shm"):
        files[f".council/{name}"] = (council / name).read_bytes()
    assert hive.is_dir()
    return files


def _tree(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*")) if p.is_file()
    }


# ---------------------------------------------------------------------------
# StorageLayout
# ---------------------------------------------------------------------------


def test_layout_has_plugins_dir_and_config_env_names(homes) -> None:
    layout = homes.layout
    assert layout.plugins_dir == homes.nvh_home / "plugins"
    env = layout.env()
    assert env["NVH_CONFIG"] == env["HIVE_CONFIG_HOME"] == str(layout.config_dir)
    assert env["NVH_PLUGINS"] == str(layout.plugins_dir)
    status = ensure_storage(min_free_gb=0, activate=False)
    assert status.layout.plugins_dir.is_dir()
    assert "NVH_CONFIG" in (homes.nvh_home / "nvh-env.sh").read_text(encoding="utf-8")


def test_config_override_prefers_nvh_config_over_the_legacy_alias(homes, monkeypatch) -> None:
    monkeypatch.setenv("HIVE_CONFIG_HOME", str(homes.nvh_home.parent / "legacy-cfg"))
    assert storage_layout().config_dir == (homes.nvh_home.parent / "legacy-cfg").resolve()
    monkeypatch.setenv("NVH_CONFIG", str(homes.nvh_home.parent / "cfg"))
    assert storage_layout().config_dir == (homes.nvh_home.parent / "cfg").resolve()
    monkeypatch.setenv("NVH_PLUGINS", str(homes.nvh_home.parent / "plug"))
    assert storage_layout().plugins_dir == (homes.nvh_home.parent / "plug").resolve()
    # An explicit home argument ignores the component overrides, as before.
    explicit = storage_layout(homes.nvh_home.parent / "other")
    assert explicit.config_dir == (homes.nvh_home.parent / "other" / "config").resolve()
    assert explicit.plugins_dir == (homes.nvh_home.parent / "other" / "plugins").resolve()


# ---------------------------------------------------------------------------
# migrate_legacy_homes
# ---------------------------------------------------------------------------


def test_migration_copies_everything_once_and_never_writes_back(homes) -> None:
    seeded = _seed_legacy(homes.os_home)
    before = _tree(homes.os_home)

    report = ml.migrate_legacy_homes()

    layout = homes.layout
    expected = {
        layout.config_dir / "config.yaml": seeded[".hive/config.yaml"],
        layout.config_dir / ".env": seeded[".hive/.env"],
        layout.config_dir / "user.json": seeded[".hive/user.json"],
        layout.config_dir / "global_context.md": seeded[".hive/global_context.md"],
        layout.config_dir / "workflows" / "review.yaml": seeded[".hive/workflows/review.yaml"],
        layout.plugins_dir / "hello.py": seeded[".hive/plugins/hello.py"],
        layout.state_dir / "schedules.json": seeded[".hive/schedules.json"],
        layout.state_dir / "last_query.json": seeded[".hive/last_query.json"],
        layout.state_dir / "memory" / "memories.json": seeded[".hive/memory/memories.json"],
        layout.home / "knowledge" / "documents.json": seeded[".hive/knowledge/documents.json"],
        layout.home / "knowledge" / "chunks" / "a_0000.json": seeded[".hive/knowledge/chunks/a_0000.json"],
        layout.outputs_dir / "benchmark_results.md": seeded[".hive/benchmark_results.md"],
        layout.support_dir / "nvidia-bug-report.log.gz": seeded["nvh/nvidia-bug-report.log.gz"],
    }
    for path, data in expected.items():
        assert path.read_bytes() == data, path
    # The database travels with its -wal / -shm sidecars (renamed to the new stem).
    db_copy = layout.state_dir / "nvhive.db"
    assert db_copy.read_bytes() == seeded[".council/council.db"]
    for suffix in ("-wal", "-shm"):
        sidecar = db_copy.with_name(db_copy.name + suffix)
        assert sidecar.read_bytes() == seeded[f".council/council.db{suffix}"]
        sidecar.unlink()  # stand-in bytes, not a real WAL: drop them before opening the copy
    conn = sqlite3.connect(db_copy)
    try:
        assert conn.execute("SELECT x FROM legacy_marker").fetchone() == (1,)
    finally:
        conn.close()

    assert report.already_done is False and report.ran_at
    assert set(report.legacy_roots) == {"hive", "council", "install"}
    assert {entry["label"] for entry in report.moved} == {t.label for t in ml.legacy_targets(layout)}
    assert report.skipped == []
    marker = json.loads((layout.state_dir / ml.MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["moved"] == report.moved

    # Read-only: the legacy tree is byte-for-byte and mtime-for-mtime unchanged.
    assert _tree(homes.os_home) == before

    # One-shot: a later edit of a legacy file never propagates.
    (homes.os_home / ".hive" / "config.yaml").write_bytes(b"defaults:\n  timeout: 1\n")
    again = ml.migrate_legacy_homes()
    assert again.already_done is True and again.moved == report.moved
    assert (layout.config_dir / "config.yaml").read_bytes() == seeded[".hive/config.yaml"]


def test_migration_never_overwrites_existing_layout_files(homes) -> None:
    _seed_legacy(homes.os_home)
    mine = homes.layout.config_dir / "config.yaml"
    mine.parent.mkdir(parents=True)
    mine.write_text("defaults:\n  timeout: 5\n")
    plugins = homes.layout.plugins_dir
    plugins.mkdir(parents=True)
    (plugins / "mine.py").write_text("def register(reg):\n    pass\n")

    report = ml.migrate_legacy_homes()

    # The layout copy wins every conflict (``defaults`` is in both) and a
    # directory the user already has is left alone entirely.
    assert mine.read_text() == "defaults:\n  timeout: 5\n"
    assert sorted(p.name for p in plugins.iterdir()) == ["mine.py"]
    skipped = {entry["label"]: entry["reason"] for entry in report.skipped}
    assert skipped == {"user config": "destination exists (nothing to merge)", "plugins": "destination exists"}
    assert report.merged == [] and report.failed == []


def test_migration_merges_the_keys_and_stanzas_the_layout_copies_lack(homes) -> None:
    """0.43 kept ``~/.hive/.env`` + ``~/.hive/config.yaml`` (``nvh setup``) and the
    layout's copies (the web wizard) live at once and read both; a key or a
    provider stanza that only the legacy file has must survive the upgrade."""
    _seed_legacy(homes.os_home)
    hive = homes.os_home / ".hive"
    (hive / ".env").write_text("GROQ_API_KEY=from_cli_setup\nOPENAI_API_KEY=legacy_openai\n# a comment\n")
    (hive / "config.yaml").write_text(
        "defaults:\n  timeout: 77\nadvisors:\n  groq:\n    enabled: true\n  openai:\n    enabled: false\nbudget:\n  daily_limit: 5\n"
    )
    cfg = homes.layout.config_dir
    cfg.mkdir(parents=True)
    (cfg / ".env").write_text("OPENAI_API_KEY=from_wizard\n")
    (cfg / "config.yaml").write_text("version: '1'\ndefaults:\n  timeout: 5\nproviders:\n  openai:\n    enabled: true\n")

    report = ml.migrate_legacy_homes()

    env_text = (cfg / ".env").read_text()
    assert env_text.startswith("OPENAI_API_KEY=from_wizard\n")
    assert "GROQ_API_KEY=from_cli_setup" in env_text and "legacy_openai" not in env_text
    data = yaml.safe_load((cfg / "config.yaml").read_text())
    assert data["version"] == "1" and data["defaults"] == {"timeout": 5}
    # The legacy ``advisors:`` section fills the layout's ``providers:`` — one section to the loader.
    assert data["providers"] == {"openai": {"enabled": True}, "groq": {"enabled": True}}
    assert "advisors" not in data and data["budget"] == {"daily_limit": 5}
    merged = {entry["label"]: entry["added"] for entry in report.merged}
    assert merged == {"API keys (.env)": ["GROQ_API_KEY"], "user config": ["advisors.groq", "budget"]}
    # The pre-merge layout copies are kept beside the merged files.
    assert (cfg / (".env" + ml.MERGE_BACKUP_SUFFIX)).read_text() == "OPENAI_API_KEY=from_wizard\n"
    assert yaml.safe_load((cfg / ("config.yaml" + ml.MERGE_BACKUP_SUFFIX)).read_text())["providers"] == {"openai": {"enabled": True}}
    assert settings.load_config().providers["groq"].enabled is True
    marker = json.loads((homes.layout.state_dir / ml.MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["merged"] == report.merged
    # Idempotent: a second forced run finds nothing more to merge.
    again = ml.migrate_legacy_homes(force=True)
    assert again.merged == [] and (cfg / ".env").read_text() == env_text


def test_migration_copies_symlinks_as_links(homes) -> None:
    """An edit-in-place ``~/.hive/plugins/dev.py -> ~/src/dev.py`` stays a link, and a
    ``~/.hive/knowledge -> archive`` link is re-created, never the archive duplicated."""
    import shutil

    _seed_legacy(homes.os_home)
    hive = homes.os_home / ".hive"
    real = homes.os_home / "src" / "dev_tool.py"
    real.parent.mkdir()
    real.write_text("def register(reg):\n    pass\n")
    archive = homes.os_home / "archive"
    archive.mkdir()
    (archive / "documents.json").write_bytes(b"[]")
    shutil.rmtree(hive / "knowledge")
    try:
        (hive / "plugins" / "dev_tool.py").symlink_to(real)
        (hive / "knowledge").symlink_to(archive, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available to this user")

    report = ml.migrate_legacy_homes()
    assert report.failed == []

    copied = homes.layout.plugins_dir / "dev_tool.py"
    assert copied.is_symlink() and copied.resolve() == real.resolve()
    real.write_text("def register(reg):\n    reg.touched = True\n")
    assert copied.read_text() == real.read_text()  # still edit-in-place
    knowledge = homes.layout.home / "knowledge"
    assert knowledge.is_symlink() and knowledge.resolve() == archive.resolve()
    assert (knowledge / "documents.json").read_bytes() == b"[]"


def test_failed_copy_is_reported_and_retried_because_no_marker_is_written(homes, monkeypatch) -> None:
    import shutil

    _seed_legacy(homes.os_home)
    real_copytree = shutil.copytree

    def boom(src, dst, *args, **kwargs):  # copytree recurses into itself positionally
        if Path(src).name == "plugins":
            Path(dst).mkdir(parents=True)
            (Path(dst) / "half.py").write_text("x")  # a partial copy
            raise OSError("disk full")
        return real_copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(shutil, "copytree", boom)
    first = ml.migrate_legacy_homes()
    assert [entry["label"] for entry in first.failed] == ["plugins"]
    assert "copy failed" in first.failed[0]["reason"]
    assert not (homes.layout.state_dir / ml.MARKER_NAME).exists()
    assert not homes.layout.plugins_dir.exists()  # the partial copy is cleaned up
    assert (homes.layout.config_dir / "config.yaml").is_file()  # everything else still copied

    monkeypatch.setattr(shutil, "copytree", real_copytree)
    second = ml.migrate_legacy_homes()
    assert second.already_done is False and second.failed == []
    assert [entry["label"] for entry in second.moved] == ["plugins"]
    assert (homes.layout.plugins_dir / "hello.py").is_file()
    assert (homes.layout.state_dir / ml.MARKER_NAME).is_file()
    assert ml.migrate_legacy_homes().already_done is True


def test_kill_switch_makes_the_migration_a_no_op(homes, monkeypatch) -> None:
    _seed_legacy(homes.os_home)
    monkeypatch.setenv(ml.LEGACY_MIGRATION_ENV, "0")
    assert ml.migration_enabled() is False
    report = ml.migrate_legacy_homes()
    assert report.disabled is True and report.moved == [] and report.ran_at is None
    assert not homes.nvh_home.exists()
    for value in ("1", "", "yes"):
        assert ml.migration_enabled({ml.LEGACY_MIGRATION_ENV: value}) is True
    for value in ("0", "false", "OFF", "no"):
        assert ml.migration_enabled({ml.LEGACY_MIGRATION_ENV: value}) is False


def test_migration_skips_stores_a_one_shot_importer_already_consumed(homes) -> None:
    _seed_legacy(homes.os_home)
    layout = homes.layout
    (layout.state_dir).mkdir(parents=True)
    (layout.state_dir / "legacy-memories-imported.json").write_text("{}")
    (layout.home / "rag").mkdir(parents=True)
    (layout.home / "rag" / "legacy-import.json").write_text("{}")

    report = ml.migrate_legacy_homes()

    skipped = {entry["label"] for entry in report.skipped}
    assert skipped == {"REPL memories", "knowledge store"}
    assert not (layout.state_dir / "memory").exists()
    assert not (layout.home / "knowledge").exists()


def test_migration_with_no_legacy_roots_writes_nothing(homes) -> None:
    report = ml.migrate_legacy_homes()
    assert report.moved == [] and report.skipped == [] and report.ran_at is None
    assert report.legacy_roots == {}
    assert not homes.nvh_home.exists()


def test_migration_warns_about_the_retired_data_dir_knob(homes, monkeypatch) -> None:
    assert ml.legacy_env_warnings({}) == []
    monkeypatch.setenv("HIVE_DATA_DIR", str(homes.nvh_home.parent / "data"))
    report = ml.migrate_legacy_homes()
    assert len(report.warnings) == 1
    assert "HIVE_DATA_DIR" in report.warnings[0] and "NVH_STATE" in report.warnings[0]


def test_force_reruns_over_an_existing_marker(homes) -> None:
    _seed_legacy(homes.os_home)
    first = ml.migrate_legacy_homes()
    (homes.layout.config_dir / "user.json").unlink()
    again = ml.migrate_legacy_homes(force=True)
    assert again.already_done is False
    assert {e["label"] for e in again.moved} == {"user profile"}
    # The two merge targets re-compare (identical copies: nothing to merge); the rest are skipped outright.
    assert all(e["reason"].startswith("destination exists") for e in again.skipped)
    assert len(again.skipped) == len(first.moved) - 1 and again.merged == []


# ---------------------------------------------------------------------------
# The consumers read the migrated copies from the layout, never from $HOME
# ---------------------------------------------------------------------------


def test_every_consumer_reads_from_the_layout_after_migration(homes) -> None:
    _seed_legacy(homes.os_home)
    ml.migrate_legacy_homes()
    layout = homes.layout

    assert settings.DEFAULT_CONFIG_PATH == layout.config_dir / "config.yaml"
    assert settings.load_config().defaults.timeout == 77

    from nvh.core.scheduler import Scheduler, default_schedule_file

    assert default_schedule_file() == layout.state_dir / "schedules.json"
    assert [t.id for t in Scheduler()._tasks] == ["t1"]

    from nvh.core.workflows import default_workflow_dirs, discover_workflows

    assert default_workflow_dirs()[0] == layout.config_dir / "workflows"
    assert discover_workflows()["review"] == layout.config_dir / "workflows" / "review.yaml"

    from nvh.core.context_files import find_context_files, global_context_file

    assert global_context_file() == layout.config_dir / "global_context.md"
    found = find_context_files(project_dir=homes.nvh_home.parent / "proj", home_dir=homes.os_home)
    assert [f.content for f in found if f.source == "global"] == ["Global rules."]

    from nvh.cli.repl import legacy_memory_file
    from nvh.integrations.rag.legacy import legacy_knowledge_dir
    from nvh.storage.repository import _default_db_path

    assert legacy_memory_file() == layout.state_dir / "memory" / "memories.json"
    assert legacy_memory_file().is_file()
    assert legacy_knowledge_dir() == layout.home / "knowledge"
    assert (legacy_knowledge_dir() / "documents.json").is_file()
    assert _default_db_path() == layout.state_dir / "nvhive.db"
    assert _default_db_path().is_file()


def test_load_config_migrates_when_only_the_legacy_config_exists(homes) -> None:
    """The first ``load_config()`` after an upgrade sees the ~/.hive config once."""
    (homes.os_home / ".hive").mkdir()
    (homes.os_home / ".hive" / "config.yaml").write_text("defaults:\n  timeout: 66\n")

    assert settings.load_config().defaults.timeout == 66
    assert (homes.layout.config_dir / "config.yaml").is_file()
    assert (homes.layout.state_dir / ml.MARKER_NAME).is_file()

    # Explicit paths never trigger it, and a present user config never re-reads the legacy one.
    (homes.os_home / ".hive" / "config.yaml").write_text("defaults:\n  timeout: 1\n")
    assert settings.load_config().defaults.timeout == 66


def test_get_config_dir_runs_the_migration_on_the_init_path(homes) -> None:
    _seed_legacy(homes.os_home)
    config_dir = settings.get_config_dir()
    assert config_dir == homes.layout.config_dir and config_dir.is_dir()
    assert (config_dir / ".env").read_text() == "LEGACY_KEY=from_legacy\n"


def test_first_run_touches_nothing_under_the_os_home(homes, monkeypatch) -> None:
    """Everything the normal startup path writes lands under NVH_HOME (invariant I8)."""
    import nvh.cli.setup as setup
    from nvh.core.context_files import find_context_files
    from nvh.core.scheduler import Scheduler
    from nvh.core.workflows import discover_workflows

    _seed_legacy(homes.os_home)
    before = _tree(homes.os_home)
    monkeypatch.setattr(setup, "DEFAULT_CONFIG_DIR", homes.layout.config_dir)

    assert setup._env_key_files() == [homes.layout.config_dir / ".env"]
    setup.load_env_keys(use_keyring=False)
    settings.load_config()
    settings.get_config_dir()
    Scheduler().add("ping", 60)
    discover_workflows()
    find_context_files(project_dir=homes.nvh_home.parent / "proj", home_dir=homes.os_home)

    assert _tree(homes.os_home) == before
    assert os.environ.get("LEGACY_KEY") == "from_legacy"
    monkeypatch.delenv("LEGACY_KEY", raising=False)
    written = {p.relative_to(homes.nvh_home).as_posix() for p in homes.nvh_home.rglob("*") if p.is_file()}
    assert {"config/config.yaml", "config/.env", "state/schedules.json", f"state/{ml.MARKER_NAME}"} <= written
