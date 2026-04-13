"""Environment snapshot/restore for ephemeral cloud VMs.

Save and restore config, learned scores, agent memory across VM sessions.
Pure stdlib: tarfile, pathlib, os, time, dataclasses.
"""

from __future__ import annotations

import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path

# Paths collected by default (relative to home).
_DEFAULT_PATHS: list[str] = [
    ".hive/config.yaml",
    ".nvhive/agent-memory.json",
]
_DB_PATH = ".council/council.db"


@dataclass
class SnapshotInfo:
    """Metadata returned after save/restore operations."""

    files: list[str] = field(default_factory=list)
    total_size_bytes: int = 0
    timestamp: float = 0.0
    error: str | None = None


def _home() -> Path:
    return Path.home()


def save_snapshot(
    output_path: Path,
    include_db: bool = True,
) -> SnapshotInfo:
    """Collect config/data files into a tarball at *output_path*."""
    home = _home()
    candidates = list(_DEFAULT_PATHS)
    if include_db:
        candidates.append(_DB_PATH)

    info = SnapshotInfo(timestamp=time.time())
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output_path, "w:gz") as tar:
            for rel in candidates:
                full = home / rel
                if full.is_file():
                    tar.add(str(full), arcname=rel)
                    info.files.append(rel)
                    info.total_size_bytes += full.stat().st_size
    except OSError as exc:
        info.error = str(exc)
    return info


def restore_snapshot(snapshot_path: Path) -> SnapshotInfo:
    """Extract a tarball, restoring files to their original locations."""
    home = _home()
    info = SnapshotInfo(timestamp=time.time())
    try:
        with tarfile.open(snapshot_path, "r:gz") as tar:
            for member in tar.getmembers():
                if member.isfile():
                    dest = home / member.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(member) as src:  # type: ignore[union-attr]
                        dest.write_bytes(src.read())
                    info.files.append(member.name)
                    info.total_size_bytes += member.size
    except (OSError, tarfile.TarError) as exc:
        info.error = str(exc)
    return info


def list_snapshot_contents(snapshot_path: Path) -> list[str]:
    """List files in a snapshot tarball without extracting."""
    try:
        with tarfile.open(snapshot_path, "r:gz") as tar:
            return [m.name for m in tar.getmembers() if m.isfile()]
    except (OSError, tarfile.TarError):
        return []


# Keep module slim – suppress unused-import warnings for os.
__all__ = [
    "SnapshotInfo",
    "save_snapshot",
    "restore_snapshot",
    "list_snapshot_contents",
]
