"""Rootless runtime fallback helpers for locked-down Linux desktops.

The primary nvHive path uses the Python interpreter that launched nvHive plus
standard ``venv`` and ``pip`` environments.  Micromamba is treated as a fallback
runtime that can be installed under ``NVH_HOME`` when a cloud image lacks usable
Python virtualenv support.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import sys
import tarfile
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from nvh.integrations.storage import nvh_home, storage_layout

MICROMAMBA_BASE_URL = "https://micro.mamba.pm/api/micromamba"


@dataclass(frozen=True)
class RuntimeStatus:
    """Status for the active rootless Python/runtime toolchain."""

    python_executable: str
    python_version: str
    venv_available: bool
    pip_available: bool
    strategy: str
    micromamba_installed: bool
    micromamba_binary: str
    micromamba_root_prefix: str
    notes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def micromamba_root() -> Path:
    """Return the persistent micromamba root prefix."""
    home, _ = nvh_home()
    return storage_layout(home).runtime_dir / "micromamba"


def micromamba_binary() -> Path:
    """Return the managed micromamba binary path."""
    home, _ = nvh_home()
    return storage_layout(home).bin_dir / "micromamba"


def micromamba_subdir() -> str:
    """Return the micromamba platform subdir for this machine."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux":
        if machine in {"x86_64", "amd64"}:
            return "linux-64"
        if machine in {"aarch64", "arm64"}:
            return "linux-aarch64"
    if system == "darwin":
        if machine in {"x86_64", "amd64"}:
            return "osx-64"
        if machine in {"aarch64", "arm64"}:
            return "osx-arm64"
    raise RuntimeError(f"Unsupported micromamba platform: {platform.system()} {platform.machine()}")


def _module_available(module: str) -> bool:
    try:
        __import__(module)
        return True
    except Exception:
        return False


def runtime_status() -> RuntimeStatus:
    """Detect whether the default rootless Python strategy is ready."""
    notes: list[str] = []
    venv_available = _module_available("venv")
    pip_available = _module_available("pip")
    mamba = micromamba_binary()
    installed = mamba.exists() and os.access(mamba, os.X_OK)

    if venv_available and pip_available:
        strategy = "python-venv"
        notes.append("Default path is ready: nvHive can use Python venv and pip without conda.")
    elif installed:
        strategy = "micromamba-fallback"
        notes.append("Python venv/pip is incomplete; use the managed micromamba fallback.")
    else:
        strategy = "needs-runtime"
        notes.append(
            "Python venv/pip is incomplete; install the rootless micromamba fallback pack."
        )

    return RuntimeStatus(
        python_executable=sys.executable,
        python_version=platform.python_version(),
        venv_available=venv_available,
        pip_available=pip_available,
        strategy=strategy,
        micromamba_installed=installed,
        micromamba_binary=str(mamba),
        micromamba_root_prefix=str(micromamba_root()),
        notes=notes,
    )


def _extract_micromamba(archive: Path, binary: Path) -> None:
    """Extract only bin/micromamba from the downloaded tarball."""
    binary.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, mode="r:bz2") as tar:
        member = next(
            (
                item for item in tar.getmembers()
                if item.name.endswith("bin/micromamba") and item.isfile()
            ),
            None,
        )
        if member is None:
            raise RuntimeError("Downloaded archive did not contain bin/micromamba")
        source = tar.extractfile(member)
        if source is None:
            raise RuntimeError("Could not read micromamba binary from archive")
        with binary.open("wb") as target:
            shutil.copyfileobj(source, target)
    binary.chmod(0o755)


def _write_micromamba_launcher() -> Path:
    """Write a stable wrapper that pins micromamba to NVH_HOME."""
    layout = storage_layout()
    launcher = layout.bin_dir / "nvhive-micromamba"
    launcher.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail

export NVH_HOME="${{NVH_HOME:-{layout.home}}}"
export MAMBA_ROOT_PREFIX="${{MAMBA_ROOT_PREFIX:-{micromamba_root()}}}"
export MAMBA_NO_BANNER=1
mkdir -p "$MAMBA_ROOT_PREFIX"
exec "{micromamba_binary()}" "$@"
""",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher


async def install_micromamba(*, force_update: bool = False) -> AsyncIterator[dict[str, Any]]:
    """Install micromamba under NVH_HOME and stream progress events."""
    layout = storage_layout()
    binary = micromamba_binary()
    root_prefix = micromamba_root()

    if binary.exists() and not force_update:
        _write_micromamba_launcher()
        yield {
            "event": "step",
            "status": "complete",
            "message": "Rootless micromamba already installed",
            "binary": str(binary),
        }
        return

    subdir = micromamba_subdir()
    url = f"{MICROMAMBA_BASE_URL}/{subdir}/latest"
    download_dir = layout.cache_dir / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    root_prefix.mkdir(parents=True, exist_ok=True)
    archive = download_dir / f"micromamba-{subdir}.tar.bz2"

    yield {
        "event": "plan",
        "status": "running",
        "message": f"Installing rootless micromamba for {subdir}",
        "url": url,
        "target": str(binary),
    }

    try:
        import httpx

        async with httpx.AsyncClient(follow_redirects=True, timeout=300) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length", "0") or "0")
                downloaded = 0
                last_emit = time.monotonic()
                with archive.open("wb") as handle:
                    async for chunk in response.aiter_bytes(chunk_size=1024 * 256):
                        handle.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        if total and now - last_emit > 0.75:
                            yield {
                                "event": "download",
                                "status": "running",
                                "message": f"Downloaded {downloaded / 1024 / 1024:.1f} MB",
                                "progress": min(80, int(downloaded / total * 80)),
                            }
                            last_emit = now

        yield {"event": "step", "status": "running", "message": "Extracting micromamba"}
        await asyncio.to_thread(_extract_micromamba, archive, binary)
        launcher = _write_micromamba_launcher()
    except Exception as exc:
        yield {
            "event": "error",
            "status": "failed",
            "message": f"Micromamba fallback install failed: {exc}",
        }
        return
    finally:
        archive.unlink(missing_ok=True)

    marker = root_prefix / "nvh-runtime.json"
    marker.write_text(
        (
            "{\n"
            f'  "installed_at": "{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}",\n'
            f'  "binary": "{binary}",\n'
            f'  "launcher": "{launcher}",\n'
            f'  "subdir": "{subdir}"\n'
            "}\n"
        ),
        encoding="utf-8",
    )
    yield {
        "event": "complete",
        "status": "complete",
        "message": "Rootless micromamba fallback installed",
        "binary": str(binary),
        "launcher": str(launcher),
        "status_snapshot": runtime_status().as_dict(),
    }
