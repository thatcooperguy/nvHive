"""Workspace snapshot — bundle ``NVH_HOME`` for handoff between desktops.

Rented cloud GPU desktops are ephemeral. When the user moves to a fresh
session — same provider, different host, or a different provider entirely —
they want their *workspace* to follow: vault notes, RAG index, install
receipts, provider config (without secrets), pinned conversations.

This module produces a deterministic tarball that another nvHive install
can extract and pick up from. By design we do NOT bundle:

  - API keys or anything in the secrets store
  - Local model weights (re-pull from Ollama)
  - System binaries (re-install from studio packs)

That keeps the bundle small (~MB, not GB) and avoids leaking credentials
into a file users might mail around. The snapshot is a *configuration*
artifact, not an image.
"""

from __future__ import annotations

import io
import json
import logging
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Paths included in the snapshot, relative to NVH_HOME. Each entry is a
# (path, optional) pair; missing optional paths are skipped silently.
SNAPSHOT_INCLUDES: tuple[tuple[str, bool], ...] = (
    ("vault", True),
    ("rag/index.sqlite", True),
    ("config/preferences.yaml", True),
    ("config/workspace.yaml", True),
    ("receipts", True),
    ("logs/install-passport.json", True),
)

# Path patterns that must NEVER be bundled. .env contains provider keys;
# .secrets is the rootless secrets store path used by some providers.
SNAPSHOT_EXCLUDES: tuple[str, ...] = (
    ".env",
    ".secrets",
    "secrets.yaml",
    "api_keys.json",
)

SNAPSHOT_MANIFEST_NAME = "snapshot.json"
SNAPSHOT_VERSION = 1


def _is_excluded(arcname: str) -> bool:
    parts = Path(arcname).parts
    return any(part in SNAPSHOT_EXCLUDES for part in parts)


def export_snapshot(home_dir: str | Path | None = None) -> dict[str, Any]:
    """Build a tarball of the workspace and return ``{ok, path, bytes, manifest}``.

    The tarball lands at ``$NVH_HOME/snapshots/snapshot-<UTC>.tar.gz`` so the
    user can find it via the file manager without leaving the rootless layout.
    """
    from nvh.integrations.workspace.storage import nvh_home

    home, _source = nvh_home(home_dir)
    out_dir = home / "snapshots"
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"snapshot-{stamp}.tar.gz"

    manifest: dict[str, Any] = {
        "snapshot_version": SNAPSHOT_VERSION,
        "exported_at": stamp,
        "source_home": str(home),
        "includes": [],
        "excluded_patterns": list(SNAPSHOT_EXCLUDES),
    }

    with tarfile.open(out_path, mode="w:gz") as tar:
        for rel, _optional in SNAPSHOT_INCLUDES:
            src = home / rel
            if not src.exists():
                continue
            if _is_excluded(rel):
                continue
            try:
                tar.add(src, arcname=rel, filter=lambda ti: None if _is_excluded(ti.name) else ti)
                manifest["includes"].append(rel)
            except Exception as exc:
                logger.warning("snapshot: failed to add %s: %s", rel, exc)

        # Embed the manifest at the root so import_snapshot can read it
        # without extracting the whole archive.
        manifest_bytes = json.dumps(manifest, indent=2, default=str).encode("utf-8")
        info = tarfile.TarInfo(name=SNAPSHOT_MANIFEST_NAME)
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))

    return {
        "ok": True,
        "path": str(out_path),
        "bytes": out_path.stat().st_size,
        "manifest": manifest,
    }


def import_snapshot(
    tar_path: str | Path,
    *,
    home_dir: str | Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Extract a snapshot tarball into ``NVH_HOME``.

    By default we refuse to overwrite existing files — pass ``overwrite=True``
    to replace conflicting paths. The manifest is returned so the caller can
    verify what landed.
    """
    from nvh.integrations.workspace.storage import nvh_home

    home, _source = nvh_home(home_dir)
    src = Path(tar_path).expanduser().resolve()
    if not src.exists():
        return {"ok": False, "error": f"snapshot not found: {src}"}

    extracted: list[str] = []
    skipped: list[str] = []
    manifest: dict[str, Any] | None = None

    with tarfile.open(src, mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.name == SNAPSHOT_MANIFEST_NAME:
                f = tar.extractfile(member)
                if f is not None:
                    try:
                        manifest = json.loads(f.read().decode("utf-8"))
                    except Exception as exc:
                        logger.warning("snapshot: bad manifest (%s)", exc)
                continue
            if _is_excluded(member.name):
                skipped.append(member.name)
                continue
            dest = home / member.name
            if dest.exists() and not overwrite:
                skipped.append(member.name)
                continue
            try:
                tar.extract(member, path=home, filter="data")
                extracted.append(member.name)
            except Exception as exc:
                logger.warning("snapshot: failed to extract %s: %s", member.name, exc)
                skipped.append(member.name)

    return {
        "ok": True,
        "extracted": len(extracted),
        "skipped": len(skipped),
        "manifest": manifest,
        "target_home": str(home),
    }
