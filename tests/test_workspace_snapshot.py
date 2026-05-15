"""Tests for the workspace snapshot bundle (Tier 2, feature #6)."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from nvh.integrations.workspace.snapshot import (
    SNAPSHOT_EXCLUDES,
    SNAPSHOT_MANIFEST_NAME,
    SNAPSHOT_VERSION,
    export_snapshot,
    import_snapshot,
)


def _seed_workspace(home: Path) -> None:
    """Create a representative NVH_HOME layout in a tmp dir."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "vault").mkdir()
    (home / "vault" / "notes.md").write_text("Migration plan: …")
    (home / "rag").mkdir()
    (home / "rag" / "index.sqlite").write_bytes(b"\x00\x01" * 32)  # fake SQLite header
    (home / "config").mkdir()
    (home / "config" / "preferences.yaml").write_text("theme: dark\n")
    # Secrets that must NOT be bundled.
    (home / ".env").write_text("OPENAI_API_KEY=sk-real-secret")
    (home / "config" / "secrets.yaml").write_text("groq_key: gsk-x")


def test_export_snapshot_returns_tarball_with_manifest(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_workspace(home)

    result = export_snapshot(home_dir=home)
    assert result["ok"] is True
    out = Path(result["path"])
    assert out.exists()
    assert result["bytes"] > 0
    assert result["manifest"]["snapshot_version"] == SNAPSHOT_VERSION
    # vault + rag index + prefs all show up; secret paths do not.
    includes = set(result["manifest"]["includes"])
    assert "vault" in includes
    assert "rag/index.sqlite" in includes
    assert "config/preferences.yaml" in includes


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
    # Excluded secrets did NOT cross over.
    assert not (dest_home / ".env").exists()
    assert not (dest_home / "config" / "secrets.yaml").exists()


def test_import_refuses_to_overwrite_by_default(tmp_path: Path) -> None:
    """Existing files at the destination must not be silently clobbered."""
    src_home = tmp_path / "src"
    _seed_workspace(src_home)
    exported = export_snapshot(home_dir=src_home)

    dest_home = tmp_path / "dest"
    dest_home.mkdir()
    (dest_home / "vault").mkdir()
    (dest_home / "vault" / "notes.md").write_text("EXISTING CONTENT")

    restored = import_snapshot(exported["path"], home_dir=dest_home, overwrite=False)
    assert restored["ok"] is True
    # Existing file untouched, member counted as skipped.
    assert (dest_home / "vault" / "notes.md").read_text() == "EXISTING CONTENT"
    assert restored["skipped"] >= 1


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
