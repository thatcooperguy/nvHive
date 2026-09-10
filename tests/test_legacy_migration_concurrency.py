"""Real filesystem/process regressions for first-run import publication races."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from nvh.integrations.workspace import migrate_legacy as ml
from nvh.integrations.workspace import storage


@pytest.fixture()
def homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".hive").mkdir(parents=True)
    layout = storage.storage_layout(tmp_path / "layout")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv(ml.LEGACY_MIGRATION_ENV, "1")
    return SimpleNamespace(home=home, layout=layout, root=tmp_path)


def seed(homes, kind):
    directory = kind in {"directory", "empty-directory"}
    source = homes.home / ".hive" / ("plugins" if directory else "user.json")
    destination = homes.layout.plugins_dir if directory else homes.layout.config_dir / "user.json"
    if directory:
        source.mkdir()
        (source / "legacy.py").write_bytes(b"legacy tree")
    else:
        source.write_bytes(b"legacy profile")
    return source, destination


@pytest.mark.parametrize("kind", ["file", "directory", "empty-directory"])
def test_competing_destination_at_publication_is_never_replaced(homes, monkeypatch, kind):
    source, destination = seed(homes, kind)
    publish = ml._publish_no_replace
    observed = []

    def competing_creator(staged, final):
        if final == destination:
            # The full payload is staged, but the public destination is absent.
            assert not os.path.lexists(final)
            assert staged.is_dir() if kind != "file" else staged.read_bytes() == b"legacy profile"
            if kind == "file":
                final.write_bytes(b"competing owner's bytes")
            else:
                final.mkdir()
                if kind == "directory":
                    (final / "owner.txt").write_bytes(b"competing owner's bytes")
            observed.append(final.stat().st_ino)
        return publish(staged, final)  # Real platform no-replace syscall.

    monkeypatch.setattr(ml, "_publish_no_replace", competing_creator)
    report = ml.migrate_legacy_homes(homes.layout)
    assert len(observed) == 1 and report.failed
    assert destination.stat().st_ino == observed[0]
    if kind == "file":
        assert destination.read_bytes() == b"competing owner's bytes"
        assert source.read_bytes() == b"legacy profile"
    else:
        assert not (destination / "legacy.py").exists()
        assert (source / "legacy.py").read_bytes() == b"legacy tree"
        if kind == "directory":
            assert (destination / "owner.txt").read_bytes() == b"competing owner's bytes"
        else:
            assert list(destination.iterdir()) == []
    assert not Path(report.marker).exists()
    assert not list(destination.parent.glob(".nvh-legacy-copy-*"))
    # Retry recognizes the winner and deliberately skips; it does not overwrite.
    monkeypatch.setattr(ml, "_publish_no_replace", publish)
    retry = ml.migrate_legacy_homes(homes.layout)
    assert not retry.failed and retry.skipped and Path(retry.marker).exists()


def test_partial_staging_failure_preserves_other_staging_and_retries(homes, monkeypatch):
    source, destination = seed(homes, "file")
    destination.parent.mkdir(parents=True)
    unrelated = destination.parent / ".nvh-legacy-copy-unrelated"
    unrelated.mkdir()
    (unrelated / "owner.txt").write_bytes(b"not our staging")
    copy = ml.shutil.copy2

    def interrupted(src, dst, *args, **kwargs):
        if Path(src) == source:
            assert Path(dst) != destination and not destination.exists()
            Path(dst).write_bytes(b"unfinished")
            raise OSError("simulated interrupted private copy")
        return copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(ml.shutil, "copy2", interrupted)
    report = ml.migrate_legacy_homes(homes.layout)
    assert report.failed and not destination.exists() and not Path(report.marker).exists()
    assert list(destination.parent.glob(".nvh-legacy-copy-*")) == [unrelated]
    assert (unrelated / "owner.txt").read_bytes() == b"not our staging"
    monkeypatch.setattr(ml.shutil, "copy2", copy)
    retry = ml.migrate_legacy_homes(homes.layout)
    assert not retry.failed and destination.read_bytes() == b"legacy profile"


def test_unavailable_publication_is_retryable_without_public_partial(homes, monkeypatch):
    _, destination = seed(homes, "directory")
    publish = ml._publish_no_replace

    def unavailable(*args):
        raise OSError("filesystem does not support exclusive rename")

    monkeypatch.setattr(ml, "_publish_no_replace", unavailable)
    report = ml.migrate_legacy_homes(homes.layout)
    assert report.failed and not destination.exists() and not Path(report.marker).exists()
    assert not list(destination.parent.glob(".nvh-legacy-copy-*"))
    monkeypatch.setattr(ml, "_publish_no_replace", publish)
    assert not ml.migrate_legacy_homes(homes.layout).failed
    assert (destination / "legacy.py").read_bytes() == b"legacy tree"


def test_missing_native_symbol_never_falls_back_to_replacing_rename(homes, monkeypatch):
    source = homes.root / "staged"
    source.mkdir()
    destination = homes.root / "competing-empty-directory"
    destination.mkdir()
    before = destination.stat().st_ino

    def forbidden(*args):
        pytest.fail("ordinary rename may replace the competing empty directory")

    # Exercise the actual dispatcher with an absent libc symbol even on Windows;
    # native Linux/macOS runners separately exercise their real supported syscalls.
    with monkeypatch.context() as context:
        context.setattr(ml.os, "name", "posix")
        context.setattr(ml.sys, "platform", "linux")
        context.setattr(ml.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace())
        context.setattr(ml.os, "rename", forbidden)
        with pytest.raises(OSError, match="no-replace migration is unavailable"):
            ml._publish_no_replace(source, destination)
    assert source.is_dir() and destination.stat().st_ino == before


def make_symlink(source, destination):
    try:
        destination.symlink_to(source, target_is_directory=source.is_dir())
    except OSError as exc:
        pytest.skip(f"This test user/filesystem cannot create symlinks: {exc}")


def test_existing_dangling_destination_symlink_is_preserved(homes):
    _, destination = seed(homes, "file")
    destination.parent.mkdir(parents=True)
    missing = homes.root / "missing-owner-target"
    make_symlink(missing, destination)
    report = ml.migrate_legacy_homes(homes.layout)
    assert not report.failed and report.skipped
    assert destination.is_symlink() and not missing.exists()
    assert os.readlink(destination) == str(missing)


def test_relative_source_link_keeps_its_referent_after_staged_move(homes):
    actual = homes.home / "authored.json"
    actual.write_bytes(b"authored source")
    source = homes.home / ".hive" / "user.json"
    make_symlink(Path("../authored.json"), source)
    report = ml.migrate_legacy_homes(homes.layout)
    destination = homes.layout.config_dir / "user.json"
    assert not report.failed and destination.is_symlink()
    assert destination.resolve() == actual.resolve()
    assert destination.read_bytes() == b"authored source"


@pytest.mark.parametrize(
    "name,current,legacy",
    [
        (".env", b"CURRENT_KEY=keep\n", b"LEGACY_KEY=add\n"),
        ("config.yaml", b"defaults:\n  timeout: 77\n", b"providers:\n  local:\n    model: test\n"),
    ],
)
def test_linked_config_merge_backup_preserves_pre_merge_content(homes, name, current, legacy):
    original = homes.root / "actual-config"
    original.write_bytes(current)
    destination = homes.layout.config_dir / name
    destination.parent.mkdir(parents=True)
    make_symlink(original, destination)
    (homes.home / ".hive" / name).write_bytes(legacy)
    report = ml.migrate_legacy_homes(homes.layout)
    backup = destination.with_name(destination.name + ml.MERGE_BACKUP_SUFFIX)
    assert not report.failed and report.merged
    assert destination.is_symlink() and destination.resolve() == original.resolve()
    assert original.read_bytes() != current
    assert backup.is_file() and not backup.is_symlink()
    assert backup.read_bytes() == current
    # Later edits cannot rewrite the old-content snapshot through a shared link.
    original.write_bytes(b"later independent edit")
    assert backup.read_bytes() == current


def test_competing_merge_backup_is_preserved_while_missing_keys_merge(homes, monkeypatch):
    destination = homes.layout.config_dir / ".env"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"CURRENT_KEY=keep\n")
    (homes.home / ".hive" / ".env").write_bytes(b"LEGACY_KEY=add\n")
    backup = destination.with_name(destination.name + ml.MERGE_BACKUP_SUFFIX)
    publish = ml._publish_no_replace
    raced = []

    def competing_backup(staged, final):
        if final == backup:
            assert staged.read_bytes() == b"CURRENT_KEY=keep\n"
            final.write_bytes(b"competing backup owner")
            raced.append(True)
        return publish(staged, final)

    monkeypatch.setattr(ml, "_publish_no_replace", competing_backup)
    report = ml.migrate_legacy_homes(homes.layout)
    assert raced == [True] and not report.failed and report.merged
    assert backup.read_bytes() == b"competing backup owner"
    text = destination.read_text()
    assert "CURRENT_KEY=keep" in text and "LEGACY_KEY=add" in text
    assert not list(destination.parent.glob(".nvh-legacy-copy-*"))


# Children load only the actual two stdlib storage modules. No nvh package
# startup, SDK/provider initialization, user profile discovery, or service calls.
_WORKER = r'''
import importlib.util, json, os, sys, time, types
from pathlib import Path
root, storage_path, migration_path, action = map(str, sys.argv[1:])
root = Path(root)
for name in ("nvh", "nvh.integrations", "nvh.integrations.workspace"):
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
storage = load("nvh.integrations.workspace.storage", storage_path)
ml = load("nvh.integrations.workspace.migrate_legacy", migration_path)
Path.home = classmethod(lambda cls: root / "home")
os.environ[ml.LEGACY_MIGRATION_ENV] = "1"
layout = storage.storage_layout(root / "layout")
def wait_for_release():
    (root / "ready").write_text("ready")
    deadline = time.monotonic() + 15
    while not (root / "release").exists():
        if time.monotonic() > deadline:
            raise TimeoutError("parent did not release isolated worker")
        time.sleep(.02)
if action == "hold":
    with ml._migration_lock(layout):
        wait_for_release()
elif action == "migrate":
    original = ml._copy
    def paused_copy(target, report=None):
        wait_for_release()
        return original(target, report)
    ml._copy = paused_copy
    report = ml.migrate_legacy_homes(layout)
    (root / "worker-report.json").write_text(json.dumps(report.as_dict()))
elif action == "partial-copy":
    def interrupted_copy(source, destination, *args, **kwargs):
        Path(destination).write_bytes(b"incomplete private data")
        (root / "staging-path").write_text(str(destination))
        wait_for_release()
        raise RuntimeError("test should abruptly terminate this worker")
    ml.shutil.copy2 = interrupted_copy
    ml.migrate_legacy_homes(layout)
else:
    raise ValueError(action)
'''


def start_worker(homes, action):
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", _WORKER, str(homes.root),
         str(Path(storage.__file__).resolve()), str(Path(ml.__file__).resolve()), action],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    deadline = monotonic() + 10
    while not (homes.root / "ready").exists():
        if process.poll() is not None or monotonic() > deadline:
            if process.poll() is None:
                process.kill()
            _, errors = process.communicate(timeout=5)
            pytest.fail(f"private worker failed to acquire lock: {errors.decode(errors='replace')}")
        sleep(.02)
    return process


def stop_worker(process):
    if process.poll() is None:
        process.kill()  # Only this test's Popen object; no process-name lookup.
    process.communicate(timeout=5)


def test_interprocess_contention_is_bounded_and_process_crash_releases_lock(homes, monkeypatch):
    _, destination = seed(homes, "file")
    worker = start_worker(homes, "hold")
    monkeypatch.setattr(ml, "_MIGRATION_LOCK_SECONDS", .15)
    try:
        started = monotonic()
        with pytest.raises(OSError, match="acquire legacy migration lock"):
            ml.migrate_legacy_homes(homes.layout)
        assert .10 <= monotonic() - started < 3
        assert not destination.exists()
        assert not (homes.layout.state_dir / ml.MARKER_NAME).exists()
        assert not (homes.layout.state_dir / "nvhive.db").exists()
    finally:
        stop_worker(worker)  # Abrupt exit: no Python finally/rollback executes.
    retry = ml.migrate_legacy_homes(homes.layout)
    assert not retry.failed and destination.read_bytes() == b"legacy profile"
    assert Path(retry.marker).exists()


def test_second_migration_rechecks_marker_after_first_process_finishes(homes):
    _, destination = seed(homes, "file")
    worker = start_worker(homes, "migrate")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(ml.migrate_legacy_homes, homes.layout)
            try:
                sleep(.15)
                assert not future.done() and not destination.exists()
            finally:
                (homes.root / "release").write_text("release")
            stdout, stderr = worker.communicate(timeout=10)
            assert worker.returncode == 0, (stdout, stderr)
            report = future.result(timeout=5)
        first = json.loads((homes.root / "worker-report.json").read_text())
        assert first["moved"] and not first["failed"]
        assert report.already_done and report.ran_at == first["ran_at"]
        assert report.moved == first["moved"] and destination.read_bytes() == b"legacy profile"
    finally:
        stop_worker(worker)


def test_crash_during_copy_leaves_no_public_partial_and_retry_ignores_stale_staging(homes):
    _, destination = seed(homes, "file")
    worker = start_worker(homes, "partial-copy")
    try:
        staging = Path((homes.root / "staging-path").read_text())
        staging.resolve().relative_to(homes.root.resolve())
        assert staging.read_bytes() == b"incomplete private data"
        assert not destination.exists()
    finally:
        stop_worker(worker)
    assert not (homes.layout.state_dir / ml.MARKER_NAME).exists()
    retry = ml.migrate_legacy_homes(homes.layout)
    assert not retry.failed and destination.read_bytes() == b"legacy profile"
    assert Path(retry.marker).exists()
    # No opportunistic cleanup of old directories by name: they may be owned
    # by another process. This test's whole private home is removed by pytest.
    assert staging.read_bytes() == b"incomplete private data"


async def test_repository_does_not_create_empty_database_on_migration_lock_timeout(
    homes, monkeypatch,
):
    from nvh.storage import repository as repository

    seed(homes, "file")
    monkeypatch.setattr(
        repository, "_default_db_path", lambda: homes.layout.state_dir / "nvhive.db",
    )
    monkeypatch.setattr(ml, "storage_layout", lambda: homes.layout)
    monkeypatch.setattr(ml, "_MIGRATION_LOCK_SECONDS", .15)
    await repository.close_db()
    worker = start_worker(homes, "hold")
    try:
        with pytest.raises(RuntimeError, match="retry before creating a new database"):
            await repository.init_db()
        assert not (homes.layout.state_dir / "nvhive.db").exists()
    finally:
        stop_worker(worker)
        await repository.close_db()
