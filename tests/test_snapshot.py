"""Tests for nvh.core.snapshot."""

from __future__ import annotations

import tarfile
from pathlib import Path
from unittest.mock import patch

from nvh.core.snapshot import (
    SnapshotInfo,
    list_snapshot_contents,
    restore_snapshot,
    save_snapshot,
)


def _create_tarball(tmp_path: Path, files: dict[str, str]) -> Path:
    """Helper: create a .tar.gz with the given arcname->content mapping."""
    tarball = tmp_path / "snap.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        for arcname, content in files.items():
            data = content.encode("utf-8")
            import io
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return tarball


class TestSaveSnapshot:
    def test_saves_existing_config_files(self, tmp_path: Path):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        # Create a config file that matches _DEFAULT_PATHS
        config = fake_home / ".hive" / "config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("version: 1", encoding="utf-8")

        output = tmp_path / "out" / "snap.tar.gz"
        with patch("nvh.core.snapshot._home", return_value=fake_home):
            info = save_snapshot(output, include_db=False)

        assert output.exists()
        assert ".hive/config.yaml" in info.files
        assert info.total_size_bytes > 0
        assert info.error is None

    def test_skips_missing_files_gracefully(self, tmp_path: Path):
        fake_home = tmp_path / "empty_home"
        fake_home.mkdir()
        output = tmp_path / "snap.tar.gz"
        with patch("nvh.core.snapshot._home", return_value=fake_home):
            info = save_snapshot(output, include_db=False)
        assert info.files == []
        assert info.error is None

    def test_includes_db_when_requested(self, tmp_path: Path):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        db = fake_home / ".council" / "council.db"
        db.parent.mkdir(parents=True)
        db.write_text("fake-db", encoding="utf-8")

        output = tmp_path / "snap.tar.gz"
        with patch("nvh.core.snapshot._home", return_value=fake_home):
            info = save_snapshot(output, include_db=True)
        assert ".council/council.db" in info.files

    def test_reports_os_error(self, tmp_path: Path):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        # Force an OSError by patching tarfile.open
        output = tmp_path / "snap.tar.gz"
        with patch("nvh.core.snapshot._home", return_value=fake_home):
            with patch("nvh.core.snapshot.tarfile.open", side_effect=OSError("disk full")):
                info = save_snapshot(output, include_db=False)
        assert info.error is not None


class TestRestoreSnapshot:
    def test_restores_files_from_tarball(self, tmp_path: Path):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        tarball = _create_tarball(tmp_path, {"test/file.txt": "hello"})

        with patch("nvh.core.snapshot._home", return_value=fake_home):
            info = restore_snapshot(tarball)

        restored = fake_home / "test" / "file.txt"
        assert restored.exists()
        assert restored.read_text(encoding="utf-8") == "hello"
        assert "test/file.txt" in info.files
        assert info.error is None

    def test_handles_invalid_tarball(self, tmp_path: Path):
        bad_tar = tmp_path / "bad.tar.gz"
        bad_tar.write_text("not a tarball", encoding="utf-8")
        info = restore_snapshot(bad_tar)
        assert info.error is not None

    def test_handles_missing_file(self, tmp_path: Path):
        info = restore_snapshot(tmp_path / "nonexistent.tar.gz")
        assert info.error is not None


class TestListSnapshotContents:
    def test_lists_files_in_tarball(self, tmp_path: Path):
        tarball = _create_tarball(tmp_path, {
            "a.txt": "aaa",
            "sub/b.txt": "bbb",
        })
        contents = list_snapshot_contents(tarball)
        assert "a.txt" in contents
        assert "sub/b.txt" in contents

    def test_returns_empty_for_bad_file(self, tmp_path: Path):
        bad = tmp_path / "nope.tar.gz"
        bad.write_text("garbage", encoding="utf-8")
        assert list_snapshot_contents(bad) == []

    def test_returns_empty_for_missing_file(self, tmp_path: Path):
        assert list_snapshot_contents(tmp_path / "gone.tar.gz") == []


class TestSnapshotInfo:
    def test_default_values(self):
        info = SnapshotInfo()
        assert info.files == []
        assert info.total_size_bytes == 0
        assert info.timestamp == 0.0
        assert info.error is None
