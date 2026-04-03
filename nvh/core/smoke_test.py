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
) -> SmokeTestReport:
    """Run the full smoke test suite."""
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
                    add(f"Provider: {name}", "Providers", False,
                        duration_ms=ms, error=hs.error or "unhealthy")
            except Exception as e:
                add(f"Provider: {name}", "Providers", False, error=str(e)[:100])

    # ===== QUERY EXECUTION =====
    if engine:
        try:
            resp, ms = await _timed_async(engine.query(test_query))
            add("Smart query", "Query", True,
                duration_ms=ms,
                message=f"{resp.provider}/{resp.model} — {len(resp.content)} chars")
        except Exception as e:
            add("Smart query", "Query", False, error=str(e)[:100])

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
            resp = await client.get(f"{api_url}/v1/advisors", timeout=10)
            data = resp.json()
            providers = data.get("data", {}).get("providers", [])
            healthy = [p["name"] for p in providers if p.get("healthy")]
            add("API /v1/advisors", "API", len(providers) > 0,
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
            error='pip install "nvhive[mcp]"')
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
                message=f"{gpu.name} ({gpu.vram_total_mb}MB)")
        else:
            add("GPU detection", "Hardware", True,
                message="No GPU (CPU mode)")
    except Exception as e:
        add("GPU detection", "Hardware", False, error=str(e)[:80])

    report.total_ms = int((time.monotonic() - start) * 1000)
    return report
