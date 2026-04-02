"""NVHive Plugin System — extend with custom providers and agents.

Plugins are Python packages that register themselves via entry points
or manifest files in ~/.hive/plugins/.

Plugin types:
- provider: Custom LLM provider (implements Provider protocol)
- agent: Custom agent persona template
- cabinet: Custom agent cabinet (group of agents)

Creating a plugin:
    1. Create a Python file in ~/.hive/plugins/
    2. Define a class that implements Provider protocol
    3. Add a manifest dict:
       NVHIVE_PLUGIN = {
           "type": "provider",
           "name": "my_provider",
           "class": MyProvider,
       }

Or via pip packages with entry points:
    [project.entry-points."nvhive.plugins"]
    my_provider = "my_package:MyProvider"
"""

import importlib
import importlib.metadata
import importlib.util
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PluginInfo:
    name: str
    type: str           # "provider", "agent", "cabinet"
    source: str         # "file", "entrypoint", "builtin"
    module: str         # module path
    enabled: bool = True
    error: str = ""


class PluginManager:
    """Discovers and manages NVHive plugins."""

    def __init__(self):
        self._plugins: dict[str, PluginInfo] = {}
        self._loaded: dict[str, Any] = {}

    def discover(self, plugin_dir: Path | None = None) -> list[PluginInfo]:
        """Discover plugins from entry points and plugin directory."""
        found = []

        # 1. Entry points (pip-installed plugins)
        try:
            eps = importlib.metadata.entry_points()
            nvhive_eps = eps.select(group="nvhive.plugins") if hasattr(eps, "select") else eps.get("nvhive.plugins", [])
            for ep in nvhive_eps:
                info = PluginInfo(
                    name=ep.name,
                    type="provider",
                    source="entrypoint",
                    module=str(ep.value),
                )
                self._plugins[ep.name] = info
                found.append(info)
        except Exception as e:
            logger.debug(f"Entry point discovery failed: {e}")

        # 2. Plugin directory (~/.hive/plugins/)
        if plugin_dir is None:
            plugin_dir = Path.home() / ".hive" / "plugins"

        if plugin_dir.is_dir():
            for py_file in plugin_dir.glob("*.py"):
                if py_file.name.startswith("_"):
                    continue
                try:
                    name = py_file.stem
                    info = PluginInfo(
                        name=name,
                        type="unknown",
                        source="file",
                        module=str(py_file),
                    )
                    self._plugins[name] = info
                    found.append(info)
                except Exception as e:
                    logger.warning(f"Failed to discover plugin {py_file}: {e}")

        return found

    def load(self, name: str) -> Any | None:
        """Load a specific plugin by name."""
        if name in self._loaded:
            return self._loaded[name]

        info = self._plugins.get(name)
        if not info:
            return None

        try:
            if info.source == "entrypoint":
                ep = importlib.metadata.entry_points()
                nvhive_eps = ep.select(group="nvhive.plugins") if hasattr(ep, "select") else ep.get("nvhive.plugins", [])
                for entry in nvhive_eps:
                    if entry.name == name:
                        obj = entry.load()
                        self._loaded[name] = obj
                        return obj

            elif info.source == "file":
                spec = importlib.util.spec_from_file_location(name, info.module)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    # Look for NVHIVE_PLUGIN manifest
                    manifest = getattr(module, "NVHIVE_PLUGIN", None)
                    if manifest:
                        info.type = manifest.get("type", "provider")
                        obj = manifest.get("class")
                        if obj:
                            self._loaded[name] = obj
                            return obj
                    self._loaded[name] = module
                    return module

        except Exception as e:
            info.error = str(e)
            logger.warning(f"Failed to load plugin '{name}': {e}")

        return None

    def load_all(self) -> dict[str, Any]:
        """Load all discovered plugins."""
        for name in self._plugins:
            self.load(name)
        return self._loaded

    def list_plugins(self) -> list[PluginInfo]:
        """List all discovered plugins."""
        return list(self._plugins.values())

    def get_providers(self) -> dict[str, Any]:
        """Get all loaded provider plugins."""
        return {
            name: obj for name, obj in self._loaded.items()
            if self._plugins.get(name, PluginInfo(name="", type="", source="", module="")).type == "provider"
        }
