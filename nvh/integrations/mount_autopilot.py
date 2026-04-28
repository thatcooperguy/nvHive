"""Persistent mount discovery for rootless cloud desktop sessions."""

from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from nvh.integrations.storage import ensure_storage, storage_status

DEFAULT_MIN_FREE_GB = 20.0


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
    score: int
    warnings: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _expand(path: str | Path) -> Path:
    return Path(os.path.expandvars(str(path))).expanduser()


def _nearest_existing(path: Path) -> Path | None:
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current if current.exists() else None


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
        if (path / marker).exists():
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
    exists = path.exists()
    writable = _is_writable(path)
    free_gb, total_gb = _disk_usage(path)
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
        warnings.append("Path is not writable by this user.")
    if free_gb is not None:
        if free_gb >= min_free_gb:
            score += 25
        else:
            warnings.append(f"Only {free_gb} GB free; recommended minimum is {min_free_gb:.0f} GB.")
    if total_gb is not None and total_gb >= 50:
        score += 10
    if evidence:
        score += 35
    if source.startswith("env:") or source == "current":
        score += 15
    if _looks_ephemeral(path):
        score -= 40
        warnings.append("Path looks ephemeral for a cloud desktop.")
    if str(path).startswith(str(Path.home())):
        score -= 5
    return MountCandidate(
        path=str(path),
        recommended_home=str(_recommended_home(path)),
        label=path.name or str(path),
        source=source,
        exists=exists,
        writable=writable,
        free_gb=free_gb,
        total_gb=total_gb,
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
    return roots


def _candidate_paths(extra_roots: list[str | Path] | None = None) -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for source, root in [*_common_roots(), *[("candidate", _expand(path)) for path in extra_roots or []]]:
        root = root.expanduser()
        options = [root]
        if root.exists() and root.is_dir():
            try:
                options.extend(path for path in root.iterdir() if path.is_dir())
            except Exception:
                pass
        for option in options:
            key = str(option.resolve() if option.exists() else option)
            if key in seen:
                continue
            seen.add(key)
            candidates.append((source, option))
    return candidates


def mount_autopilot_report(
    *,
    min_free_gb: float = DEFAULT_MIN_FREE_GB,
    extra_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    """Rank likely persistent mounts and recommend an NVH_HOME."""
    current = storage_status(min_free_gb=min_free_gb).as_dict()
    candidates = [
        _score_candidate(path, source=source, min_free_gb=min_free_gb)
        for source, path in _candidate_paths(extra_roots)
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
