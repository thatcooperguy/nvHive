"""Tests for nvh.plugins.manager — discover, list, load."""

from __future__ import annotations

from pathlib import Path


class TestPluginManager:
    def test_construct_and_list_empty(self):
        from nvh.plugins.manager import PluginManager

        pm = PluginManager()
        assert pm.list_plugins() == []

    def test_discover_empty_dir(self, tmp_path: Path):
        from nvh.plugins.manager import PluginManager

        pm = PluginManager()
        found = pm.discover(plugin_dir=tmp_path)
        # Only entry-point plugins (if any); no file plugins
        for p in found:
            assert p.source == "entrypoint"

    def test_discover_py_file(self, tmp_path: Path):
        from nvh.plugins.manager import PluginManager

        (tmp_path / "my_plugin.py").write_text("x = 1\n")
        pm = PluginManager()
        found = pm.discover(plugin_dir=tmp_path)
        names = [p.name for p in found]
        assert "my_plugin" in names

    def test_load_unknown_returns_none(self):
        from nvh.plugins.manager import PluginManager

        pm = PluginManager()
        assert pm.load("nonexistent") is None

    def test_load_file_plugin(self, tmp_path: Path):
        from nvh.plugins.manager import PluginManager

        (tmp_path / "simple.py").write_text("VALUE = 42\n")
        pm = PluginManager()
        pm.discover(plugin_dir=tmp_path)
        mod = pm.load("simple")
        assert mod is not None
        assert mod.VALUE == 42


class TestPluginManagerManifest:
    def test_discover_with_empty_dir(self, tmp_path):
        from nvh.plugins.manager import PluginManager
        mgr = PluginManager()
        found = mgr.discover(plugin_dir=tmp_path)
        # Only entry-point plugins (if any); no file plugins
        file_plugins = [p for p in found if p.source == "file"]
        assert len(file_plugins) == 0

    def test_discover_with_plugin_file(self, tmp_path):
        from nvh.plugins.manager import PluginManager
        plugin_file = tmp_path / "my_plugin.py"
        plugin_file.write_text("NVHIVE_PLUGIN = {'type': 'provider', 'name': 'test'}\n")
        mgr = PluginManager()
        found = mgr.discover(plugin_dir=tmp_path)
        file_plugins = [p for p in found if p.source == "file"]
        assert len(file_plugins) == 1
        assert file_plugins[0].name == "my_plugin"

    def test_load_file_plugin(self, tmp_path):
        from nvh.plugins.manager import PluginManager
        plugin_file = tmp_path / "sample.py"
        plugin_file.write_text(
            "class MyProv:\n    pass\n\n"
            "NVHIVE_PLUGIN = {'type': 'provider', 'name': 'sample', 'class': MyProv}\n"
        )
        mgr = PluginManager()
        mgr.discover(plugin_dir=tmp_path)
        loaded = mgr.load("sample")
        assert loaded is not None

    def test_load_unknown_returns_none(self):
        from nvh.plugins.manager import PluginManager
        mgr = PluginManager()
        assert mgr.load("does_not_exist") is None
