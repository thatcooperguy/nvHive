"""Tests for the workspace snapshot bundle (Tier 2, feature #6)."""

from __future__ import annotations

import io
import sqlite3
import tarfile
from contextlib import closing
from pathlib import Path

import pytest

from nvh.integrations.workspace.snapshot import (
    SNAPSHOT_EXCLUDES,
    SNAPSHOT_MANIFEST_NAME,
    SNAPSHOT_VERSION,
    export_snapshot,
    import_snapshot,
    list_snapshot,
)


def _sqlite_with_rows(path: Path, table: str, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, title TEXT)")
        conn.executemany(
            f"INSERT INTO {table} (title) VALUES (?)", [(f"row-{i}",) for i in range(rows)]
        )
        conn.commit()


def _count_rows(path: Path, table: str) -> int:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _seed_workspace(home: Path) -> None:
    """Create a representative NVH_HOME layout in a tmp dir."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "vault").mkdir()
    (home / "vault" / "notes.md").write_text("Migration plan: …")
    _sqlite_with_rows(home / "rag" / "index.sqlite", "chunks", 2)
    (home / "config").mkdir()
    (home / "config" / "preferences.yaml").write_text("theme: dark\n")
    _sqlite_with_rows(home / "state" / "nvhive.db", "conversations", 3)
    (home / "state" / "mcp-tools-cache.json").write_text("{}")  # regenerable, not bundled
    # Secrets that must NOT be bundled.
    (home / ".env").write_text("OPENAI_API_KEY=sk-real-secret")
    (home / "config" / "secrets.yaml").write_text("groq_key: gsk-x")


def _extract_member(tar_path: str, name: str, dest: Path) -> Path:
    with tarfile.open(tar_path, mode="r:gz") as tar:
        f = tar.extractfile(name)
        assert f is not None
        dest.write_bytes(f.read())
    return dest


def test_export_snapshot_returns_tarball_with_manifest(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_workspace(home)

    result = export_snapshot(home_dir=home)
    assert result["ok"] is True
    out = Path(result["path"])
    assert out.exists()
    assert out.parent == home / "snapshots"
    assert result["bytes"] > 0
    assert result["manifest"]["snapshot_version"] == SNAPSHOT_VERSION
    # vault + rag index + prefs + conversations DB all show up; secret paths do not.
    includes = set(result["manifest"]["includes"])
    assert "vault" in includes
    assert "rag/index.sqlite" in includes
    assert "config/preferences.yaml" in includes
    assert "state/nvhive.db" in includes
    assert "state/nvhive.db-wal" not in includes
    assert result["manifest"]["databases"]["state/nvhive.db"] == {
        "source": str(home / "state" / "nvhive.db"),
        "method": "sqlite-backup",
    }


def test_export_backs_up_live_wal_database(tmp_path: Path) -> None:
    """Rows committed to the WAL but not yet checkpointed must be in the bundle."""
    home = tmp_path / "home"
    _seed_workspace(home)
    db = home / "state" / "nvhive.db"

    live = sqlite3.connect(db)
    try:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("INSERT INTO conversations (title) VALUES ('still in the wal')")
        live.commit()
        assert db.with_name("nvhive.db-wal").stat().st_size > 0
        result = export_snapshot(home_dir=home)
    finally:
        live.close()

    assert result["ok"] is True
    with tarfile.open(result["path"], mode="r:gz") as tar:
        names = tar.getnames()
    assert "state/nvhive.db" in names
    assert not any(n.endswith(("-wal", "-shm")) for n in names)

    copy = _extract_member(result["path"], "state/nvhive.db", tmp_path / "copy.db")
    assert _count_rows(copy, "conversations") == 4


def test_export_finds_db_relocated_by_hive_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _seed_workspace(home)
    (home / "state" / "nvhive.db").unlink()
    data_dir = tmp_path / "persist"
    _sqlite_with_rows(data_dir / "state" / "nvhive.db", "conversations", 5)
    monkeypatch.setenv("NVH_HOME", str(home))
    monkeypatch.setenv("HIVE_DATA_DIR", str(data_dir))
    monkeypatch.delenv("NVH_STATE", raising=False)

    result = export_snapshot()

    assert result["ok"] is True
    assert result["manifest"]["databases"]["state/nvhive.db"]["source"] == str(
        data_dir / "state" / "nvhive.db"
    )
    copy = _extract_member(result["path"], "state/nvhive.db", tmp_path / "copy.db")
    assert _count_rows(copy, "conversations") == 5


def test_export_snapshot_honours_out_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_workspace(home)
    out = tmp_path / "backups" / "state.tar.gz"

    result = export_snapshot(home_dir=home, out_path=out)
    assert result["ok"] is True
    assert Path(result["path"]) == out
    assert out.exists()
    assert not (home / "snapshots").exists()


def test_export_snapshot_reports_unwritable_out_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_workspace(home)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")

    result = export_snapshot(home_dir=home, out_path=blocker / "state.tar.gz")
    assert result["ok"] is False
    assert "cannot write snapshot" in result["error"]


def test_list_snapshot_reports_manifest_and_members(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_workspace(home)
    exported = export_snapshot(home_dir=home)

    listing = list_snapshot(exported["path"])
    assert listing["ok"] is True
    assert listing["manifest"]["source_home"] == str(home)
    names = {m["name"] for m in listing["members"]}
    assert "vault/notes.md" in names
    assert "state/nvhive.db" in names
    assert "state/mcp-tools-cache.json" not in names
    assert SNAPSHOT_MANIFEST_NAME not in names
    assert all(m["size"] >= 0 for m in listing["members"])


def test_list_snapshot_errors_are_returned_not_raised(tmp_path: Path) -> None:
    assert list_snapshot(tmp_path / "missing.tar.gz")["ok"] is False
    bad = tmp_path / "bad.tar.gz"
    bad.write_text("not a tarball")
    result = list_snapshot(bad)
    assert result["ok"] is False
    assert "unreadable" in result["error"]


def test_export_snapshot_excludes_secrets(tmp_path: Path) -> None:
    """Secrets paths must never appear in the bundled tarball."""
    home = tmp_path / "home"
    _seed_workspace(home)
    result = export_snapshot(home_dir=home)

    with tarfile.open(result["path"], mode="r:gz") as tar:
        names = tar.getnames()

    # No member should contain any excluded pattern segment.
    for name in names:
        for excluded in SNAPSHOT_EXCLUDES:
            assert excluded not in Path(name).parts, f"{excluded} leaked into snapshot via {name}"


def test_export_snapshot_includes_manifest_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_workspace(home)
    result = export_snapshot(home_dir=home)

    with tarfile.open(result["path"], mode="r:gz") as tar:
        names = tar.getnames()
    assert SNAPSHOT_MANIFEST_NAME in names


def test_import_snapshot_round_trips(tmp_path: Path) -> None:
    """Export then import into a fresh home — files land in the right place."""
    src_home = tmp_path / "src"
    _seed_workspace(src_home)
    exported = export_snapshot(home_dir=src_home)

    dest_home = tmp_path / "dest"
    dest_home.mkdir()
    restored = import_snapshot(exported["path"], home_dir=dest_home)

    assert restored["ok"] is True
    assert restored["extracted"] > 0
    # Real artifacts landed where the manifest claims.
    assert (dest_home / "vault" / "notes.md").read_text().startswith("Migration plan")
    assert (dest_home / "config" / "preferences.yaml").read_text() == "theme: dark\n"
    assert _count_rows(dest_home / "state" / "nvhive.db", "conversations") == 3
    assert _count_rows(dest_home / "rag" / "index.sqlite", "chunks") == 2
    # Excluded secrets did NOT cross over.
    assert not (dest_home / ".env").exists()
    assert not (dest_home / "config" / "secrets.yaml").exists()


def test_import_rejects_non_gzip_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.tar.gz"
    bad.write_text("not a tarball")

    result = import_snapshot(bad, home_dir=tmp_path / "home")
    assert result["ok"] is False
    assert "unreadable" in result["error"]


def test_import_refuses_archive_without_manifest(tmp_path: Path) -> None:
    """A 0.41.0-era `nvh snapshot` tarball (~/.hive + ~/.council, raw keys) must not land."""
    legacy = tmp_path / "legacy.tar.gz"
    with tarfile.open(legacy, mode="w:gz") as tar:
        for name, payload in (
            (".hive/config.yaml", b"providers:\n  openai:\n    api_key: sk-raw\n"),
            (".council/council.db", b"SQLite format 3\x00"),
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

    dest_home = tmp_path / "dest"
    result = import_snapshot(legacy, home_dir=dest_home)

    assert result["ok"] is False
    assert SNAPSHOT_MANIFEST_NAME in result["error"]
    assert "0.41.1" in result["error"]
    assert not (dest_home / ".hive").exists()
    assert not (dest_home / ".council").exists()


def test_import_removes_stale_wal_sidecars(tmp_path: Path) -> None:
    """A -wal/-shm left by a previous DB would be replayed into the restored file."""
    src_home = tmp_path / "src"
    _seed_workspace(src_home)
    exported = export_snapshot(home_dir=src_home)

    dest_home = tmp_path / "dest"
    _sqlite_with_rows(dest_home / "state" / "nvhive.db", "conversations", 1)
    stale_wal = dest_home / "state" / "nvhive.db-wal"
    stale_shm = dest_home / "state" / "nvhive.db-shm"
    stale_wal.write_bytes(b"\x37\x7f\x06\x82" + b"\x00" * 28)
    stale_shm.write_bytes(b"\x00" * 32)

    restored = import_snapshot(exported["path"], home_dir=dest_home, overwrite=True)

    assert restored["ok"] is True
    assert not stale_wal.exists()
    assert not stale_shm.exists()
    assert _count_rows(dest_home / "state" / "nvhive.db", "conversations") == 3


def test_import_writes_db_where_repository_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src_home = tmp_path / "src"
    _seed_workspace(src_home)
    exported = export_snapshot(home_dir=src_home)

    dest_home = tmp_path / "dest"
    data_dir = tmp_path / "persist"
    monkeypatch.setenv("NVH_HOME", str(dest_home))
    monkeypatch.setenv("HIVE_DATA_DIR", str(data_dir))
    monkeypatch.delenv("NVH_STATE", raising=False)

    restored = import_snapshot(exported["path"])

    assert restored["ok"] is True
    assert restored["target_home"] == str(dest_home)
    assert _count_rows(data_dir / "state" / "nvhive.db", "conversations") == 3
    assert not (dest_home / "state" / "nvhive.db").exists()
    assert (dest_home / "vault" / "notes.md").exists()


def test_import_refuses_to_overwrite_by_default(tmp_path: Path) -> None:
    """Existing files at the destination must not be silently clobbered."""
    src_home = tmp_path / "src"
    _seed_workspace(src_home)
    exported = export_snapshot(home_dir=src_home)

    dest_home = tmp_path / "dest"
    dest_home.mkdir()
    (dest_home / "vault").mkdir()
    (dest_home / "vault" / "notes.md").write_text("EXISTING CONTENT")
    _sqlite_with_rows(dest_home / "state" / "nvhive.db", "conversations", 1)

    restored = import_snapshot(exported["path"], home_dir=dest_home, overwrite=False)
    assert restored["ok"] is True
    # Existing file untouched, member counted as skipped.
    assert (dest_home / "vault" / "notes.md").read_text() == "EXISTING CONTENT"
    assert _count_rows(dest_home / "state" / "nvhive.db", "conversations") == 1
    assert restored["skipped"] >= 2


def test_import_overwrites_when_explicitly_requested(tmp_path: Path) -> None:
    src_home = tmp_path / "src"
    _seed_workspace(src_home)
    exported = export_snapshot(home_dir=src_home)

    dest_home = tmp_path / "dest"
    dest_home.mkdir()
    (dest_home / "vault").mkdir()
    (dest_home / "vault" / "notes.md").write_text("STALE")

    restored = import_snapshot(exported["path"], home_dir=dest_home, overwrite=True)
    assert restored["ok"] is True
    assert "Migration plan" in (dest_home / "vault" / "notes.md").read_text()


def test_import_missing_path_returns_error(tmp_path: Path) -> None:
    result = import_snapshot(tmp_path / "does-not-exist.tar.gz", home_dir=tmp_path / "home")
    assert result["ok"] is False
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_conversation_pin_repo_roundtrip(tmp_path: Path) -> None:
    """Pin flag flips on/off via the repo helper."""
    from nvh.storage import repository as repo

    db = tmp_path / "test.db"
    repo._engine = None
    repo._session_factory = None
    await repo.init_db(db_path=db)
    try:
        conv = await repo.create_conversation(provider="ollama", model="x", title="test")
        ok = await repo.set_conversation_pinned(conv.id, True)
        assert ok is True
        pinned = await repo.list_pinned_conversations()
        assert any(c.id == conv.id for c in pinned)
        ok2 = await repo.set_conversation_pinned(conv.id, False)
        assert ok2 is True
        pinned2 = await repo.list_pinned_conversations()
        assert not any(c.id == conv.id for c in pinned2)
        # Pinning a missing conversation returns False, not raises.
        missing = await repo.set_conversation_pinned("ghost", True)
        assert missing is False
    finally:
        repo._engine = None
        repo._session_factory = None
