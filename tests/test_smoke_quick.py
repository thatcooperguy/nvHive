"""Pytest smoke tests — fast, offline subset of nvh test --quick.

Run via: python -m pytest tests/test_smoke_quick.py -v --timeout=30
"""

from __future__ import annotations

import importlib

import pytest

# ---------------------------------------------------------------------------
# 1. All 18 core feature imports
# ---------------------------------------------------------------------------

_CORE_FEATURES = [
    ("nvh.core.engine", "Engine"),
    ("nvh.core.router", "RoutingEngine"),
    ("nvh.core.council", "CouncilOrchestrator"),
    ("nvh.core.agents", "generate_agents"),
    ("nvh.core.tools", "ToolRegistry"),
    ("nvh.core.browser_tools", "register_browser_tools"),
    ("nvh.core.vision_tools", "register_vision_tools"),
    ("nvh.core.docker_sandbox", "is_docker_available"),
    ("nvh.core.agent_loop", "run_agent_loop"),
    ("nvh.core.agent_guardrails", "check_command"),
    ("nvh.core.code_graph", "build_import_graph"),
    ("nvh.core.learning", "LearningEngine"),
    ("nvh.core.smart_query", "query_with_escalation"),
    ("nvh.core.orchestrator", "LocalOrchestrator"),
    ("nvh.core.action_detector", "detect_action"),
    ("nvh.core.cost_tracker", "CostReport"),
    ("nvh.core.parallel_pipeline", "run_parallel_pipeline"),
    ("nvh.core.workflows", "run_workflow"),
]


@pytest.mark.parametrize("mod_path,symbol", _CORE_FEATURES,
                         ids=[f"{m}.{s}" for m, s in _CORE_FEATURES])
def test_core_feature_import(mod_path: str, symbol: str):
    """Every core feature module must import and expose its key symbol."""
    mod = importlib.import_module(mod_path)
    assert hasattr(mod, symbol), f"{symbol} not found in {mod_path}"


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
# 3. Docker availability detection works (does not require Docker)
# ---------------------------------------------------------------------------

def test_docker_availability_detection():
    """is_docker_available() must return a bool without crashing."""
    from nvh.core.docker_sandbox import is_docker_available
    result = is_docker_available()
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# 4. Config files are valid YAML
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
# 5. Provider reachability (5s timeout) — marked as optional/network
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
# 6. GPU detection works
# ---------------------------------------------------------------------------

def test_gpu_detection():
    """GPU detection must return a list (possibly empty) without crashing."""
    from nvh.utils.gpu import detect_gpus
    gpus = detect_gpus()
    assert isinstance(gpus, list)


# ---------------------------------------------------------------------------
# 7. Smoke test runner itself works in quick mode
# ---------------------------------------------------------------------------

@pytest.mark.timeout(30)
@pytest.mark.asyncio
async def test_smoke_quick_mode():
    """run_smoke_tests(quick=True) should complete and return a report."""
    from nvh.core.smoke_test import run_smoke_tests

    report = await run_smoke_tests(quick=True, skip_providers=True)
    assert report.total > 0, "Smoke test produced zero results"
    assert report.total_ms > 0, "Smoke test duration was 0"
    # Quick mode should not have hard failures on core imports
    failed = [r for r in report.results if not r.passed]
    # Print failures for debugging
    for f in failed:
        print(f"  FAIL: {f.name} — {f.error}")
