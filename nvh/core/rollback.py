"""Autonomous error handling and rollback.

State snapshots before execution, automatic undo on failure.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class ExecutionCheckpoint:
    """Snapshot of file states at a point in time."""

    checkpoint_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float = field(default_factory=time.time)
    files_snapshot: dict[str, str | None] = field(default_factory=dict)
    description: str = ""
    working_dir: str = ""


@dataclass
class RollbackResult:
    """Outcome of a rollback operation."""

    restored: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def create_checkpoint(
    working_dir: Path, files: list[str], description: str = "",
) -> ExecutionCheckpoint:
    """Read and snapshot the content of each file."""
    snapshot: dict[str, str | None] = {}
    for fpath in files:
        p = Path(fpath)
        if p.exists():
            snapshot[fpath] = p.read_text(encoding="utf-8")
        else:
            snapshot[fpath] = None  # will be deleted on rollback
    return ExecutionCheckpoint(
        files_snapshot=snapshot,
        description=description,
        working_dir=str(working_dir),
    )


def rollback_to_checkpoint(checkpoint: ExecutionCheckpoint) -> RollbackResult:
    """Restore all files to their checkpoint state."""
    result = RollbackResult()
    for fpath, content in checkpoint.files_snapshot.items():
        try:
            p = Path(fpath)
            if content is None:
                if p.exists():
                    p.unlink()
                    result.deleted.append(fpath)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
                result.restored.append(fpath)
        except OSError as exc:
            result.errors.append(f"{fpath}: {exc}")
    return result


def _checkpoints_dir(working_dir: Path) -> Path:
    d = working_dir / ".nvhive" / "checkpoints"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_checkpoint(checkpoint: ExecutionCheckpoint, path: Path) -> None:
    """Persist checkpoint to .nvhive/checkpoints/{id}.json."""
    dest = _checkpoints_dir(path) / f"{checkpoint.checkpoint_id}.json"
    dest.write_text(json.dumps(asdict(checkpoint), indent=2), encoding="utf-8")


def load_checkpoint(path: Path) -> ExecutionCheckpoint:
    """Load a checkpoint from a JSON file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return ExecutionCheckpoint(**data)


def list_checkpoints(working_dir: Path) -> list[ExecutionCheckpoint]:
    """List all saved checkpoints, newest first."""
    cp_dir = working_dir / ".nvhive" / "checkpoints"
    if not cp_dir.exists():
        return []
    checkpoints = [
        load_checkpoint(f)
        for f in cp_dir.glob("*.json")
    ]
    checkpoints.sort(key=lambda c: c.timestamp, reverse=True)
    return checkpoints
