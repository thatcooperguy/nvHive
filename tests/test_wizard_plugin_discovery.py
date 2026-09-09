"""Tests for Wizard tool discovery (Tier 6 — plugin entry-points + workspace dir)."""

from __future__ import annotations

from pathlib import Path


def test_workspace_plugin_dir_registers_tool(tmp_path: Path, monkeypatch) -> None:
    """A .py file with a top-level register(reg) callable is loaded on
    default_registry() build, and its tool appears in list_tools()."""
    from nvh.integrations.wizard.tools import default_registry

    plugin_dir = tmp_path / "wizard-tools"
    plugin_dir.mkdir()
    (plugin_dir / "my_plugin.py").write_text(
        """
from nvh.integrations.wizard.tools import WizardTool


async def _noop_handler(args):
    return {"ok": True, "summary": "noop"}


def register(reg):
    reg.register(WizardTool(
        name="my_local_plugin",
        description="A workspace plugin loaded from disk.",
        safety_class="auto",
        parameters={},
        handler=_noop_handler,
    ))
""",
    )
    monkeypatch.setenv("NVH_WIZARD_PLUGIN_DIR", str(plugin_dir))

    registry = default_registry()
    names = {t.name for t in registry.list_tools()}
    assert "my_local_plugin" in names


def test_workspace_plugin_dir_skips_private_files(tmp_path: Path, monkeypatch) -> None:
    """Files starting with _ are ignored — same convention as Python packages."""
    from nvh.integrations.wizard.tools import default_registry

    plugin_dir = tmp_path / "wizard-tools"
    plugin_dir.mkdir()
    (plugin_dir / "_helper.py").write_text(
        "def register(reg): raise RuntimeError('should not load')\n",
    )
    monkeypatch.setenv("NVH_WIZARD_PLUGIN_DIR", str(plugin_dir))

    # No raise = the underscore-prefixed file was skipped.
    registry = default_registry()
    assert registry is not None


def test_workspace_plugin_dir_swallows_broken_plugin(tmp_path: Path, monkeypatch) -> None:
    """A broken plugin must log and skip — never break the rest of the registry."""
    from nvh.integrations.wizard.tools import default_registry

    plugin_dir = tmp_path / "wizard-tools"
    plugin_dir.mkdir()
    (plugin_dir / "broken.py").write_text("this is not valid python ::\n")
    monkeypatch.setenv("NVH_WIZARD_PLUGIN_DIR", str(plugin_dir))

    registry = default_registry()
    # Stock tools still present.
    assert registry.get("refresh_models") is not None


def test_workspace_plugin_dir_missing_is_noop(tmp_path: Path, monkeypatch) -> None:
    """Missing plugin dir → registry build returns the stock tools without raising."""
    from nvh.integrations.wizard.tools import default_registry

    monkeypatch.setenv("NVH_WIZARD_PLUGIN_DIR", str(tmp_path / "does-not-exist"))
    registry = default_registry()
    assert registry.get("refresh_models") is not None


_PLUGIN_SOURCE = """
from nvh.integrations.wizard.tools import WizardTool


async def _noop(args):
    return {"ok": True}


def register(reg):
    reg.register(WizardTool(name="%s", description="d", safety_class="auto", parameters={}, handler=_noop))
"""


def test_default_plugin_dir_is_the_one_plugins_directory_under_nvh_home(tmp_path: Path, monkeypatch) -> None:
    """Without the override, Wizard tools load from ``nvh.plugins.manager.plugins_dir()``
    (``$NVH_HOME/plugins``, shared with provider plugins) — and, for one more
    release, from the pre-0.44 ``$NVH_HOME/wizard-tools``. A file without a
    top-level ``register`` (a provider plugin) is never *executed* by the
    Wizard build, not merely left unregistered."""
    from nvh.integrations.wizard.tools import default_registry
    from nvh.plugins.manager import plugins_dir

    monkeypatch.setenv("NVH_HOME", str(tmp_path))
    monkeypatch.delenv("NVH_WIZARD_PLUGIN_DIR", raising=False)
    primary = plugins_dir()
    primary.mkdir(parents=True)
    (primary / "one.py").write_text(_PLUGIN_SOURCE % "plugin_from_plugins_dir")
    sentinel = tmp_path / "provider-plugin-was-imported"
    (primary / "provider_plugin.py").write_text(
        "import pathlib\n"
        f"pathlib.Path({str(sentinel)!r}).write_text('imported')\n"
        "class P:\n    pass\n\nNVHIVE_PLUGIN = {'type': 'provider', 'name': 'p', 'class': P}\n"
    )
    legacy = primary.parent / "wizard-tools"
    legacy.mkdir(parents=True)
    (legacy / "old.py").write_text(_PLUGIN_SOURCE % "plugin_from_legacy_dir")

    names = {t.name for t in default_registry().list_tools()}
    assert {"plugin_from_plugins_dir", "plugin_from_legacy_dir"} <= names
    assert "refresh_models" in names
    assert not sentinel.exists(), "a provider plugin's module body ran inside the Wizard registry build"


def test_plugin_tool_without_a_safety_class_is_refused_not_registered_as_auto(tmp_path: Path, monkeypatch, caplog) -> None:
    """Pre-0.44 ``WizardTool`` required ``safety_class``; the 0.44 subclass keeps
    that contract so a plugin that forgets it fails to load (logged, skipped)
    instead of becoming a no-click ``auto`` tool."""
    import logging

    import pytest

    from nvh.integrations.wizard.tools import WizardTool, default_registry

    with pytest.raises(TypeError, match="safety_class"):
        WizardTool(name="purge_models", description="d", parameters={}, handler=None)
    # ``safe=`` (the older spelling) is an explicit choice too.
    assert WizardTool(name="x", description="d", parameters={}, handler=None, safe=False).safety_class == "confirm"

    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "cleanup.py").write_text(
        "from nvh.integrations.wizard.tools import WizardTool\n\n"
        "async def _purge(args):\n    return {'ok': True}\n\n"
        "def register(reg):\n"
        "    reg.register(WizardTool(name='purge_models', description='d', parameters={}, handler=_purge))\n",
    )
    monkeypatch.setenv("NVH_WIZARD_PLUGIN_DIR", str(plugin_dir))
    with caplog.at_level(logging.WARNING, logger="nvh.integrations.wizard.tools"):
        registry = default_registry()
    assert registry.get("purge_models") is None
    assert any("cleanup.py failed" in record.getMessage() for record in caplog.records)
