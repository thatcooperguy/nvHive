"""Tests for nvh.plugins.manager — discover, list, load, the one plugins directory."""

from __future__ import annotations

from pathlib import Path


class TestPluginsDir:
    """One discovery path: ``storage_layout().plugins_dir`` if declared, else ``NVH_HOME/plugins``."""

    def test_plugins_dir_follows_nvh_home(self, tmp_path: Path, monkeypatch):
        from nvh.integrations.workspace.storage import storage_layout
        from nvh.plugins.manager import plugins_dir

        monkeypatch.setenv("NVH_HOME", str(tmp_path))
        expected = getattr(storage_layout(), "plugins_dir", None) or (tmp_path.resolve() / "plugins")
        assert plugins_dir() == Path(expected)
        assert ".hive" not in plugins_dir().parts
        assert plugins_dir(tmp_path / "other") == Path(getattr(storage_layout(tmp_path / "other"), "plugins_dir", None) or (tmp_path / "other").resolve() / "plugins")

    def test_discover_defaults_to_the_plugins_dir(self, tmp_path: Path, monkeypatch):
        from nvh.plugins.manager import PluginManager, plugins_dir

        monkeypatch.setenv("NVH_HOME", str(tmp_path))
        target = plugins_dir()
        target.mkdir(parents=True)
        (target / "from_home.py").write_text("NVHIVE_PLUGIN = {'type': 'agent', 'name': 'from_home'}\n")
        (target / "_private.py").write_text("raise RuntimeError('never')\n")
        pm = PluginManager()
        found = {p.name: p for p in pm.discover() if p.source == "file"}
        assert set(found) == {"from_home"}
        assert found["from_home"].module == str(target / "from_home.py")
        assert pm.load("from_home") is not None and pm.list_plugins()[-1].type == "agent"

    def test_no_hive_dot_dir_is_read(self):
        source = Path(__import__("nvh.plugins.manager", fromlist=["x"]).__file__).read_text(encoding="utf-8")
        assert '".hive"' not in source

    def test_one_walk_serves_both_loaders_and_neither_executes_the_others_files(self, tmp_path: Path):
        """``plugin_files`` is the single listing; ``declares_top_level`` tells a
        provider manifest from a Wizard tool without running the file; a Wizard
        tool file is the Wizard registry's to execute, not ``nvh plugins``'."""
        import pytest

        from nvh.plugins.manager import (
            PluginManager,
            declares_top_level,
            load_plugin_module,
            plugin_files,
        )

        sentinel = tmp_path / "wizard-tool-was-imported"
        (tmp_path / "b.py").write_text("NVHIVE_PLUGIN = {'type': 'agent', 'name': 'b'}\n")
        (tmp_path / "a.py").write_text(
            f"import pathlib\npathlib.Path({str(sentinel)!r}).write_text('x')\n\ndef register(reg):\n    reg.seen = True\n"
        )
        (tmp_path / "_private.py").write_text("raise RuntimeError('never')\n")
        (tmp_path / "bad.py").write_text("this is not python ::\n")

        assert [p.name for p in plugin_files(tmp_path)] == ["a.py", "b.py", "bad.py"]
        assert plugin_files(tmp_path / "missing") == []
        assert declares_top_level(tmp_path / "a.py", "register")
        assert not declares_top_level(tmp_path / "a.py", "NVHIVE_PLUGIN")
        assert declares_top_level(tmp_path / "b.py", "NVHIVE_PLUGIN")
        with pytest.raises(SyntaxError):
            declares_top_level(tmp_path / "bad.py", "register")
        assert not sentinel.exists()  # probing never runs the file

        pm = PluginManager()
        pm.discover(plugin_dir=tmp_path)
        assert pm.load("a") is None and pm.list_plugins()[0].type == "wizard-tool"
        assert not sentinel.exists()  # nvh plugins does not run Wizard tool files
        assert pm.load("b") is not None
        assert load_plugin_module(tmp_path / "b.py").NVHIVE_PLUGIN["name"] == "b"
        with pytest.raises(SyntaxError):
            load_plugin_module(tmp_path / "bad.py")


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
