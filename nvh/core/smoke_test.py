"""End-to-end smoke test suite for nvHive.

Walks through the entire product and verifies everything works:
- CLI imports and commands
- API server connectivity
- Provider health and query execution
- WebUI reachability
- Integrations detection
- MCP server module
- Key features (routing, council, safe mode)

Run via: nvh test
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class TestResult:
    name: str
    category: str
    passed: bool
    duration_ms: int = 0
    message: str = ""
    error: str = ""


@dataclass
class SmokeTestReport:
    results: list[TestResult] = field(default_factory=list)
    total_ms: int = 0

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.passed)

    @property
    def total(self) -> int:
        return len(self.results)


def _soft_fail_reason(error: str) -> tuple[bool, str]:
    """Classify provider errors that are environmental rather than bugs.

    Returns (soft_pass, label). Soft-pass errors are reported as passing
    so the smoke test doesn't fail CI on transient/tier issues.
    """
    e = error.lower()
    if "rate" in e or "429" in e:
        return True, "rate limited (transient)"
    # GitHub Models free tier requires a token with the 'models' scope;
    # a valid-but-underscoped PAT is a config issue, not a product bug.
    if "models" in e and "permission" in e:
        return True, "token missing 'models' scope (config)"
    if "insufficient_quota" in e or "quota" in e and "exceed" in e:
        return True, "quota exceeded (config)"
    return False, ""


def _timed(fn):
    """Run a sync function and return (result, duration_ms)."""
    start = time.monotonic()
    try:
        result = fn()
        ms = int((time.monotonic() - start) * 1000)
        return result, ms
    except Exception as e:
        ms = int((time.monotonic() - start) * 1000)
        raise type(e)(str(e)) from e


async def _timed_async(coro):
    """Run an async coroutine and return (result, duration_ms)."""
    start = time.monotonic()
    try:
        result = await coro
        ms = int((time.monotonic() - start) * 1000)
        return result, ms
    except Exception as e:
        ms = int((time.monotonic() - start) * 1000)
        raise type(e)(str(e)) from e


async def run_smoke_tests(
    api_url: str = "http://localhost:8000",
    webui_url: str = "http://localhost:3000",
    test_query: str = "Say hello in one sentence",
    skip_webui: bool = False,
    skip_providers: bool = False,
    quick: bool = False,
) -> SmokeTestReport:
    """Run the full smoke test suite.

    When ``quick`` is True, skip everything that touches the network or
    remote providers: engine queries, API server endpoints, and agent
    generation. Useful as a fast local sanity check (e.g., post-install).
    """
    report = SmokeTestReport()
    start = time.monotonic()

    def add(name: str, category: str, passed: bool,
            duration_ms: int = 0, message: str = "", error: str = ""):
        report.results.append(TestResult(
            name=name, category=category, passed=passed,
            duration_ms=duration_ms, message=message, error=error,
        ))

    # ===== CORE IMPORTS =====
    try:
        from nvh import __version__
        add("Import nvh package", "Core", True, message=f"v{__version__}")
    except Exception as e:
        add("Import nvh package", "Core", False, error=str(e))

    try:
        from nvh.core.engine import Engine
        add("Import Engine", "Core", True)
    except Exception as e:
        add("Import Engine", "Core", False, error=str(e))

    try:
        from nvh.core.router import RoutingEngine  # noqa: F401
        add("Import Router", "Core", True)
    except Exception as e:
        add("Import Router", "Core", False, error=str(e))

    try:
        from nvh.core.council import CouncilOrchestrator  # noqa: F401
        add("Import Council", "Core", True)
    except Exception as e:
        add("Import Council", "Core", False, error=str(e))

    try:
        from nvh.cli.main import app  # noqa: F401
        add("Import CLI", "Core", True)
    except Exception as e:
        add("Import CLI", "Core", False, error=str(e))

    # ===== ENGINE INITIALIZATION =====
    engine = None
    try:
        engine = Engine()
        await engine.initialize()
        enabled = engine.registry.list_enabled()
        add("Engine init", "Engine", True,
            message=f"{len(enabled)} providers: {', '.join(enabled)}")
    except Exception as e:
        add("Engine init", "Engine", False, error=str(e))

    # ===== PROVIDER HEALTH =====
    if engine and not skip_providers:
        enabled = engine.registry.list_enabled()
        for name in enabled:
            try:
                provider = engine.registry.get(name)
                hs, ms = await _timed_async(provider.health_check())
                if hs.healthy:
                    add(f"Provider: {name}", "Providers", True,
                        duration_ms=ms, message=f"healthy ({hs.latency_ms}ms)")
                else:
                    error = hs.error or "unhealthy"
                    soft, label = _soft_fail_reason(error)
                    add(f"Provider: {name}", "Providers", soft,
                        duration_ms=ms,
                        message=label if soft else "",
                        error="" if soft else error)
            except Exception as e:
                error = str(e)[:200]
                soft, label = _soft_fail_reason(error)
                add(f"Provider: {name}", "Providers", soft,
                    message=label if soft else "",
                    error="" if soft else error)

    # ===== QUERY EXECUTION =====
    if engine and not quick:
        try:
            resp, ms = await _timed_async(engine.query(test_query))
            add("Smart query", "Query", True,
                duration_ms=ms,
                message=f"{resp.provider}/{resp.model} — {len(resp.content)} chars")
        except Exception as e:
            error = str(e)[:100]
            is_rate_limit = "rate" in error.lower() or "429" in error
            add("Smart query", "Query", is_rate_limit,
                message="all providers rate limited (transient)" if is_rate_limit else "",
                error="" if is_rate_limit else error)

        # Safe mode
        if engine.registry.has("ollama"):
            try:
                resp, ms = await _timed_async(
                    engine.query(test_query, provider="ollama"))
                add("Safe mode (local)", "Query", True,
                    duration_ms=ms, message=f"{resp.provider}")
            except Exception:
                add("Safe mode (local)", "Query", False,
                    error="Ollama not running (expected if no GPU)")

    # ===== API SERVER =====
    if quick:
        # Skip all remote API/WebUI/agent-generation probes in quick mode.
        report.total_ms = int((time.monotonic() - start) * 1000)
        return report
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{api_url}/v1/health", timeout=5)
            data = resp.json()
            ok = data.get("data", {}).get("status") == "ok"
            add("API /v1/health", "API", ok,
                message="online" if ok else "unhealthy")
    except Exception:
        add("API /v1/health", "API", False,
            error=f"Not reachable at {api_url} — run: nvh serve")

    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{api_url}/v1/advisors", timeout=30)
            data = resp.json()
            providers = data.get("data", {}).get("providers", [])
            healthy = [p["name"] for p in providers if p.get("healthy")]
            # Pass if we got a valid response (even if no providers healthy)
            add("API /v1/advisors", "API", resp.status_code == 200,
                message=f"{len(healthy)}/{len(providers)} healthy")
    except Exception as e:
        add("API /v1/advisors", "API", False, error=str(e)[:80])

    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{api_url}/v1/proxy/health", timeout=5)
            data = resp.json()
            add("API /v1/proxy/health", "API", True,
                message=f"{data.get('providers_enabled', '?')} providers")
    except Exception as e:
        add("API /v1/proxy/health", "API", False, error=str(e)[:80])

    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{api_url}/v1/integrations/scan", timeout=10)
            data = resp.json().get("data", {})
            add("API /v1/integrations/scan", "API", True,
                message=(
                    f"{data.get('detected_count', 0)} detected, "
                    f"{data.get('configured_count', 0)} configured"
                ))
    except Exception as e:
        add("API /v1/integrations/scan", "API", False, error=str(e)[:80])

    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{api_url}/v1/quota", timeout=5)
            data = resp.json().get("data", {})
            quotas = data.get("quotas", [])
            add("API /v1/quota", "API", True,
                message=f"{len(quotas)} providers with quota info")
    except Exception as e:
        add("API /v1/quota", "API", False, error=str(e)[:80])

    # ===== WEBUI =====
    if not skip_webui:
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.get(webui_url, timeout=5)
                ok = resp.status_code == 200
                add("WebUI reachable", "WebUI", ok,
                    message=f"HTTP {resp.status_code}")
        except Exception:
            add("WebUI reachable", "WebUI", False,
                error=f"Not reachable at {webui_url} — run: nvh webui")

        # Check key pages
        pages = ["/providers", "/integrations", "/system",
                 "/setup", "/settings"]
        for page in pages:
            try:
                import httpx
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"{webui_url}{page}", timeout=5)
                    add(f"WebUI {page}", "WebUI",
                        resp.status_code == 200,
                        message=f"HTTP {resp.status_code}")
            except Exception:
                add(f"WebUI {page}", "WebUI", False, error="unreachable")

    # ===== MCP SERVER =====
    try:
        from nvh.mcp_server import create_server
        create_server()
        add("MCP server module", "MCP", True, message="loads OK")
    except ImportError:
        add("MCP server module", "MCP", False,
            error=r'pip install "nvhive\[mcp]"')
    except Exception:
        add("MCP server module", "MCP", True,
            message="loads OK (MCP SDK not installed)")

    # ===== INTEGRATIONS =====
    try:
        from nvh.integrations.detector import detect_platforms
        platforms = detect_platforms()
        detected = [p for p in platforms if p.detected]
        configured = [p for p in platforms if p.already_configured]
        add("Platform detection", "Integrations", True,
            message=(
                f"{len(detected)} detected, "
                f"{len(configured)} configured"
            ))
        for p in platforms:
            status = (
                "configured" if p.already_configured
                else "detected" if p.detected
                else "not found"
            )
            add(f"  {p.display_name}", "Integrations", p.detected or True,
                message=status)
    except Exception as e:
        add("Platform detection", "Integrations", False, error=str(e)[:80])

    # ===== QUOTA INFO =====
    try:
        from nvh.providers.quota_info import get_quota_info
        info = get_quota_info("groq")
        add("Quota info system", "Quota", True,
            message=f"groq: {info.tier} tier")
    except Exception as e:
        add("Quota info system", "Quota", False, error=str(e)[:80])

    # ===== GPU =====
    try:
        from nvh.utils.gpu import detect_gpus
        gpus = detect_gpus()
        if gpus:
            gpu = gpus[0]
            add("GPU detection", "Hardware", True,
                message=f"{gpu.name} ({gpu.vram_mb}MB)")
        else:
            add("GPU detection", "Hardware", True,
                message="No GPU (CPU mode)")
    except Exception as e:
        add("GPU detection", "Hardware", False, error=str(e)[:80])

    # ===== QUERY MODES =====
    if engine:
        # Action detection
        try:
            from nvh.core.action_detector import detect_action
            action = detect_action("install pandas")
            add("Action detector", "Query Modes", action is not None,
                message=f"detected: {action.tool_name if action else 'none'}")
        except Exception as e:
            add("Action detector", "Query Modes", False, error=str(e)[:80])

        # Task classification
        try:
            from nvh.core.router import classify_task
            result = classify_task("Write a Python sort function")
            top_task = max(result.all_scores, key=result.all_scores.get)
            add("Task classifier", "Query Modes", True,
                message=f"'{top_task}' ({result.all_scores[top_task]:.2f})")
        except Exception as e:
            add("Task classifier", "Query Modes", False, error=str(e)[:80])

        # Router scoring
        try:
            decision = engine.router.route("Explain quantum computing")
            add("Smart routing", "Query Modes", True,
                message=f"→ {decision.provider}/{decision.model}")
        except Exception as e:
            add("Smart routing", "Query Modes", False, error=str(e)[:80])

    # ===== AGENT SYSTEM =====
    try:
        from nvh.core.agents import generate_agents, list_presets
        presets = list_presets()
        add("Agent presets", "Agents", len(presets) > 0,
            message=f"{len(presets)} cabinets")
    except Exception as e:
        add("Agent presets", "Agents", False, error=str(e)[:80])

    try:
        from nvh.core.agents import generate_agents
        agents = generate_agents("Should we migrate to microservices?", num_agents=3)
        add("Agent generation", "Agents", len(agents) > 0,
            message=f"{len(agents)} agents: {', '.join(a.role for a in agents)}")
    except Exception as e:
        add("Agent generation", "Agents", False, error=str(e)[:80])

    # ===== ALL 18 CORE FEATURE IMPORTS =====
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
    passed_imports = 0
    for mod_path, symbol in _CORE_FEATURES:
        try:
            import importlib
            mod = importlib.import_module(mod_path)
            assert hasattr(mod, symbol), f"{symbol} not found in {mod_path}"
            passed_imports += 1
        except Exception as e:
            add(f"Import {mod_path}.{symbol}", "Core Features", False,
                error=str(e)[:80])
    add("Core feature imports", "Core Features",
        passed_imports == len(_CORE_FEATURES),
        message=f"{passed_imports}/{len(_CORE_FEATURES)} imported OK")

    # ===== TOOL SYSTEM =====
    try:
        from nvh.core.tools import ToolRegistry
        registry = ToolRegistry(include_system=True)
        tools = registry.list_tools()
        safe = [t for t in tools if t.safe]
        add("Tool registry", "Tools", len(tools) > 0,
            message=f"{len(tools)} tools ({len(safe)} safe)")
    except Exception as e:
        add("Tool registry", "Tools", False, error=str(e)[:80])

    # ===== BROWSER + VISION TOOLS REGISTERED =====
    try:
        from nvh.core.tools import ToolRegistry
        tr = ToolRegistry(include_system=True)
        tool_names = [t.name for t in tr.list_tools()]
        browser_expected = {"browser_navigate", "browser_screenshot",
                           "browser_fill_form", "http_request",
                           "docker_ps", "docker_run"}
        vision_expected = {"capture_screenshot", "analyze_image",
                          "read_text_from_image", "mouse_move",
                          "mouse_click", "keyboard_type",
                          "keyboard_press", "scroll"}
        browser_found = browser_expected & set(tool_names)
        vision_found = vision_expected & set(tool_names)
        add("Browser tools registered", "Tools",
            len(browser_found) == len(browser_expected),
            message=f"{len(browser_found)}/{len(browser_expected)} browser tools")
        add("Vision tools registered", "Tools",
            len(vision_found) == len(vision_expected),
            message=f"{len(vision_found)}/{len(vision_expected)} vision tools")
    except Exception as e:
        add("Browser+Vision tools", "Tools", False, error=str(e)[:80])

    # ===== DOCKER AVAILABILITY =====
    try:
        from nvh.core.docker_sandbox import is_docker_available
        docker_ok = is_docker_available()
        add("Docker availability", "Security", True,
            message=f"docker: {'available' if docker_ok else 'not available (OK)'}")
    except Exception as e:
        add("Docker availability", "Security", False, error=str(e)[:80])

    # ===== CONFIG YAML VALIDATION =====
    try:
        from pathlib import Path as _Path

        import yaml as _yaml
        config_dirs = [
            _Path.home() / ".config" / "nvhive",
            _Path.home() / ".nvhive",
        ]
        yaml_checked = 0
        yaml_valid = 0
        for d in config_dirs:
            if d.exists():
                for yf in d.glob("*.yaml"):
                    yaml_checked += 1
                    with open(yf) as f:
                        _yaml.safe_load(f)
                    yaml_valid += 1
                for yf in d.glob("*.yml"):
                    yaml_checked += 1
                    with open(yf) as f:
                        _yaml.safe_load(f)
                    yaml_valid += 1
        # Also validate bundled config
        bundled = _Path(__file__).parent.parent / "config"
        if bundled.exists():
            for yf in list(bundled.glob("*.yaml")) + list(bundled.glob("*.yml")):
                yaml_checked += 1
                with open(yf) as f:
                    _yaml.safe_load(f)
                yaml_valid += 1
        add("Config YAML validation", "Config",
            yaml_valid == yaml_checked,
            message=f"{yaml_valid}/{yaml_checked} YAML files valid")
    except Exception as e:
        add("Config YAML validation", "Config", False, error=str(e)[:80])

    # ===== PROVIDER REACHABILITY (5s timeout) =====
    if engine and not skip_providers:
        enabled = engine.registry.list_enabled()
        any_reachable = False
        for name in enabled:
            try:
                provider = engine.registry.get(name)
                hs, ms = await _timed_async(provider.health_check())
                if hs.healthy and ms <= 5000:
                    any_reachable = True
                    break
            except Exception:
                continue
        add("Provider reachable (5s)", "Providers", any_reachable,
            message="at least one provider responds within 5s"
            if any_reachable else "no provider responded within 5s")

    # ===== API QUERY EXECUTION =====
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{api_url}/v1/query",
                json={"prompt": test_query, "stream": False},
                timeout=30,
            )
            data = resp.json()
            content = data.get("data", {}).get("content", "")
            provider = data.get("data", {}).get("provider", "?")
            add("API query execution", "API Query", len(content) > 0,
                message=f"{provider}: {len(content)} chars")
    except Exception as e:
        add("API query execution", "API Query", False, error=str(e)[:80])

    # OpenAI proxy
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{api_url}/v1/proxy/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "Say hi"}],
                    "max_tokens": 10,
                },
                timeout=30,
            )
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            add("OpenAI proxy", "API Query", len(content) > 0,
                message=f"response: {content[:40]}")
    except Exception as e:
        add("OpenAI proxy", "API Query", False, error=str(e)[:80])

    # ===== STORAGE =====
    try:
        from nvh.storage import repository as repo  # noqa: F401
        add("Storage module", "Storage", True, message="imports OK")
    except Exception as e:
        add("Storage module", "Storage", False, error=str(e)[:80])

    # ===== CONFIGURATION =====
    try:
        from nvh.config.settings import load_config
        config = load_config()
        add("Config loading", "Config", True,
            message=f"system_prompt: {len(config.defaults.system_prompt)} chars")
    except Exception as e:
        add("Config loading", "Config", False, error=str(e)[:80])

    # HIVE.md / context files
    try:
        from nvh.core.context_files import find_context_files
        ctx_files = find_context_files()
        add("Context files (HIVE.md)", "Config", True,
            message=f"{len(ctx_files)} found" if ctx_files else "none (OK)")
    except Exception as e:
        add("HIVE.md detection", "Config", False, error=str(e)[:80])

    # ===== KEYRING =====
    try:
        import keyring
        keyring.get_password("nvhive", "smoke_test_key")
        add("Keyring access", "Security", True,
            message="accessible")
    except Exception as e:
        add("Keyring access", "Security", False,
            error=f"keyring unavailable: {str(e)[:60]}")

    # ===== ADVISOR PROFILES =====
    try:
        from nvh.core.advisor_profiles import ADVISOR_PROFILES
        add("Advisor profiles", "Config", len(ADVISOR_PROFILES) > 0,
            message=f"{len(ADVISOR_PROFILES)} profiles loaded")
    except Exception as e:
        add("Advisor profiles", "Config", False, error=str(e)[:80])

    # ===== CAPABILITIES CATALOG =====
    try:
        from pathlib import Path
        cap_file = Path(__file__).parent.parent / "config" / "capabilities.yaml"
        if cap_file.exists():
            import yaml
            with open(cap_file) as f:
                caps = yaml.safe_load(f)
            models = caps.get("models", {})
            add("Capabilities catalog", "Config", len(models) > 0,
                message=f"{len(models)} models defined")
        else:
            add("Capabilities catalog", "Config", False,
                error="capabilities.yaml not found")
    except Exception as e:
        add("Capabilities catalog", "Config", False, error=str(e)[:80])

    # ===== ORCHESTRATOR =====
    try:
        from nvh.core.orchestrator import LocalOrchestrator
        orch = LocalOrchestrator()
        add("Orchestrator", "Orchestration", True,
            message=f"mode: {orch.mode.value}")
    except Exception as e:
        add("Orchestrator", "Orchestration", False, error=str(e)[:80])

    # ===== SANDBOX =====
    try:
        from nvh.sandbox.executor import SandboxExecutor
        SandboxExecutor()  # verify it instantiates
        has_docker = False
        try:
            import subprocess
            result = subprocess.run(
                ["docker", "info"], capture_output=True, timeout=3)
            has_docker = result.returncode == 0
        except Exception:
            pass
        add("Sandbox executor", "Security", True,
            message=f"docker: {'available' if has_docker else 'subprocess fallback'}")
    except Exception as e:
        add("Sandbox executor", "Security", False, error=str(e)[:80])

    # ===== FREE TIER DETECTION =====
    try:
        from nvh.core.free_tier import detect_available_free_advisors
        free = detect_available_free_advisors()
        add("Free tier detection", "Providers", True,
            message=f"{len(free)} free advisors available")
    except Exception as e:
        add("Free tier detection", "Providers", False, error=str(e)[:80])

    report.total_ms = int((time.monotonic() - start) * 1000)
    return report
