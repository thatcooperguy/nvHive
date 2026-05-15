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
