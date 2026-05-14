"""Rootless Node.js runtime helpers.

The Linux-first install path cannot assume sudo, apt, snap, or a preinstalled
Node runtime. These helpers keep Node under ``NVH_HOME/runtimes`` and also
recognize fnm's default user-level install path when its installer ignores
``FNM_DIR``.
"""

from __future__ import annotations

import os
import platform
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from nvh.integrations.workspace.storage import storage_layout

NODE_MAJOR_VERSION = "22"


def _path_from_env(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def find_fnm_binary(fnm_root: str | Path | None = None) -> str | None:
    """Return an fnm binary from PATH, NVH_HOME, or fnm's default user path."""
    found = shutil.which("fnm")
    if found:
        return found

    roots: list[Path] = []
    if fnm_root:
        roots.append(Path(fnm_root).expanduser())
    env_root = _path_from_env("FNM_DIR")
    if env_root:
        roots.append(env_root)
    roots.extend([
        Path.home() / ".local" / "share" / "fnm",
        Path.home() / ".fnm",
    ])

    seen: set[Path] = set()
    candidates: list[Path] = []
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        candidates.extend([
            root / "fnm",
            root / "fnm.exe",
            root / "bin" / "fnm",
            root / "bin" / "fnm.exe",
        ])
        if root.exists():
            candidates.extend(root.glob("**/fnm"))
            candidates.extend(root.glob("**/fnm.exe"))

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _bin_has_node_and_npm(path: Path) -> bool:
    suffix = ".cmd" if os.name == "nt" else ""
    return (path / "node").exists() and (path / f"npm{suffix}").exists()


def find_rootless_node_bin(
    runtime_dir: str | Path | None = None,
    *,
    major: str = NODE_MAJOR_VERSION,
) -> Path | None:
    """Find a Node/npm bin directory managed by nvHive or fnm."""
    base = Path(runtime_dir).expanduser() if runtime_dir else storage_layout().runtime_dir
    candidates: list[Path] = [
        base / "node" / "bin",
        base / "node" / "current" / "bin",
    ]
    candidates.extend(sorted((base / "node").glob("node-v*/bin"), reverse=True))
    candidates.extend(sorted((base / "fnm" / "node-versions").glob(f"v{major}.*/installation/bin"), reverse=True))

    for root in [
        _path_from_env("FNM_DIR"),
        Path.home() / ".local" / "share" / "fnm",
        Path.home() / ".fnm",
    ]:
        if root:
            candidates.extend(sorted((root / "node-versions").glob(f"v{major}.*/installation/bin"), reverse=True))

    for candidate in candidates:
        if _bin_has_node_and_npm(candidate):
            return candidate
    return None


def _node_arch() -> str:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "x64"
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    raise RuntimeError(f"Unsupported Node.js Linux architecture: {platform.machine()}")


def _latest_node_archive_name(major: str, arch: str) -> str:
    url = f"https://nodejs.org/dist/latest-v{major}.x/SHASUMS256.txt"
    with urllib.request.urlopen(url, timeout=30) as response:
        text = response.read().decode("utf-8", errors="replace")
    needle = f"linux-{arch}.tar.xz"
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].endswith(needle):
            return parts[1]
    raise RuntimeError(f"Could not find latest Node.js v{major} Linux {arch} archive")


def _safe_extract_tar_xz(archive: Path, target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    target_root = target.resolve()
    with tarfile.open(archive, "r:xz") as tar:
        members = tar.getmembers()
        top_dirs = {member.name.split("/", 1)[0] for member in members if member.name}
        for member in members:
            destination = (target / member.name).resolve()
            if not destination.is_relative_to(target_root):
                raise RuntimeError(f"Unsafe path in Node.js archive: {member.name}")
        tar.extractall(target, members)
    if len(top_dirs) != 1:
        raise RuntimeError("Node.js archive did not contain one top-level directory")
    return target / next(iter(top_dirs))


def install_node_tarball(
    runtime_dir: str | Path | None = None,
    *,
    major: str = NODE_MAJOR_VERSION,
) -> Path:
    """Install Node.js directly from nodejs.org into NVH_HOME/runtimes/node."""
    layout = storage_layout()
    runtime_root = Path(runtime_dir).expanduser() if runtime_dir else layout.runtime_dir
    runtime_root.mkdir(parents=True, exist_ok=True)
    existing = find_rootless_node_bin(runtime_root, major=major)
    if existing:
        return existing

    arch = _node_arch()
    archive_name = _latest_node_archive_name(major, arch)
    archive_url = f"https://nodejs.org/dist/latest-v{major}.x/{archive_name}"
    downloads = layout.cache_dir / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / archive_name
    if not archive.exists() or archive.stat().st_size == 0:
        urllib.request.urlretrieve(archive_url, archive)

    node_root = runtime_root / "node"
    stage = Path(tempfile.mkdtemp(prefix="node-", dir=str(runtime_root)))
    try:
        extracted = _safe_extract_tar_xz(archive, stage)
        target = node_root / extracted.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(extracted), str(target))
        current = node_root / "current"
        try:
            if current.exists() or current.is_symlink():
                if current.is_dir() and not current.is_symlink():
                    shutil.rmtree(current)
                else:
                    current.unlink()
            current.symlink_to(target, target_is_directory=True)
        except Exception:
            pass
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    bin_dir = target / "bin"
    if not _bin_has_node_and_npm(bin_dir):
        raise RuntimeError(f"Node.js archive did not provide node and npm in {bin_dir}")
    return bin_dir
