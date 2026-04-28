"""Tests for rootless persistent mount discovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nvh.integrations import mount_autopilot as autopilot


class _FakeStorageStatus:
    def as_dict(self) -> dict[str, Any]:
        return {"ok": True, "warnings": []}


def _stub_current_storage(monkeypatch) -> None:
    monkeypatch.setattr(autopilot, "storage_status", lambda **_: _FakeStorageStatus())


def _mount(path: Path, fs_type: str, source: str, options: set[str] | None = None) -> autopilot.MountInfo:
    return autopilot.MountInfo(
        mount_point=path,
        fs_type=fs_type,
        source=source,
        options=options or {"rw", "relatime"},
    )


def _stub_paths(monkeypatch, *paths: Path) -> None:
    known = {str(path) for path in paths}
    monkeypatch.setattr(autopilot, "_path_exists", lambda path: str(path) in known)
    monkeypatch.setattr(autopilot, "_evidence", lambda path: [])


def test_mount_autopilot_prefers_large_block_backed_home(monkeypatch) -> None:
    home = Path("/home/student")
    share = Path("/mnt/readonly-share")
    _stub_current_storage(monkeypatch)
    _stub_paths(monkeypatch, home, share)
    monkeypatch.setattr(autopilot.Path, "home", lambda: home)
    monkeypatch.setattr(autopilot, "_candidate_paths", lambda _: [("home", home), ("mount", share)])
    monkeypatch.setattr(autopilot, "_is_writable", lambda path: "readonly" not in str(path))
    monkeypatch.setattr(
        autopilot,
        "_disk_usage",
        lambda path: (860.0, 1000.0) if str(path).startswith(str(home)) else (450.0, 500.0),
    )
    monkeypatch.setattr(
        autopilot,
        "_mount_info_for_path",
        lambda path: _mount(home, "ext4", "/dev/nvme1n1")
        if str(path).startswith(str(home))
        else _mount(share, "cifs", "//fileserver/share", {"ro", "relatime"}),
    )

    report = autopilot.mount_autopilot_report(min_free_gb=20)

    assert report["confidence"] == "high"
    assert report["recommended"]["recommended_home"] == str(home / "nvhive")
    assert report["recommended"]["large_block_mount"] is True
    assert "home-on-persistent-block-mount" in report["recommended"]["evidence"]


def test_mount_autopilot_downranks_read_only_network_mount(monkeypatch) -> None:
    block = Path("/mnt/persistent-block")
    share = Path("/mnt/readonly-share")
    _stub_current_storage(monkeypatch)
    _stub_paths(monkeypatch, block, share)
    monkeypatch.setattr(autopilot, "_candidate_paths", lambda _: [("mount", share), ("mount", block)])
    monkeypatch.setattr(autopilot, "_is_writable", lambda path: path == block)
    monkeypatch.setattr(
        autopilot,
        "_disk_usage",
        lambda path: (950.0, 1000.0) if path == share else (430.0, 500.0),
    )
    monkeypatch.setattr(
        autopilot,
        "_mount_info_for_path",
        lambda path: _mount(share, "cifs", "//fileserver/share", {"ro", "relatime"})
        if path == share
        else _mount(block, "xfs", "/dev/disk/by-id/nvh-persist"),
    )

    report = autopilot.mount_autopilot_report(min_free_gb=20)
    share_candidate = next(candidate for candidate in report["candidates"] if candidate["path"] == str(share))

    assert report["recommended"]["path"] == str(block)
    assert share_candidate["read_only"] is True
    assert share_candidate["network_mount"] is True
    assert any("read-only" in warning for warning in share_candidate["warnings"])


def test_mount_autopilot_downranks_os_root_disk(monkeypatch) -> None:
    os_home = Path("/home/student")
    persistent = Path("/mnt/persistent-block")
    _stub_current_storage(monkeypatch)
    _stub_paths(monkeypatch, os_home, persistent)
    monkeypatch.setattr(autopilot.Path, "home", lambda: os_home)
    monkeypatch.setattr(autopilot, "_candidate_paths", lambda _: [("home", os_home), ("mount", persistent)])
    monkeypatch.setattr(autopilot, "_is_writable", lambda path: True)
    monkeypatch.setattr(
        autopilot,
        "_disk_usage",
        lambda path: (860.0, 1000.0) if path == os_home else (430.0, 500.0),
    )
    monkeypatch.setattr(
        autopilot,
        "_mount_info_for_path",
        lambda path: _mount(Path("/"), "ext4", "/dev/nvme0n1")
        if path == os_home
        else _mount(persistent, "ext4", "/dev/nvme1n1"),
    )

    report = autopilot.mount_autopilot_report(min_free_gb=20)
    os_candidate = next(candidate for candidate in report["candidates"] if candidate["path"] == str(os_home))

    assert report["recommended"]["path"] == str(persistent)
    assert os_candidate["os_mount"] is True
    assert any("OS/root disk" in warning for warning in os_candidate["warnings"])
