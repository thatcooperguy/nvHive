"""Rootless persistent storage helpers for cloud GPU desktop sessions.

The target deployment is a Linux desktop session where the operating system is
ephemeral, but a user-owned file mount persists across sessions.  ``NVH_HOME``
is the one directory students and admins should point at that mount.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_MIN_FREE_GB = 20.0
NVH_HOME_ENV = "NVH_HOME"


@dataclass(frozen=True)
class StorageLayout:
    """Canonical rootless nvHive filesystem layout."""

    home: Path
    bin_dir: Path
    models_dir: Path
    ollama_models_dir: Path
    cache_dir: Path
    logs_dir: Path
    tmp_dir: Path
    runtime_dir: Path
    apps_dir: Path
    webui_dir: Path
    studio_dir: Path
    comfyui_dir: Path
    config_dir: Path

    def env(self) -> dict[str, str]:
        """Environment variables that make this layout active."""
        return {
            "NVH_HOME": str(self.home),
            "NVH_BIN": str(self.bin_dir),
            "NVH_MODELS": str(self.models_dir),
            "NVH_CACHE": str(self.cache_dir),
            "NVH_LOGS": str(self.logs_dir),
            "NVH_RUNTIME_HOME": str(self.runtime_dir),
            "NVH_APPS_HOME": str(self.apps_dir),
            "NVH_WEB_HOME": str(self.webui_dir),
            "NVH_STUDIO_HOME": str(self.studio_dir),
            "COMFYUI_HOME": str(self.comfyui_dir),
            "OLLAMA_MODELS": str(self.ollama_models_dir),
            "HIVE_CONFIG_HOME": str(self.config_dir),
            "XDG_CACHE_HOME": str(self.cache_dir / "xdg"),
            "PIP_CACHE_DIR": str(self.cache_dir / "pip"),
            "UV_CACHE_DIR": str(self.cache_dir / "uv"),
            "HF_HOME": str(self.cache_dir / "huggingface"),
            "HUGGINGFACE_HUB_CACHE": str(self.cache_dir / "huggingface" / "hub"),
            "TORCH_HOME": str(self.cache_dir / "torch"),
            "TMPDIR": str(self.tmp_dir),
            "TEMP": str(self.tmp_dir),
            "TMP": str(self.tmp_dir),
        }

    def export_lines(self) -> list[str]:
        lines = [f'export {key}="{value}"' for key, value in self.env().items()]
        lines.append('export PATH="$NVH_BIN:$PATH"')
        return lines


@dataclass(frozen=True)
class StorageStatus:
    """Preflight status for the selected rootless storage home."""

    layout: StorageLayout
    configured_by: str
    exists: bool
    writable: bool
    free_gb: float | None
    total_gb: float | None
    min_free_gb: float
    ok: bool
    warnings: list[str]
    env_file: Path

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["layout"] = {key: str(value) for key, value in asdict(self.layout).items()}
        data["env_file"] = str(self.env_file)
        data["export_lines"] = self.layout.export_lines()
        return data


def _expand_path(path: str | Path) -> Path:
    expanded = os.path.expandvars(str(path))
    return Path(expanded).expanduser().resolve()


def nvh_home(home_dir: str | Path | None = None) -> tuple[Path, str]:
    """Return the active NVH home and how it was selected."""
    if home_dir:
        return _expand_path(home_dir), "argument"
    env_home = os.environ.get(NVH_HOME_ENV)
    if env_home:
        return _expand_path(env_home), "env:NVH_HOME"
    return Path.home() / ".nvh", "default"


def storage_layout(home_dir: str | Path | None = None) -> StorageLayout:
    """Return the canonical directory layout for the active rootless home."""
    home, _ = nvh_home(home_dir)
    use_component_env = home_dir is None
    bin_dir = _expand_path(os.environ.get("NVH_BIN", home / "bin") if use_component_env else home / "bin")
    models_dir = _expand_path(
        os.environ.get("NVH_MODELS", home / "models") if use_component_env else home / "models"
    )
    cache_dir = _expand_path(
        os.environ.get("NVH_CACHE", home / "cache") if use_component_env else home / "cache"
    )
    logs_dir = _expand_path(
        os.environ.get("NVH_LOGS", home / "logs") if use_component_env else home / "logs"
    )
    runtime_dir = _expand_path(
        os.environ.get("NVH_RUNTIME_HOME", home / "runtimes")
        if use_component_env
        else home / "runtimes"
    )
    apps_dir = _expand_path(
        os.environ.get("NVH_APPS_HOME", home / "apps") if use_component_env else home / "apps"
    )
    webui_dir = _expand_path(
        os.environ.get("NVH_WEB_HOME", home / "webui") if use_component_env else home / "webui"
    )
    studio_dir = _expand_path(
        os.environ.get("NVH_STUDIO_HOME", home / "studio") if use_component_env else home / "studio"
    )
    comfyui_dir = _expand_path(
        os.environ.get("COMFYUI_HOME", home / "comfyui") if use_component_env else home / "comfyui"
    )
    config_dir = _expand_path(
        os.environ.get("HIVE_CONFIG_HOME", home / "config") if use_component_env else home / "config"
    )
    ollama_models_dir = _expand_path(
        os.environ.get("OLLAMA_MODELS", models_dir / "ollama")
        if use_component_env
        else models_dir / "ollama"
    )
    return StorageLayout(
        home=home,
        bin_dir=bin_dir,
        models_dir=models_dir,
        ollama_models_dir=ollama_models_dir,
        cache_dir=cache_dir,
        logs_dir=logs_dir,
        tmp_dir=cache_dir / "tmp",
        runtime_dir=runtime_dir,
        apps_dir=apps_dir,
        webui_dir=webui_dir,
        studio_dir=studio_dir,
        comfyui_dir=comfyui_dir,
        config_dir=config_dir,
    )


def _nearest_existing_parent(path: Path) -> Path:
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def _disk_usage_gb(path: Path) -> tuple[float | None, float | None]:
    try:
        usage = shutil.disk_usage(path)
    except Exception:
        return None, None
    gb = 1024 ** 3
    return round(usage.free / gb, 1), round(usage.total / gb, 1)


def _looks_ephemeral(path: Path) -> bool:
    parts = {part.lower() for part in path.parts}
    return bool(parts.intersection({"tmp", "temp", "run", "var", "cache"}))


def write_env_file(layout: StorageLayout) -> Path:
    """Write a source-able shell file inside the persistent home."""
    env_file = layout.home / "nvh-env.sh"
    env_file.write_text("\n".join(layout.export_lines()) + "\n", encoding="utf-8")
    try:
        env_file.chmod(0o600)
    except Exception:
        pass
    return env_file


def activate_storage(layout: StorageLayout) -> None:
    """Activate the layout for the current process."""
    os.environ.update(layout.env())
    for module_name in ("nvh.config.settings", "nvh.cli.setup", "nvh.cli.main"):
        module = sys.modules.get(module_name)
        if module is None:
            continue
        if hasattr(module, "DEFAULT_CONFIG_DIR"):
            setattr(module, "DEFAULT_CONFIG_DIR", layout.config_dir)
        if hasattr(module, "DEFAULT_CONFIG_PATH"):
            setattr(module, "DEFAULT_CONFIG_PATH", layout.config_dir / "config.yaml")
    current_path = os.environ.get("PATH", "")
    bin_dir = str(layout.bin_dir)
    if bin_dir not in current_path.split(os.pathsep):
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{current_path}"


def ensure_storage(
    home_dir: str | Path | None = None,
    *,
    min_free_gb: float = DEFAULT_MIN_FREE_GB,
    activate: bool = True,
) -> StorageStatus:
    """Create and activate the rootless storage layout."""
    layout = storage_layout(home_dir)
    for path in [
        layout.home,
        layout.bin_dir,
        layout.models_dir,
        layout.ollama_models_dir,
        layout.cache_dir,
        layout.logs_dir,
        layout.tmp_dir,
        layout.runtime_dir,
        layout.apps_dir,
        layout.webui_dir,
        layout.studio_dir,
        layout.comfyui_dir,
        layout.config_dir,
    ]:
        path.mkdir(parents=True, exist_ok=True)
    for value in layout.env().values():
        Path(value).mkdir(parents=True, exist_ok=True)
    env_file = write_env_file(layout)
    if activate:
        activate_storage(layout)
    return storage_status(home_dir=layout.home, min_free_gb=min_free_gb)


def storage_status(
    home_dir: str | Path | None = None,
    *,
    min_free_gb: float = DEFAULT_MIN_FREE_GB,
) -> StorageStatus:
    """Return status for the active or requested rootless storage home."""
    home, configured_by = nvh_home(home_dir)
    layout = storage_layout(home)
    exists = home.exists()
    probe = home if exists else _nearest_existing_parent(home)
    free_gb, total_gb = _disk_usage_gb(probe)
    writable = False
    warnings: list[str] = []

    try:
        parent = home if exists else probe
        writable = os.access(parent, os.W_OK)
    except Exception:
        writable = False

    if configured_by == "default":
        warnings.append(
            "NVH_HOME is not set. On ephemeral cloud desktops, point it at the mounted persistent volume."
        )
    if _looks_ephemeral(home):
        warnings.append("Selected storage path looks ephemeral; use a mounted persistent directory instead.")
    if free_gb is not None and free_gb < min_free_gb:
        warnings.append(f"Only {free_gb} GB free; recommended minimum is {min_free_gb:.0f} GB.")
    if not writable:
        warnings.append("Selected storage path is not writable by this user.")

    ok = writable and (free_gb is None or free_gb >= min_free_gb)
    return StorageStatus(
        layout=layout,
        configured_by=configured_by,
        exists=exists,
        writable=writable,
        free_gb=free_gb,
        total_gb=total_gb,
        min_free_gb=min_free_gb,
        ok=ok,
        warnings=warnings,
        env_file=layout.home / "nvh-env.sh",
    )
