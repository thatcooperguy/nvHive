"""Persistent mount discovery for rootless cloud desktop sessions."""

from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from nvh.integrations.storage import ensure_storage, storage_status

DEFAULT_MIN_FREE_GB = 20.0

NETWORK_FS_TYPES = {
    "cifs",
    "smb3",
    "nfs",
    "nfs4",
    "sshfs",
    "fuse.sshfs",
    "9p",
    "davfs",
    "ceph",
    "glusterfs",
}
EPHEMERAL_FS_TYPES = {
    "autofs",
    "cgroup",
    "cgroup2",
    "configfs",
    "debugfs",
    "devpts",
    "devtmpfs",
    "fuse.portal",
    "mqueue",
    "overlay",
    "proc",
    "ramfs",
    "securityfs",
    "squashfs",
    "sysfs",
    "tmpfs",
    "tracefs",
}
LOCAL_BLOCK_FS_TYPES = {
    "bcachefs",
    "btrfs",
    "ext2",
    "ext3",
    "ext4",
    "f2fs",
    "xfs",
    "zfs",
}
PREFERRED_MOUNT_PREFIXES = (
    "/mnt",
    "/media",
    "/workspace",
    "/data",
    "/persistent",
    "/storage",
)
OS_MOUNT_PREFIXES = (
    "/boot",
    "/etc",
    "/nix",
    "/opt",
    "/root",
    "/run",
    "/snap",
    "/tmp",
    "/usr",
    "/var",
)


@dataclass(frozen=True)
class MountInfo:
    """Linux mount metadata for a path."""

    mount_point: Path
    fs_type: str
    source: str
    options: set[str]


@dataclass(frozen=True)
class MountCandidate:
    """One possible persistent storage location."""

    path: str
    recommended_home: str
    label: str
    source: str
    exists: bool
    writable: bool
    free_gb: float | None
    total_gb: float | None
    fs_type: str | None
    device: str | None
    mount_point: str | None
    read_only: bool
    network_mount: bool
    os_mount: bool
    large_block_mount: bool
    score: int
    warnings: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _expand(path: str | Path) -> Path:
    return Path(os.path.expandvars(str(path))).expanduser()


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except Exception:
        return False


def _path_is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except Exception:
        return False


def _candidate_key(path: Path) -> str:
    try:
        return str(path.resolve() if _path_exists(path) else path)
    except Exception:
        return str(path)


