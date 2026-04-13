"""Tests for nvh.core.rollback."""

from __future__ import annotations

from pathlib import Path

from nvh.core.rollback import (
    ExecutionCheckpoint,
    create_checkpoint,
    list_checkpoints,
    load_checkpoint,
    rollback_to_checkpoint,
    save_checkpoint,
)


class TestCreateCheckpoint:
    def test_snapshots_existing_files(self, tmp_path: Path):
        f1 = tmp_path / "a.py"
        f1.write_text("print('hello')", encoding="utf-8")
        cp = create_checkpoint(tmp_path, [str(f1)], description="before edit")
        assert str(f1) in cp.files_snapshot
        assert cp.files_snapshot[str(f1)] == "print('hello')"
        assert cp.description == "before edit"

    def test_snapshots_missing_file_as_none(self, tmp_path: Path):
        missing = tmp_path / "nonexistent.py"
        cp = create_checkpoint(tmp_path, [str(missing)])
        assert cp.files_snapshot[str(missing)] is None

    def test_checkpoint_has_unique_id(self, tmp_path: Path):
        cp1 = create_checkpoint(tmp_path, [])
        cp2 = create_checkpoint(tmp_path, [])
        assert cp1.checkpoint_id != cp2.checkpoint_id

    def test_checkpoint_stores_working_dir(self, tmp_path: Path):
        cp = create_checkpoint(tmp_path, [])
        assert cp.working_dir == str(tmp_path)

    def test_multiple_files_snapshotted(self, tmp_path: Path):
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("aaa", encoding="utf-8")
        f2.write_text("bbb", encoding="utf-8")
        cp = create_checkpoint(tmp_path, [str(f1), str(f2)])
        assert len(cp.files_snapshot) == 2
        assert cp.files_snapshot[str(f1)] == "aaa"
        assert cp.files_snapshot[str(f2)] == "bbb"


class TestRollbackToCheckpoint:
    def test_restores_modified_file(self, tmp_path: Path):
        f = tmp_path / "code.py"
        f.write_text("original", encoding="utf-8")
        cp = create_checkpoint(tmp_path, [str(f)])
        f.write_text("modified", encoding="utf-8")
        result = rollback_to_checkpoint(cp)
        assert f.read_text(encoding="utf-8") == "original"
        assert str(f) in result.restored

    def test_deletes_newly_created_file(self, tmp_path: Path):
        new_file = tmp_path / "new.py"
        # Snapshot says file didn't exist (None)
        cp = ExecutionCheckpoint(
            files_snapshot={str(new_file): None},
            working_dir=str(tmp_path),
        )
        new_file.write_text("should be deleted", encoding="utf-8")
        result = rollback_to_checkpoint(cp)
        assert not new_file.exists()
        assert str(new_file) in result.deleted

    def test_creates_parent_dirs_on_restore(self, tmp_path: Path):
        deep = tmp_path / "sub" / "dir" / "file.py"
        cp = ExecutionCheckpoint(
            files_snapshot={str(deep): "restored content"},
            working_dir=str(tmp_path),
        )
        result = rollback_to_checkpoint(cp)
        assert deep.exists()
        assert deep.read_text(encoding="utf-8") == "restored content"
        assert str(deep) in result.restored

    def test_noop_delete_when_file_already_gone(self, tmp_path: Path):
        ghost = tmp_path / "ghost.py"
        cp = ExecutionCheckpoint(
            files_snapshot={str(ghost): None},
            working_dir=str(tmp_path),
        )
        # File doesn't exist, rollback should not error
        result = rollback_to_checkpoint(cp)
        assert result.deleted == []
        assert result.errors == []


class TestSaveAndLoadCheckpoint:
    def test_roundtrip(self, tmp_path: Path):
        f = tmp_path / "src.py"
        f.write_text("content", encoding="utf-8")
        cp = create_checkpoint(tmp_path, [str(f)], description="test save")
        save_checkpoint(cp, tmp_path)

        cp_dir = tmp_path / ".nvhive" / "checkpoints"
        saved_files = list(cp_dir.glob("*.json"))
        assert len(saved_files) == 1

        loaded = load_checkpoint(saved_files[0])
        assert loaded.checkpoint_id == cp.checkpoint_id
        assert loaded.description == "test save"
        assert loaded.files_snapshot[str(f)] == "content"

    def test_load_preserves_all_fields(self, tmp_path: Path):
        cp = ExecutionCheckpoint(
            checkpoint_id="abc123",
            timestamp=1000.0,
            files_snapshot={"x.py": "hello"},
            description="desc",
            working_dir="/some/dir",
        )
        save_checkpoint(cp, tmp_path)
        loaded = load_checkpoint(
            tmp_path / ".nvhive" / "checkpoints" / "abc123.json"
        )
        assert loaded.checkpoint_id == "abc123"
        assert loaded.timestamp == 1000.0
        assert loaded.working_dir == "/some/dir"


class TestListCheckpoints:
    def test_empty_dir_returns_empty(self, tmp_path: Path):
        result = list_checkpoints(tmp_path)
        assert result == []

    def test_lists_saved_checkpoints_newest_first(self, tmp_path: Path):
        cp1 = ExecutionCheckpoint(
            checkpoint_id="old", timestamp=100.0,
            files_snapshot={}, working_dir=str(tmp_path),
        )
        cp2 = ExecutionCheckpoint(
            checkpoint_id="new", timestamp=200.0,
            files_snapshot={}, working_dir=str(tmp_path),
        )
        save_checkpoint(cp1, tmp_path)
        save_checkpoint(cp2, tmp_path)
        result = list_checkpoints(tmp_path)
        assert len(result) == 2
        assert result[0].checkpoint_id == "new"
        assert result[1].checkpoint_id == "old"
