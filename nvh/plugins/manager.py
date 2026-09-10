"""NVHive Plugin System — extend with custom providers and agents.

Plugins are Python packages that register themselves via entry points
or manifest files in the one plugins directory, ``$NVH_HOME/plugins``
(:func:`plugins_dir` — the rootless layout's ``plugins_dir`` when the storage
layout declares one, else ``NVH_HOME/plugins``). Nothing is read from
``~/.hive`` any more.

Plugin types:
- provider: Custom LLM provider (implements Provider protocol)
- agent: Custom agent persona template
- cabinet: Custom agent cabinet (group of agents)

Creating a plugin:
    1. Create a Python file in $NVH_HOME/plugins/
    2. Define a class that implements Provider protocol
    3. Add a manifest dict:
       NVHIVE_PLUGIN = {
           "type": "provider",
           "name": "my_provider",
           "class": MyProvider,
       }

Or via pip packages with entry points (the group is declared in pyproject.toml):
    [project.entry-points."nvhive.plugins"]
    my_provider = "my_package:MyProvider"

The same directory also hosts Wizard tool plugins (a ``.py`` file with a
top-level ``register(reg)``; see nvh/integrations/wizard/tools.py) — one
path for everything a user drops in, and ONE walk over it: both loaders use
:func:`plugin_files` for the listing, :func:`declares_top_level` to tell a
provider manifest from a Wizard tool *without executing the file*, and
:func:`load_plugin_module` to execute it. A provider plugin is never run by
the Wizard's registry build, and a Wizard tool file is never run by
``nvh plugins``.
"""

import ast
import importlib
import importlib.metadata
import importlib.util
import logging
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

logger = logging.getLogger(__name__)

#: The entry-point group pip-installed plugins advertise themselves under.
ENTRY_POINT_GROUP = "nvhive.plugins"
#: The top-level name a provider / agent / cabinet plugin file binds.
MANIFEST_NAME = "NVHIVE_PLUGIN"
#: The top-level callable a Wizard tool plugin file defines.
WIZARD_REGISTER_NAME = "register"


def plugins_dir(home_dir: str | Path | None = None) -> Path:
    """The one plugins directory: ``storage_layout().plugins_dir`` (``$NVH_PLUGINS``, else ``$NVH_HOME/plugins``)."""
    from nvh.integrations.workspace.storage import storage_layout

    return storage_layout(home_dir).plugins_dir


def plugin_files(plugin_dir: Path | None = None) -> list[Path]:
    """The ``.py`` files of one plugins directory in load order.

    Sorted by name; ``_``-prefixed files are private helpers and skipped (the
    Python-package convention); a missing directory is an empty list. This is
    the single walk both :class:`PluginManager` and the Wizard registry build
    perform.
    """
    directory = plugins_dir() if plugin_dir is None else Path(plugin_dir)
    if not directory.is_dir():
        return []
    return [path for path in sorted(directory.glob("*.py")) if not path.name.startswith("_")]


def _top_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def declares_top_level(path: Path, *names: str) -> bool:
    """Does the module at ``path`` bind any of ``names`` at its top level?

    A cheap pre-exec probe (``ast.parse``, nothing runs): a ``def`` / ``async
    def`` / ``class``, a plain assignment or a ``from x import name`` counts.
    Raises :class:`SyntaxError` for a file that does not parse, so a loader can
    report a broken plugin instead of silently ignoring it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return bool(_top_level_names(tree) & set(names))


def load_plugin_module(path: Path, module_name: str | None = None) -> ModuleType:
    """Execute the plugin file at ``path`` and return the module (raises on failure)."""
    spec = importlib.util.spec_from_file_location(module_name or path.stem, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load plugin {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entry_points() -> list[Any]:
    eps = importlib.metadata.entry_points()
    if hasattr(eps, "select"):
        return list(eps.select(group=ENTRY_POINT_GROUP))
    return list(eps.get(ENTRY_POINT_GROUP, []))  # pragma: no cover — pre-3.10 shape


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
        """Discover plugins from entry points and the plugins directory (:func:`plugins_dir` by default)."""
        found = []

        # 1. Entry points (pip-installed plugins)
        try:
            for ep in _entry_points():
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

        # 2. The plugins directory ($NVH_HOME/plugins) — the one walk.
        try:
            files = plugin_files(plugin_dir)
        except Exception as e:
            logger.debug(f"plugins directory unavailable: {e}")
            return found

        for py_file in files:
            info = PluginInfo(
                name=py_file.stem,
                type="unknown",
                source="file",
                module=str(py_file),
            )
            self._plugins[info.name] = info
            found.append(info)

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
                for entry in _entry_points():
                    if entry.name == name:
                        obj = entry.load()
                        self._loaded[name] = obj
                        return obj

            elif info.source == "file":
                path = Path(info.module)
                if declares_top_level(path, WIZARD_REGISTER_NAME) and not declares_top_level(path, MANIFEST_NAME):
                    # A Wizard tool plugin: the Wizard registry build runs it, not us.
                    info.type = "wizard-tool"
                    return None
                module = load_plugin_module(path, name)
                # Look for the NVHIVE_PLUGIN manifest
                manifest = getattr(module, MANIFEST_NAME, None)
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