def _decode_mount_path(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _parse_mountinfo_line(line: str) -> MountInfo | None:
    parts = line.strip().split()
    if "-" not in parts or len(parts) < 10:
        return None
    separator = parts.index("-")
    if separator + 3 >= len(parts):
        return None
    mount_point = Path(_decode_mount_path(parts[4]))
    options = set(parts[5].split(","))
    fs_type = parts[separator + 1].lower()
    source = _decode_mount_path(parts[separator + 2])
    if separator + 3 < len(parts):
        options.update(parts[separator + 3].split(","))
    return MountInfo(mount_point=mount_point, fs_type=fs_type, source=source, options=options)


def _parse_mounts_line(line: str) -> MountInfo | None:
    parts = line.strip().split()
    if len(parts) < 4:
        return None
    return MountInfo(
        mount_point=Path(_decode_mount_path(parts[1])),
        fs_type=parts[2].lower(),
        source=_decode_mount_path(parts[0]),
        options=set(parts[3].split(",")),
    )


def _mount_table() -> list[MountInfo]:
    for mount_file, parser in (
        (Path("/proc/self/mountinfo"), _parse_mountinfo_line),
        (Path("/proc/mounts"), _parse_mounts_line),
    ):
        try:
            lines = mount_file.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        mounts = [mount for line in lines if (mount := parser(line))]
        if mounts:
            return mounts
    return []


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _mount_info_for_path(path: Path) -> MountInfo | None:
    probe = _nearest_existing(path) or path
    mounts = _mount_table()
    matches = [mount for mount in mounts if _is_relative_to(probe, mount.mount_point)]
    if not matches:
        return None
    return max(matches, key=lambda mount: len(mount.mount_point.parts))


def _is_network_mount(info: MountInfo | None) -> bool:
    return bool(info and info.fs_type in NETWORK_FS_TYPES)


def _is_ephemeral_mount(info: MountInfo | None) -> bool:
    return bool(info and info.fs_type in EPHEMERAL_FS_TYPES)


def _is_read_only_mount(info: MountInfo | None) -> bool:
    return bool(info and "ro" in info.options)


def _is_os_mount(path: Path, info: MountInfo | None) -> bool:
    if info is None:
        return False
    mount_point = info.mount_point.as_posix()
    if mount_point == "/":
        return True
    return any(mount_point == prefix or mount_point.startswith(f"{prefix}/") for prefix in OS_MOUNT_PREFIXES)


def _is_large_block_mount(info: MountInfo | None, total_gb: float | None) -> bool:
    if info is None or total_gb is None:
        return False
    if _is_network_mount(info) or _is_ephemeral_mount(info):
        return False
    local_signal = info.fs_type in LOCAL_BLOCK_FS_TYPES or info.source.startswith("/dev/")
    return local_signal and total_gb >= 180


def _nearest_existing(path: Path) -> Path | None:
    current = path
    while not _path_exists(current) and current.parent != current:
        current = current.parent
    return current if _path_exists(current) else None


def _disk_usage(path: Path) -> tuple[float | None, float | None]:
    probe = _nearest_existing(path)
    if probe is None:
        return None, None
    try:
        usage = shutil.disk_usage(probe)
    except Exception:
        return None, None
    gb = 1024 ** 3
    return round(usage.free / gb, 1), round(usage.total / gb, 1)


def _is_writable(path: Path) -> bool:
    probe = _nearest_existing(path)
    if probe is None:
        return False
    try:
        return os.access(probe, os.W_OK)
    except Exception:
        return False


def _looks_ephemeral(path: Path) -> bool:
    parts = {part.lower() for part in path.parts}
    return bool(parts.intersection({"tmp", "temp", "run", "var", "cache", "pytest-tmp"}))


def _evidence(path: Path) -> list[str]:
    evidence: list[str] = []
    for marker in ("nvh-env.sh", "receipts", "models", "comfyui", "studio", "apps"):
        if _path_exists(path / marker):
            evidence.append(marker)
    return evidence


def _recommended_home(path: Path) -> Path:
    if path.name.lower() in {"nvh", "nvhive", ".nvh"}:
        return path
    return path / "nvhive"


def _score_candidate(
    path: Path,
    *,
    source: str,
    min_free_gb: float,
) -> MountCandidate:
    exists = _path_exists(path)
    writable = _is_writable(path)
    free_gb, total_gb = _disk_usage(path)
    mount_info = _mount_info_for_path(path)
    fs_type = mount_info.fs_type if mount_info else None
    device = mount_info.source if mount_info else None
    mount_point = str(mount_info.mount_point) if mount_info else None
    read_only = _is_read_only_mount(mount_info)
    network_mount = _is_network_mount(mount_info)
    ephemeral_mount = _is_ephemeral_mount(mount_info)
    os_mount = _is_os_mount(path, mount_info)
    large_block_mount = _is_large_block_mount(mount_info, total_gb)
    evidence = _evidence(path) + _evidence(_recommended_home(path))
    warnings: list[str] = []
    score = 0

    if exists:
        score += 10
    else:
        warnings.append("Path does not exist yet; nvHive can create the final NVH_HOME inside it if parent is writable.")
    if writable:
        score += 30
    else:
        score -= 70
        warnings.append("Path is not writable by this user.")
    if free_gb is not None:
        if free_gb >= min_free_gb:
            score += 25
        else:
            warnings.append(f"Only {free_gb} GB free; recommended minimum is {min_free_gb:.0f} GB.")
    if total_gb is not None:
        if total_gb >= 900:
            score += 55
        elif total_gb >= 450:
            score += 45
        elif total_gb >= 180:
            score += 35
        elif total_gb >= 50:
            score += 10
    if evidence:
        score += 35
    if source.startswith("env:") or source == "current":
        score += 15
    if large_block_mount:
        score += 45
        evidence.append("large-writable-block-mount")
    if mount_info:
        evidence.append(f"mount:{mount_info.mount_point}")
        evidence.append(f"fs:{mount_info.fs_type}")
    if path.as_posix().startswith(PREFERRED_MOUNT_PREFIXES):
        score += 15
    home_path = Path.home()
    if _is_relative_to(path, home_path) and large_block_mount and not os_mount:
        score += 20
        evidence.append("home-on-persistent-block-mount")
    if read_only:
        score -= 80
        warnings.append("Mount is read-only; nvHive needs a user-writable persistent block volume.")
    if network_mount:
        score -= 30
        warnings.append("Network/share filesystem detected; prefer writable persistent block storage for models.")
    if ephemeral_mount:
        score -= 60
        warnings.append("Filesystem looks ephemeral for a cloud desktop.")
    if os_mount:
        score -= 45
        warnings.append("Path appears to live on the OS/root disk; use the persistent block-backed home or data mount.")
    if _looks_ephemeral(path):
        score -= 40
        warnings.append("Path looks ephemeral for a cloud desktop.")
    if total_gb is not None and total_gb < 180 and not source.startswith("env:"):
        warnings.append("Disk is smaller than the expected 200 GB+ persistent model/workspace volume.")
    label = path.name or str(path)
    if fs_type and total_gb is not None:
        label = f"{label} ({fs_type}, {total_gb:g} GB)"
    elif fs_type:
        label = f"{label} ({fs_type})"
    return MountCandidate(
        path=str(path),
        recommended_home=str(_recommended_home(path)),
        label=label,
        source=source,
        exists=exists,
        writable=writable,
        free_gb=free_gb,
        total_gb=total_gb,
        fs_type=fs_type,
        device=device,
        mount_point=mount_point,
        read_only=read_only,
        network_mount=network_mount,
        os_mount=os_mount,
        large_block_mount=large_block_mount,
        score=max(0, score),
        warnings=warnings,
        evidence=evidence,
    )


def _common_roots() -> list[tuple[str, Path]]:
    roots: list[tuple[str, Path]] = []
    for env_name in (
        "NVH_HOME",
        "NVH_MOUNT",
        "PERSISTENT_HOME",
        "PERSISTENT_DIR",
        "WORKSPACE",
        "PROJECTS",
    ):
        value = os.environ.get(env_name)
        if value:
            roots.append((f"env:{env_name}", _expand(value)))
    roots.extend([
        ("common", Path("/mnt")),
        ("common", Path("/media") / os.environ.get("USER", "")),
        ("common", Path("/workspace")),
        ("common", Path("/data")),
        ("common", Path("/persistent")),
        ("common", Path("/storage")),
        ("home", Path.home()),
    ])
    for mount in _mount_table():
        mount_path = mount.mount_point.as_posix()
        if mount_path == "/" or _is_ephemeral_mount(mount):
            continue
        roots.append(("mount", mount.mount_point))
    return roots


def _candidate_paths(extra_roots: list[str | Path] | None = None) -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for source, root in [*_common_roots(), *[("candidate", _expand(path)) for path in extra_roots or []]]:
        root = root.expanduser()
        options = [root]
        if _path_exists(root) and _path_is_dir(root):
            try:
                options.extend(path for path in root.iterdir() if _path_is_dir(path))
            except Exception:
                pass
        for option in options:
            key = _candidate_key(option)
            if key in seen:
                continue
            seen.add(key)
            candidates.append((source, option))
    return candidates


def mount_autopilot_report(
    *,
    min_free_gb: float = DEFAULT_MIN_FREE_GB,
    extra_roots: list[str | Path] | None = None,
    home_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Rank likely persistent mounts and recommend an NVH_HOME."""
    roots = list(extra_roots or [])
    if home_dir is not None:
        roots.append(home_dir)
    current = storage_status(home_dir=home_dir, min_free_gb=min_free_gb).as_dict()
    candidates = [
        _score_candidate(path, source=source, min_free_gb=min_free_gb)
        for source, path in _candidate_paths(roots)
    ]
    candidates.sort(key=lambda item: item.score, reverse=True)
    recommended = candidates[0] if candidates else None
    confidence = "none"
    if recommended:
        if recommended.score >= 75:
            confidence = "high"
        elif recommended.score >= 45:
            confidence = "medium"
        else:
            confidence = "low"
    return {
        "summary": (
            f"Recommended NVH_HOME: {recommended.recommended_home}"
            if recommended
            else "No persistent mount candidates found."
        ),
        "confidence": confidence,
        "current": current,
        "recommended": recommended.as_dict() if recommended else None,
        "candidates": [candidate.as_dict() for candidate in candidates[:8]],
    }


def activate_recommended_mount(
    *,
    min_free_gb: float = DEFAULT_MIN_FREE_GB,
    extra_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    """Create and activate the best discovered NVH_HOME."""
    report = mount_autopilot_report(min_free_gb=min_free_gb, extra_roots=extra_roots)
    recommended = report.get("recommended")
    if not recommended:
        raise RuntimeError("No persistent mount candidate found.")
    status = ensure_storage(recommended["recommended_home"], min_free_gb=min_free_gb)
    return {
        "summary": f"Activated NVH_HOME at {status.layout.home}",
        "storage": status.as_dict(),
        "mount_autopilot": report,
    }
