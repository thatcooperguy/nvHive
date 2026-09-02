"""Pytest smoke tests — fast, offline subset of `nvh test --imports`.

Run via: python -m pytest tests/test_smoke_quick.py -v --timeout=30
"""

from __future__ import annotations

import importlib

import pytest

from nvh.integrations.diagnostics.smoke_tests import (
    CORE_IMPORT_PROBES,
    import_probe,
    smoke_test_report,
)

# ---------------------------------------------------------------------------
# 1. Core feature imports — the same list `nvh test --imports` probes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mod_path,symbol", CORE_IMPORT_PROBES,
                         ids=[f"{m}.{s}" for m, s in CORE_IMPORT_PROBES])
def test_core_feature_import(mod_path: str, symbol: str):
    """Every core feature module must import and expose its key symbol."""
    mod = importlib.import_module(mod_path)
    assert hasattr(mod, symbol), f"{symbol} not found in {mod_path}"


def test_import_probe_passes_on_healthy_tree():
    tests = import_probe()
    assert tests[0].id == "core-imports"
    assert tests[0].status == "pass", [t.summary for t in tests[1:]]
    assert len(tests) == 1


def test_import_probe_reports_each_broken_module():
    tests = import_probe((
        ("nvh.core.engine", "Engine"),
        ("nvh.core.engine", "NoSuchSymbol"),
        ("nvh.no_such_module", "x"),
    ))
    assert tests[0].status == "fail"
    assert tests[0].summary == "1/3 modules import"
    assert {t.id for t in tests[1:]} == {"import:nvh.core.engine", "import:nvh.no_such_module"}
    assert all(t.status == "fail" for t in tests[1:])


def test_smoke_report_includes_probe_only_when_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("NVH_HOME", str(tmp_path))
    for var in ("NVHIVE_HOME", "NVH_STATE"):
        monkeypatch.delenv(var, raising=False)
    without = smoke_test_report(home_dir=str(tmp_path))
    with_probe = smoke_test_report(home_dir=str(tmp_path), imports=True)
    ids_without = {t["id"] for t in without["tests"]}
    ids_with = {t["id"] for t in with_probe["tests"]}
    assert "core-imports" not in ids_without
    assert "core-imports" in ids_with
    assert with_probe["summary"].endswith("failed")


# ---------------------------------------------------------------------------
# 2. ToolRegistry creates with browser + vision tools registered
# ---------------------------------------------------------------------------

def test_tool_registry_has_browser_tools():
    from nvh.core.tools import ToolRegistry
    tr = ToolRegistry(include_system=True)
    tool_names = {t.name for t in tr.list_tools()}
    expected = {"browser_navigate", "browser_screenshot",
                "browser_fill_form", "http_request",
                "docker_ps", "docker_run"}
    missing = expected - tool_names
    assert not missing, f"Browser tools missing from registry: {missing}"


def test_tool_registry_has_vision_tools():
    from nvh.core.tools import ToolRegistry
    tr = ToolRegistry(include_system=True)
    tool_names = {t.name for t in tr.list_tools()}
    expected = {"capture_screenshot", "analyze_image",
                "read_text_from_image", "mouse_move",
                "mouse_click", "keyboard_type",
                "keyboard_press", "scroll"}
    missing = expected - tool_names
    assert not missing, f"Vision tools missing from registry: {missing}"


def test_tool_registry_minimum_count():
    """ToolRegistry should have builtins + system + browser + vision tools."""
    from nvh.core.tools import ToolRegistry
    tr = ToolRegistry(include_system=True)
    tools = tr.list_tools()
    # builtins (8) + system + browser (8) + vision (8) = 24+
    assert len(tools) >= 20, f"Expected 20+ tools, got {len(tools)}"


# ---------------------------------------------------------------------------
# 3. Config files are valid YAML
# ---------------------------------------------------------------------------

def test_bundled_config_yaml_valid():
    """All YAML files shipped in nvh/config/ must parse without errors."""
    from pathlib import Path

    import yaml

    config_dir = Path(__file__).resolve().parent.parent / "nvh" / "config"
    yaml_files = list(config_dir.glob("*.yaml")) + list(config_dir.glob("*.yml"))
    assert len(yaml_files) > 0, "No YAML config files found in nvh/config/"
    for yf in yaml_files:
        with open(yf) as f:
            data = yaml.safe_load(f)
        assert data is not None, f"{yf.name} parsed as empty/None"


def test_load_config_succeeds():
    """load_config() must return a valid CouncilConfig."""
    from nvh.config.settings import load_config
    config = load_config()
    assert config is not None
    assert hasattr(config, "defaults")


# ---------------------------------------------------------------------------
# 4. Provider reachability (5s timeout) — marked as optional/network
# ---------------------------------------------------------------------------

@pytest.mark.timeout(15)
@pytest.mark.asyncio
async def test_at_least_one_provider_reachable():
    """At least one configured provider should respond to a health check.

    This test is best-effort: it passes if ANY provider responds within 5s.
    If no providers are configured or all are offline, it marks as xfail
    rather than hard-failing (environment-dependent).
    """
    import asyncio

    from nvh.core.engine import Engine
    engine = Engine()
    await engine.initialize()
    enabled = engine.registry.list_enabled()
    if not enabled:
        pytest.xfail("No providers configured")

    for name in enabled:
        try:
            provider = engine.registry.get(name)
            hs = await asyncio.wait_for(
                provider.health_check(), timeout=5.0
            )
            if hs.healthy:
                return  # at least one is reachable
        except Exception:
            continue

    pytest.xfail("No provider responded within 5s (environment issue)")


# ---------------------------------------------------------------------------
# 5. GPU detection works
# ---------------------------------------------------------------------------

def test_gpu_detection():
    """GPU detection must return a list (possibly empty) without crashing."""
    from nvh.utils.gpu import detect_gpus
    gpus = detect_gpus()
    assert isinstance(gpus, list)
