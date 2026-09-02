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
import os
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Paths included in the snapshot, relative to NVH_HOME. Each entry is a
# (path, optional) pair; missing optional paths are skipped silently.
SNAPSHOT_INCLUDES: tuple[tuple[str, bool], ...] = (
    ("vault", True),
    ("config/preferences.yaml", True),
    ("config/workspace.yaml", True),
    ("receipts", True),
    ("logs/install-passport.json", True),
)

# SQLite files run in WAL mode while the API is up, so a plain copy of the
# main file (or of the db/-wal pair, read at different instants) can be torn.
# They are captured through sqlite3's backup API into a single consistent
# image and stored under the path a fresh home expects.
STATE_DB_ARCNAME = "state/nvhive.db"
RAG_INDEX_ARCNAME = "rag/index.sqlite"
_DB_SIDECARS = ("-wal", "-shm")
_DB_SIDECAR_ARCNAMES = frozenset(
    f"{arcname}{suffix}"
    for arcname in (STATE_DB_ARCNAME, RAG_INDEX_ARCNAME)
    for suffix in _DB_SIDECARS
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


def _database_paths(home: Path, home_dir: str | Path | None) -> dict[str, Path]:
    """Map DB arcnames to where they live for this home.

    An explicit ``home_dir`` is authoritative, as in storage_layout(); otherwise
    the state DB is wherever the repository puts it, so installs relocated via
    HIVE_DATA_DIR / NVH_STATE are found rather than silently omitted.
    """
    if home_dir is None:
        from nvh.storage.repository import _default_db_path

        state_db = _default_db_path()
    else:
        state_db = home / STATE_DB_ARCNAME
    return {STATE_DB_ARCNAME: state_db, RAG_INDEX_ARCNAME: home / RAG_INDEX_ARCNAME}


def _backup_sqlite(src: Path, dest: Path) -> None:
    with closing(sqlite3.connect(src)) as conn, closing(sqlite3.connect(dest)) as copy:
        conn.backup(copy)


def _read_manifest(tar: tarfile.TarFile, members: list[tarfile.TarInfo]) -> dict[str, Any] | None:
    for member in members:
        if member.name != SNAPSHOT_MANIFEST_NAME:
            continue
        f = tar.extractfile(member)
        if f is None:
            return None
        try:
            return json.loads(f.read().decode("utf-8"))
        except Exception as exc:
            logger.warning("snapshot: bad manifest (%s)", exc)
            return None
    return None


def _safe_extract(tar: tarfile.TarFile, member: tarfile.TarInfo, home: Path) -> None:
    """Extract one member under ``home``, refusing links and path escapes.

    Python 3.11.4+ enforces this via ``filter="data"``; older 3.11 patch
    releases have no ``filter`` kwarg, so the same checks are applied by hand.
    """
    if member.issym() or member.islnk():
        raise ValueError(f"refusing to extract link member {member.name}")
    dest = (home / member.name).resolve()
    if not dest.is_relative_to(home.resolve()):
        raise ValueError(f"refusing to extract outside NVH_HOME: {member.name}")
    try:
        tar.extract(member, path=home, filter="data")
    except TypeError:
        tar.extract(member, path=home)


def _restore_sqlite(tar: tarfile.TarFile, member: tarfile.TarInfo, dest: Path) -> None:
    """Write a bundled DB image to ``dest`` as one unit with its sidecars.

    A -wal/-shm left behind by whatever database used to live at ``dest`` would
    be replayed into the restored file on first open ("database disk image is
    malformed"), so they go before the new image is moved into place.
    """
    if not member.isfile():
        raise ValueError(f"refusing to restore non-file member {member.name}")
    f = tar.extractfile(member)
    if f is None:
        raise ValueError(f"empty member {member.name}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    with f, tmp.open("wb") as out:
        shutil.copyfileobj(f, out)
    for suffix in _DB_SIDECARS:
        dest.with_name(dest.name + suffix).unlink(missing_ok=True)
    os.replace(tmp, dest)


def export_snapshot(
    home_dir: str | Path | None = None,
    out_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build a tarball of the workspace and return ``{ok, path, bytes, manifest}``.

    By default the tarball lands at ``$NVH_HOME/snapshots/snapshot-<UTC>.tar.gz``
    so the user can find it via the file manager without leaving the rootless
    layout; ``out_path`` overrides that (the CLI's ``-o``).
    """
    from nvh.integrations.workspace.storage import nvh_home

    home, _source = nvh_home(home_dir)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if out_path is None:
        out_path = home / "snapshots" / f"snapshot-{stamp}.tar.gz"
    else:
        out_path = Path(out_path).expanduser()

    manifest: dict[str, Any] = {
        "snapshot_version": SNAPSHOT_VERSION,
        "exported_at": stamp,
        "source_home": str(home),
        "includes": [],
        "databases": {},
        "excluded_patterns": list(SNAPSHOT_EXCLUDES),
    }

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with (
            tempfile.TemporaryDirectory(prefix="nvh-snapshot-") as tmp,
            tarfile.open(out_path, mode="w:gz") as tar,
        ):
            for rel, _optional in SNAPSHOT_INCLUDES:
                src = home / rel
                if not src.exists():
                    continue
                if _is_excluded(rel):
                    continue
                try:
                    tar.add(
                        src, arcname=rel, filter=lambda ti: None if _is_excluded(ti.name) else ti
                    )
                    manifest["includes"].append(rel)
                except Exception as exc:
                    logger.warning("snapshot: failed to add %s: %s", rel, exc)

            for arcname, src in _database_paths(home, home_dir).items():
                if not src.exists():
                    continue
                copy = Path(tmp) / Path(arcname).name
                try:
                    _backup_sqlite(src, copy)
                    tar.add(copy, arcname=arcname)
                except (sqlite3.Error, OSError) as exc:
                    logger.warning("snapshot: failed to back up %s: %s", src, exc)
                    continue
                manifest["includes"].append(arcname)
                manifest["databases"][arcname] = {"source": str(src), "method": "sqlite-backup"}

            # Embed the manifest at the root so import_snapshot can read it
            # without extracting the whole archive.
            manifest_bytes = json.dumps(manifest, indent=2, default=str).encode("utf-8")
            info = tarfile.TarInfo(name=SNAPSHOT_MANIFEST_NAME)
            info.size = len(manifest_bytes)
            tar.addfile(info, io.BytesIO(manifest_bytes))
    except OSError as exc:
        if out_path.is_file():
            out_path.unlink()
        return {"ok": False, "error": f"cannot write snapshot {out_path}: {exc}"}

    return {
        "ok": True,
        "path": str(out_path),
        "bytes": out_path.stat().st_size,
        "manifest": manifest,
    }


def list_snapshot(tar_path: str | Path) -> dict[str, Any]:
    """Read a snapshot's manifest and file members without extracting anything."""
    src = Path(tar_path).expanduser()
    if not src.exists():
        return {"ok": False, "error": f"snapshot not found: {src}"}

    try:
        with tarfile.open(src, mode="r:gz") as tar:
            all_members = tar.getmembers()
            manifest = _read_manifest(tar, all_members)
            members = [
                {"name": m.name, "size": m.size}
                for m in all_members
                if m.isfile() and m.name != SNAPSHOT_MANIFEST_NAME
            ]
    except (OSError, tarfile.TarError) as exc:
        return {"ok": False, "error": f"unreadable snapshot {src}: {exc}"}
    return {"ok": True, "manifest": manifest, "members": members}


async def import_snapshot_from_url(
    url: str,
    *,
    home_dir: str | Path | None = None,
    overwrite: bool = False,
    max_bytes: int = 200 * 1024 * 1024,
) -> dict[str, Any]:
    """Download a snapshot tarball from ``url`` and extract it locally.

    Caps the download at ``max_bytes`` (default 200 MB) so a runaway URL
    doesn't fill the persistent mount. The downloaded file lands under
    ``$NVH_HOME/snapshots/incoming/<timestamp>-<basename>.tar.gz`` so it
    survives reconnect and can be re-extracted later without re-downloading.
    """
    import asyncio
    import urllib.parse
    from datetime import UTC, datetime

    import httpx

    from nvh.integrations.workspace.storage import nvh_home

    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "URL must be http:// or https://"}

    home, _src = nvh_home(home_dir)
    incoming = home / "snapshots" / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)

    parsed = urllib.parse.urlparse(url)
    base = Path(parsed.path).name or "snapshot.tar.gz"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = incoming / f"{stamp}-{base}"

    try:
        async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    return {"ok": False, "error": f"download failed: HTTP {resp.status_code}"}
                total = 0
                with dest.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            fh.close()
                            dest.unlink(missing_ok=True)
                            return {
                                "ok": False,
                                "error": f"snapshot exceeds {max_bytes // (1024 * 1024)} MB cap",
                            }
                        fh.write(chunk)
    except httpx.HTTPError as exc:
        dest.unlink(missing_ok=True)
        return {"ok": False, "error": f"download error: {exc}"}

    result = await asyncio.to_thread(
        import_snapshot, dest, home_dir=home_dir, overwrite=overwrite
    )
    result["downloaded_from"] = url
    result["downloaded_bytes"] = total
    result["local_path"] = str(dest)
    return result


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
    databases = _database_paths(home, home_dir)

    try:
        with tarfile.open(src, mode="r:gz") as tar:
            members = tar.getmembers()
            manifest = _read_manifest(tar, members)
            if manifest is None:
                return {
                    "ok": False,
                    "error": (
                        f"{src} has no readable {SNAPSHOT_MANIFEST_NAME}, so it is not an "
                        "nvHive workspace snapshot. Archives written by `nvh snapshot` before "
                        "0.41.1 bundled ~/.hive and ~/.council (config.yaml included, raw keys "
                        "and all) and cannot be restored; export a fresh one with "
                        "`nvh snapshot save`."
                    ),
                }
            for member in members:
                if member.name == SNAPSHOT_MANIFEST_NAME:
                    continue
                if _is_excluded(member.name) or member.name in _DB_SIDECAR_ARCNAMES:
                    skipped.append(member.name)
                    continue
                dest = databases.get(member.name, home / member.name)
                if dest.exists() and not overwrite:
                    skipped.append(member.name)
                    continue
                try:
                    if member.name in databases:
                        _restore_sqlite(tar, member, dest)
                    else:
                        _safe_extract(tar, member, home)
                    extracted.append(member.name)
                except Exception as exc:
                    logger.warning("snapshot: failed to extract %s: %s", member.name, exc)
                    skipped.append(member.name)
    except (OSError, tarfile.TarError) as exc:
        return {"ok": False, "error": f"unreadable snapshot {src}: {exc}"}

    return {
        "ok": True,
        "extracted": len(extracted),
        "skipped": len(skipped),
        "manifest": manifest,
        "target_home": str(home),
    }
