"""One registry of local health checks.

``nvh status`` renders a tier of it (glance / providers / deep / smoke /
report) and ``/v1/setup/diagnostics`` embeds the same rows. Every probe that
more than one check needs (config, an initialised Engine, the GPU list, the
Ollama tag list, the API server) runs once per ``CheckContext``.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from nvh.utils.ollama import ollama_base_url, probe_installed_models

GLANCE = "glance"
PROVIDERS = "providers"
DEEP = "deep"
SMOKE = "smoke"
REPORT = "report"
TIERS = (GLANCE, PROVIDERS, DEEP, SMOKE, REPORT)

DEFAULT_API_URL = "http://127.0.0.1:8000"
SUNSET_WARNING_DAYS = 30
_SMOKE_PROMPT = "Reply with exactly one word: pong"

_UNSET = object()


@dataclass
class CheckResult:
    # ``id``/``title`` default to the owning Check's; ``run_check`` backfills them.
    id: str = ""
    title: str = ""
    status: str = "info"  # pass | warn | fail | skip | info
    detail: str = ""
    fix: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        # ``check`` first: the WebUI and CI parsers split the JSON on it.
        row: dict[str, Any] = {
            "check": self.title,
            "status": self.status,
            "detail": self.detail,
            "fix": self.fix,
            "id": self.id,
        }
        if self.data:
            row["data"] = self.data
        return row


CheckFn = Callable[["CheckContext"], Any]


@dataclass(frozen=True)
class Check:
    id: str
    title: str
    tiers: frozenset[str]
    run: CheckFn


REGISTRY: list[Check] = []


def check(id: str, title: str, *tiers: str) -> Callable[[CheckFn], CheckFn]:
    def register(fn: CheckFn) -> CheckFn:
        REGISTRY.append(Check(id, title, frozenset(tiers), fn))
        return fn

    return register


def probe_ollama_models(timeout: float = 5.0) -> list[str] | None:
    """Model tags from the local Ollama daemon, or None when unreachable."""
    return probe_installed_models(timeout=timeout)


@dataclass
class ApiProbe:
    """Result of the one ``GET /v1/health`` every API smoke check shares."""

    url: str
    reachable: bool = False
    health: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def headers(self) -> dict[str, str]:
        key = os.environ.get("HIVE_API_KEY", "")
        return {"Authorization": f"Bearer {key}"} if key else {}


class CheckContext:
    def __init__(
        self,
        *,
        home_dir: str | None = None,
        min_free_gb: float = 200.0,
        health_timeout: float = 10.0,
        api_url: str | None = None,
        api_timeout: float = 60.0,
    ) -> None:
        self.home_dir = home_dir
        self.min_free_gb = min_free_gb
        self.health_timeout = health_timeout
        # ``nvh test --api URL`` exports NVH_API_URL for a remote server.
        self.api_url = (api_url or os.environ.get("NVH_API_URL") or DEFAULT_API_URL).rstrip("/")
        self.api_timeout = api_timeout
        self.config_error: str | None = None
        self.enabled_providers: list[str] = []
        self._config: Any = _UNSET
        self._engine: Any = None
        self._ollama: Any = _UNSET
        self._gpus: Any = _UNSET
        self._api: Any = _UNSET

    @property
    def config(self) -> Any:
        if self._config is _UNSET:
            try:
                from nvh.config.settings import load_config

                self._config = load_config()
            except Exception as exc:
                self._config = None
                self.config_error = str(exc)
        return self._config

    @property
    def gpus(self) -> list[Any]:
        """Detected GPUs, probed once (nvidia-smi costs ~100 ms per spawn)."""
        if self._gpus is _UNSET:
            from nvh.utils.gpu import detect_gpus

            try:
                self._gpus = detect_gpus()
            except Exception:
                self._gpus = []
        return self._gpus

    async def engine(self) -> Any:
        """Initialised Engine, or None when the config did not load."""
        if self._engine is None and self.config is not None:
            from nvh.core.engine import Engine

            engine = Engine(config=self.config)
            vram_gb = sum(float(g.vram_gb) for g in self.gpus)
            self.enabled_providers = list(await engine.initialize(gpu_vram_gb=vram_gb))
            self._engine = engine
        return self._engine

    @property
    def ollama_models(self) -> list[str] | None:
        if self._ollama is _UNSET:
            self._ollama = probe_ollama_models()
        return self._ollama

    def reset_ollama(self) -> None:
        self._ollama = _UNSET

    async def api(self) -> ApiProbe:
        """Reachability of the API server, probed once via ``/v1/health``."""
        if self._api is _UNSET:
            probe = ApiProbe(self.api_url)
            try:
                import httpx

                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(f"{self.api_url}/v1/health", headers=probe.headers)
                probe.reachable = True
                if resp.status_code == 200:
                    probe.health = _unwrap(resp.json())
                else:
                    probe.error = f"HTTP {resp.status_code}"
            except Exception as exc:
                probe.error = str(exc) or type(exc).__name__
            self._api = probe
        return self._api


async def run_check(check: Check, ctx: CheckContext) -> list[CheckResult]:
    try:
        out = check.run(ctx)
        if inspect.isawaitable(out):
            out = await out
    except Exception as exc:
        return [CheckResult(check.id, check.title, "warn", f"check failed: {exc}")]
    if out is None:
        return []
    rows = list(out) if isinstance(out, list) else [out]
    for row in rows:
        row.id = row.id or check.id
        row.title = row.title or check.title
    return rows


def checks_for(tier: str) -> list[Check]:
    if tier == REPORT:
        return [c for c in REGISTRY if c.tiers & {DEEP, REPORT}]
    return [c for c in REGISTRY if tier in c.tiers]


async def run_checks(tier: str, ctx: CheckContext | None = None) -> list[CheckResult]:
    ctx = ctx or CheckContext()
    results: list[CheckResult] = []
    for c in checks_for(tier):
        results.extend(await run_check(c, ctx))
    return results


def run_checks_sync(tier: str, ctx: CheckContext | None = None) -> list[CheckResult]:
    """``run_checks`` for sync callers, including ones already inside a loop
    (the FastAPI diagnostics route), which get a worker thread and its own loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run_checks(tier, ctx))
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, run_checks(tier, ctx)).result()


def summarize(results: list[CheckResult]) -> dict[str, Any]:
    counts = {s: sum(1 for r in results if r.status == s) for s in ("pass", "warn", "fail", "skip")}
    return {
        "total": len(results),
        "passed": counts["pass"],
        "warned": counts["warn"],
        "failed": counts["fail"],
        "skipped": counts["skip"],
        "fixes": [r.fix for r in results if r.fix and r.status != "pass"],
        "checks": [r.as_dict() for r in results],
    }


def nvh_version() -> str:
    try:
        from nvh import __version__

        return str(__version__)
    except Exception:
        return "unknown"


def platform_summary() -> dict[str, str]:
    from nvh.integrations.diagnostics.compatibility import platform_summary as facts

    return facts()


def _jsonable(value: Any) -> Any:
    """Budget figures arrive as Decimal, which json.dumps rejects."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _unwrap(payload: Any) -> Any:
    """Strip the API's ``{"status": ..., "data": ...}`` envelope when present."""
    if isinstance(payload, dict) and "data" in payload and "status" in payload:
        return payload["data"]
    return payload


# ---------------------------------------------------------------------------
# Checks — registration order is display order.
# ---------------------------------------------------------------------------


@check("python", "Python version", DEEP)
def _python(ctx: CheckContext) -> CheckResult:
    from nvh.integrations.diagnostics.compatibility import _version_at_least, python_version

    text = python_version()
    if _version_at_least(text, "3.11"):
        return CheckResult(status="pass", detail=text)
    return CheckResult(
        status="fail",
        detail=f"{text} (need >= 3.11)",
        fix="Upgrade Python to 3.11+: https://python.org/downloads",
    )


@check("system", "System", REPORT)
def _system(ctx: CheckContext) -> CheckResult:
    import platform

    data = {
        **platform_summary(),
        "platform": platform.platform(),
        "python_build": sys.version,
        "executable": sys.executable,
        "nvh": nvh_version(),
        "cwd": os.getcwd(),
        "home": str(Path.home()),
        "user": os.environ.get("USER") or os.environ.get("USERNAME", "unknown"),
        "shell": os.environ.get("SHELL", "unknown"),
    }
    return CheckResult(status="info", detail=f"{data['platform']} · nvh {data['nvh']}", data=data)


@check("storage", "Rootless storage", DEEP)
def _storage(ctx: CheckContext) -> CheckResult:
    from nvh.integrations.workspace.storage import storage_status

    status = storage_status(home_dir=ctx.home_dir, min_free_gb=ctx.min_free_gb)
    free = status.free_gb if status.free_gb is not None else "?"
    detail = f"{status.layout.home} ({free} GB free)"
    if status.ok and status.configured_by != "default":
        return CheckResult(status="pass", detail=detail, data=status.as_dict())
    return CheckResult(
        status="warn",
        detail="; ".join(status.warnings) or detail,
        fix="Run `nvh status --deep --storage --home-dir /path/on/mounted/volume/nvhive`",
        data=status.as_dict(),
    )


@check("legacy_knowledge", "Legacy knowledge base", DEEP)
def _legacy_knowledge(ctx: CheckContext) -> CheckResult | None:
    from nvh.integrations.rag import legacy_knowledge_status

    legacy = legacy_knowledge_status(home_dir=ctx.home_dir)
    if legacy["found"] and not legacy["imported"]:
        return CheckResult(
            status="warn",
            detail=f"{legacy['documents']} document(s) in {legacy['path']} are not in the RAG index",
            fix="Run `nvh rag import-legacy` once (needs Ollama for embeddings)",
        )
    return None


@check("receipts", "Install receipts", DEEP)
def _receipts(ctx: CheckContext) -> CheckResult:
    from nvh.integrations.services.receipts import receipt_summary

    receipts = receipt_summary()
    detail = (
        f"{receipts['count']} receipt(s), {receipts['unhealthy']} need attention, "
        f"root {receipts['root']}"
    )
    if receipts["unhealthy"]:
        return CheckResult(
            status="warn",
            detail=detail,
            fix="Open the setup wizard or rerun the matching `nvh studio` / `nvh workstation` command.",
        )
    return CheckResult(status="pass", detail=detail)


@check("catalog", "Setup catalog", DEEP)
def _catalog(ctx: CheckContext) -> CheckResult:
    from nvh.integrations.setup_catalog import catalog_status

    catalog = catalog_status(refresh=False)
    detail = (
        f"{catalog.get('source')} catalog, {catalog.get('profile_count', 0)} profiles, "
        f"{catalog.get('model_count', 0)} models"
    )
    if catalog.get("error"):
        return CheckResult(status="warn", detail=f"{detail}; {catalog['error']}")
    return CheckResult(status="pass", detail=detail)


@check("config", "Config file", DEEP)
def _config(ctx: CheckContext) -> list[CheckResult]:
    from nvh.config.settings import DEFAULT_CONFIG_PATH

    path = DEFAULT_CONFIG_PATH
    if not path.exists():
        return [
            CheckResult(
                title="Config file exists",
                status="fail",
                detail=str(path),
                fix="Run `nvh config init` to create a configuration file.",
            )
        ]
    try:
        import yaml

        yaml.safe_load(path.read_text())
    except Exception as exc:
        return [
            CheckResult(
                title="Config file (YAML)",
                status="fail",
                detail=str(exc),
                fix=f"Fix YAML syntax in {path}",
            )
        ]
    rows = [CheckResult(title="Config file (YAML)", status="pass", detail=str(path))]
    if ctx.config is not None:
        rows.append(
            CheckResult(
                title="Config schema (Pydantic)",
                status="pass",
                detail="HiveConfig validated successfully",
            )
        )
    else:
        rows.append(
            CheckResult(
                title="Config schema (Pydantic)",
                status="fail",
                detail=ctx.config_error or "failed to load",
                fix=f"Fix config errors in {path}",
            )
        )
    return rows


@check("database", "Database", DEEP)
async def _database(ctx: CheckContext) -> CheckResult:
    try:
        from nvh.storage import repository as repo

        await repo.init_db()
    except Exception as exc:
        return CheckResult(
            status="fail",
            detail=str(exc),
            fix="Check storage permissions or reinstall: pip install nvhive",
        )
    return CheckResult(status="pass", detail="init_db succeeded")


def _configured_providers(ctx: CheckContext) -> list[tuple[str, Any]]:
    if ctx.config is None:
        return []
    return [(n, p) for n, p in ctx.config.providers.items() if p.enabled]


@check("provider_keys", "Advisor API keys", DEEP)
def _provider_keys(ctx: CheckContext) -> list[CheckResult]:
    from nvh.providers.registry import BESPOKE_ADAPTERS, resolve_provider_key

    rows = []
    for name, pconfig in _configured_providers(ctx):
        ptype = pconfig.type or name
        title = f"Advisor {name}: API key"
        data: dict[str, Any] = {"provider": name}
        if ptype in BESPOKE_ADAPTERS:
            rows.append(
                CheckResult(title=title, status="pass", detail="not needed (local)", data=data)
            )
            continue
        key, source = resolve_provider_key(name, pconfig, ptype=ptype)
        data["source"] = source
        if key:
            rows.append(
                CheckResult(title=title, status="pass", detail=f"found ({source})", data=data)
            )
        else:
            rows.append(
                CheckResult(
                    title=title,
                    status="fail",
                    detail="missing",
                    fix=f"Run `nvh advisor login {name}` or set {name.upper()}_API_KEY",
                    data=data,
                )
            )
    return rows


@check("provider_health", "Advisors", GLANCE, PROVIDERS, DEEP)
async def _provider_health(ctx: CheckContext) -> list[CheckResult]:
    engine = await ctx.engine()
    if engine is None:
        return [
            CheckResult(
                status="fail",
                detail=ctx.config_error or "config did not load",
                fix="Run `nvh setup`",
            )
        ]
    # Config-enabled plus whatever initialize() auto-enabled from env keys.
    names = sorted({name for name, _ in _configured_providers(ctx)} | set(ctx.enabled_providers))
    if not names:
        return [
            CheckResult(
                status="warn", detail="none enabled", fix="Run `nvh setup` to add providers"
            )
        ]
    primary = ctx.config.defaults.provider or (ctx.enabled_providers or names)[0]
    try:
        chain = list(engine._get_fallback_chain(primary))
    except Exception:
        chain = []

    async def one(name: str) -> CheckResult:
        title = f"Advisor {name}: health check"
        data: dict[str, Any] = {
            "provider": name,
            "healthy": False,
            "score": 0.5,
            "chain_position": 0,
        }
        if not engine.registry.has(name):
            return CheckResult(
                title=title,
                status="warn",
                detail="not registered (check API key)",
                fix=f"Run `nvh advisor login {name}`",
                data=data,
            )
        try:
            health = await asyncio.wait_for(
                engine.registry.get(name).health_check(),
                timeout=ctx.health_timeout,
            )
            ok = bool(health.healthy)
            detail = f"{health.latency_ms}ms" if ok else (health.error or "failed")
        except Exception as exc:
            ok, detail = False, str(exc) or type(exc).__name__
        try:
            data["score"] = float(engine.rate_manager.get_health_score(name))
        except Exception:
            pass
        data["healthy"] = ok
        data["chain_position"] = chain.index(name) + 1 if name in chain else 0
        return CheckResult(
            title=title,
            status="pass" if ok else "warn",
            detail=detail,
            fix="" if ok else f"Check your {name} API key and network access.",
            data=data,
        )

    # Every advisor is pinged at once; the slowest one bounds the wall time.
    return list(await asyncio.gather(*(one(name) for name in names)))


@check("fallback_chain", "Fallback chain", PROVIDERS)
async def _fallback_chain(ctx: CheckContext) -> CheckResult | None:
    engine = await ctx.engine()
    if engine is None or not ctx.enabled_providers:
        return None
    primary = ctx.config.defaults.provider or ctx.enabled_providers[0]
    chain = list(engine._get_fallback_chain(primary))
    return CheckResult(status="info", detail=" → ".join(chain[:5]), data={"chain": chain})


@check("retired_models", "Retired models", DEEP)
def _retired_models(ctx: CheckContext) -> list[CheckResult]:
    if ctx.config is None:
        return []
    from nvh.cli.setup import rename_retired_model, stale_default_models
    from nvh.providers.registry import RETIRED_PROVIDERS
    from nvh.providers.specs import PROVIDER_SPECS

    rows = []
    for pname, fld, model in stale_default_models(ctx.config.providers):
        if fld == "provider":
            rows.append(
                CheckResult(
                    title=f"Advisor {pname}",
                    status="warn",
                    detail=f"provider retired {RETIRED_PROVIDERS[pname]}",
                    fix="Run `nvh config migrate` to remove it.",
                )
            )
        else:
            rows.append(
                CheckResult(
                    title=f"Advisor {pname}: {fld}",
                    status="warn",
                    detail=f"'{model}' superseded by '{rename_retired_model(pname, model)}'",
                    fix="Run `nvh config migrate` to rewrite retired model IDs.",
                )
            )
    today = date.today()
    for name, pconfig in _configured_providers(ctx):
        spec = PROVIDER_SPECS.get(pconfig.type or name)
        if spec is None or not spec.sunset_date:
            continue
        days = (date.fromisoformat(spec.sunset_date) - today).days
        if days > SUNSET_WARNING_DAYS:
            continue
        what = spec.sunset_note or "API"
        when = (
            f"retired {spec.sunset_date}"
            if days < 0
            else f"retires {spec.sunset_date} ({days} day(s))"
        )
        rows.append(
            CheckResult(
                title=f"Advisor {name}",
                status="warn",
                detail=f"{what} {when}",
                fix=f"Disable the {name} advisor in config.yaml or wait for an adapter update.",
                data={"provider": name, "sunset_date": spec.sunset_date, "days": days},
            )
        )
    return rows


@check("ollama", "Ollama", GLANCE, DEEP)
def _ollama(ctx: CheckContext) -> CheckResult:
    models = ctx.ollama_models
    if models is None:
        return CheckResult(
            status="warn",
            detail=f"not reachable at {ollama_base_url()}",
            fix="Install from https://ollama.com or start with `ollama serve`",
            data={"models": []},
        )
    return CheckResult(
        status="pass", detail=f"detected, {len(models)} model(s)", data={"models": models}
    )


@check("ollama_models", "Ollama local models", DEEP)
def _ollama_local_models(ctx: CheckContext) -> CheckResult:
    models = ctx.ollama_models or []
    if models:
        return CheckResult(
            status="pass",
            detail=", ".join(models[:5]) + (" ..." if len(models) > 5 else ""),
        )
    return CheckResult(
        status="warn",
        detail="none found",
        fix="Pull the models for your GPU: `nvh models pull --recommended`",
    )


@check("ollama_required_models", "Ollama required models", DEEP)
def _ollama_required_models(ctx: CheckContext) -> CheckResult | None:
    if ctx.ollama_models is None or ctx.config is None:
        return None
    from nvh.utils.ollama import missing_models, required_ollama_models

    required = required_ollama_models(ctx.config)
    if not required:
        return None
    missing = missing_models(required, ctx.ollama_models)
    if missing:
        return CheckResult(
            status="warn",
            detail=f"{len(missing)}/{len(required)} missing: {', '.join(missing)}",
            fix="Pull missing: " + "; ".join(f"ollama pull {m}" for m in missing),
            data={"missing": missing, "required": required},
        )
    return CheckResult(status="pass", detail=f"all {len(required)} present: {', '.join(required)}")


@check("cache", "Cache", DEEP)
async def _cache(ctx: CheckContext) -> CheckResult | None:
    engine = await ctx.engine()
    if engine is None:
        return None
    stats = engine.cache.stats
    detail = f"{stats['entries']} entries / max {stats['max_size']}"
    if ctx.config.cache.enabled:
        return CheckResult(status="pass", detail=detail)
    return CheckResult(
        status="warn",
        detail="disabled in config",
        fix="Set cache.enabled: true in config to improve performance.",
    )


@check("disk", "Disk space", DEEP)
def _disk(ctx: CheckContext) -> CheckResult:
    usage = shutil.disk_usage(Path.home())
    free_gb = usage.free / (1024**3)
    data = {"free_gb": round(free_gb, 1), "total_gb": round(usage.total / (1024**3), 1)}
    if free_gb < 1.0:
        return CheckResult(
            status="fail",
            detail=f"{free_gb:.1f}GB free",
            fix="Free up disk space — less than 1GB available.",
            data=data,
        )
    if free_gb < 5.0:
        return CheckResult(
            status="warn",
            detail=f"{free_gb:.1f}GB free",
            fix="Disk space is low (< 5GB).",
            data=data,
        )
    return CheckResult(status="pass", detail=f"{free_gb:.1f}GB free", data=data)


@check("gpu", "GPU (nvidia-smi)", GLANCE, DEEP)
def _gpu(ctx: CheckContext) -> list[CheckResult]:
    from nvh.utils.gpu import get_gpu_summary, recommend_models

    gpus = ctx.gpus
    if not gpus:
        return [
            CheckResult(
                status="warn",
                detail="no NVIDIA GPU detected — Ollama will run in CPU mode",
                fix="Install NVIDIA drivers and nvidia-smi to enable GPU acceleration.",
                data={"gpus": []},
            )
        ]
    data = {
        "gpus": [
            {
                "index": g.index,
                "name": g.name,
                "vram_gb": g.vram_gb,
                "utilization_pct": g.utilization_pct,
                "driver_version": g.driver_version,
                "cuda_version": g.cuda_version,
            }
            for g in gpus
        ]
    }
    rows = [CheckResult(status="pass", detail=get_gpu_summary(gpus), data=data)]
    if len(gpus) > 1:
        rows.extend(
            CheckResult(
                title=f"  GPU {g.index}: {g.name}",
                status="pass",
                detail=f"{g.vram_gb:.1f} GB VRAM, driver {g.driver_version}",
            )
            for g in gpus
        )
    recs = recommend_models(gpus)
    rows.append(
        CheckResult(
            title="GPU model recommendations",
            status="pass",
            detail=(", ".join(r.model for r in recs) + f" — {recs[0].reason}") if recs else "none",
        )
    )
    return rows


@check("cloud_session", "Linux Desktop", GLANCE, DEEP)
def _cloud_session(ctx: CheckContext) -> CheckResult:
    from nvh.integrations.cloud_session import detect_cloud_session, format_cloud_status

    cloud = detect_cloud_session()
    if not cloud.is_cloud_session:
        return CheckResult(
            status="pass", detail="not detected (local / native)", data={"cloud": False}
        )
    tier = cloud.tier.capitalize() if cloud.tier else "Unknown"
    session = f" | Session: {cloud.session_id[:8]}..." if cloud.session_id else ""
    return CheckResult(
        status="pass",
        detail=f"{tier} tier — {cloud.gpu_class}{session}",
        data={"cloud": True, "summary": format_cloud_status(cloud)},
    )


@check("environment", "Environment", DEEP)
def _environment(ctx: CheckContext) -> list[CheckResult]:
    from nvh.utils.environment import detect_environment, get_environment_summary

    env = detect_environment()
    rows = [
        CheckResult(title="Environment: platform", status="pass", detail=env.platform),
        CheckResult(
            title="Environment: container",
            status="pass",
            detail="running inside Docker" if env.is_docker else "not in Docker (native)",
        ),
    ]
    if env.is_cloud:
        detail = env.cloud_provider
        if env.instance_type and env.instance_type != "unknown":
            detail += f" / {env.instance_type}"
        if env.public_ip:
            detail += f" / {env.public_ip}"
        rows.append(CheckResult(title="Environment: cloud", status="pass", detail=detail))
    else:
        rows.append(
            CheckResult(
                title="Environment: cloud", status="pass", detail="not detected (local / on-prem)"
            ),
        )
    if env.gpu_accessible:
        rows.append(
            CheckResult(
                title="Environment: GPU accessible",
                status="pass",
                detail=f"{env.gpu_count} GPU(s) accessible from this process",
            )
        )
    elif env.has_gpu:
        rows.append(
            CheckResult(
                title="Environment: GPU accessible",
                status="warn",
                detail="GPU detected but not accessible (container config?)",
                fix="Add --gpus all to docker run, or configure NVIDIA Container Toolkit.",
            )
        )
    else:
        rows.append(
            CheckResult(
                title="Environment: GPU accessible",
                status="pass",
                detail="no GPU present (CPU mode)",
            ),
        )
    if env.has_root:
        rows.append(
            CheckResult(
                title="Environment: root access",
                status="warn",
                detail="running as root",
                fix="Consider running as a non-root user for improved security.",
            )
        )
    else:
        rows.append(
            CheckResult(
                title="Environment: root access", status="pass", detail="non-root user (good)"
            )
        )
    rows.append(
        CheckResult(title="Environment summary", status="info", detail=get_environment_summary(env))
    )
    return rows


@check("sandbox", "Sandbox isolation", DEEP)
async def _sandbox(ctx: CheckContext) -> CheckResult:
    from nvh.sandbox.executor import (
        REQUIRE_DOCKER_ENV,
        SandboxConfig,
        docker_available,
        require_docker_source,
    )

    required = SandboxConfig().require_docker
    source = require_docker_source() or (
        f"SandboxConfig.require_docker / {REQUIRE_DOCKER_ENV}" if required else ""
    )
    try:
        docker = await asyncio.wait_for(docker_available(), timeout=10)
    except Exception:
        docker = False
    data = {"docker": docker, "require_docker": required, "source": source}
    if docker:
        return CheckResult(
            status="pass", detail="Docker available — run_code/shell run isolated", data=data
        )
    if required:
        return CheckResult(
            status="warn",
            detail=f"isolation required but Docker unavailable — run_code/shell will refuse ({source})",
            fix=f"Start Docker (rootless works) or unset {source}",
            data=data,
        )
    return CheckResult(
        status="info",
        detail="Docker unavailable — run_code/shell fall back to an unisolated subprocess",
        fix=f"Set {REQUIRE_DOCKER_ENV}=1 to fail closed instead",
        data=data,
    )


@check("nvh_on_path", "nvh on PATH", DEEP)
def _nvh_on_path(ctx: CheckContext) -> CheckResult:
    from nvh.cli.setup import _check_nvh_on_path

    hint = _check_nvh_on_path()
    if hint is None:
        return CheckResult(status="pass", detail="reachable")
    kind = hint["env_kind"]
    if kind in ("conda", "mamba") and hint["env_name"]:
        return CheckResult(
            status="warn",
            detail=f"installed in {kind} env '{hint['env_name']}' but not activated",
            fix=f"Activate the env: {hint['activate_cmd']}",
        )
    if kind == "venv" and hint["activate_cmd"]:
        return CheckResult(
            status="warn",
            detail=f"installed in venv '{hint['env_name']}' but not activated",
            fix=f"Activate the venv: {hint['activate_cmd']}",
        )
    return CheckResult(
        status="warn",
        detail=f"binary at {hint['full_path']} is not on PATH",
        fix=f'Add to PATH: export PATH="{hint["bin_dir"]}:$PATH"',
    )


@check("budget", "Budget", GLANCE)
async def _budget(ctx: CheckContext) -> list[CheckResult]:
    engine = await ctx.engine()
    if engine is None:
        return [CheckResult(status="warn", detail="unavailable")]
    b = _jsonable(await engine.get_budget_status())
    daily = (
        f"${b['daily_spend']:.2f} / ${b['daily_limit']:.2f} daily"
        if b["daily_limit"] > 0
        else f"${b['daily_spend']:.2f} spent today"
    )
    monthly = (
        f"${b['monthly_spend']:.2f} / ${b['monthly_limit']:.2f} monthly"
        if b["monthly_limit"] > 0
        else f"${b['monthly_spend']:.2f} spent this month"
    )
    rows = [CheckResult(status="info", detail=f"{daily} | {monthly}", data=b)]
    local_q = b.get("local_queries", 0)
    monthly_q = b.get("monthly_queries", 0)
    if monthly_q > 0 and local_q > 0:
        # Rough estimate: average cloud query cost × local query count.
        avg_cloud = (
            b["monthly_spend"] / max(monthly_q - local_q, 1) if monthly_q > local_q else 0.002
        )
        rows.append(
            CheckResult(
                id="savings",
                title="Savings",
                status="info",
                detail=f"${avg_cloud * local_q:.2f} saved this month ({local_q} local queries)",
            )
        )
    return rows


@check("services", "Services", GLANCE)
def _services(ctx: CheckContext) -> CheckResult:
    from nvh.cli.services import snapshot

    snap = snapshot(ollama_models=ctx.ollama_models)
    states = snap.as_list()
    detail = " · ".join(f"{s.name} :{s.port} {s.status_label}" for s in states)
    return CheckResult(
        status="pass" if snap.all_healthy() else "warn",
        detail=detail,
        fix=""
        if snap.all_healthy()
        else "Start the pipeline with `nvh webui` or `nvh services start`",
        data={
            "services": [
                {
                    "name": s.name,
                    "port": s.port,
                    "running": s.running,
                    "healthy": s.healthy,
                    "detail": s.detail,
                }
                for s in states
            ]
        },
    )


@check("dependencies", "Dependencies", REPORT)
def _dependencies(ctx: CheckContext) -> list[CheckResult]:
    import importlib

    from nvh.integrations.diagnostics.compatibility import _command_version

    rows = []
    for pkg in ("litellm", "fastapi", "rich", "typer", "pydantic", "httpx", "keyring", "tiktoken"):
        try:
            ver = getattr(importlib.import_module(pkg), "__version__", "?")
            rows.append(CheckResult(title=f"Package {pkg}", status="pass", detail=str(ver)))
        except ImportError:
            rows.append(
                CheckResult(
                    title=f"Package {pkg}",
                    status="warn",
                    detail="missing",
                    fix=f"pip install {pkg}",
                ),
            )
    driver = ctx.gpus[0].driver_version if ctx.gpus else ""
    rows.append(
        CheckResult(
            title="NVIDIA driver", status="pass" if driver else "warn", detail=driver or "not found"
        ),
    )
    for tool, args, note in (
        ("docker", ("--version",), ""),
        ("git", ("--version",), ""),
        ("ollama", ("--version",), "install with `nvh models pull --recommended`"),
        ("pdftotext", ("-v",), "optional, for PDF ingestion"),
    ):
        ver = _command_version(tool, *args)
        if not ver and shutil.which(tool):
            ver = "found"
        if ver:
            rows.append(CheckResult(title=f"Tool {tool}", status="pass", detail=ver))
        else:
            missing = f"not found ({note})" if note else "not found"
            rows.append(
                CheckResult(title=f"Tool {tool}", status="info" if note else "warn", detail=missing)
            )
    return rows


@check("free_tier", "Free-tier advisors", REPORT)
def _free_tier(ctx: CheckContext) -> CheckResult:
    from nvh.core.free_tier import detect_available_free_advisors

    names = [a.name for a in detect_available_free_advisors()]
    return CheckResult(
        status="info", detail=", ".join(names) or "none detected", data={"available": names}
    )


@check("rag_index", "RAG index", REPORT)
def _rag_index(ctx: CheckContext) -> CheckResult:
    from nvh.integrations.rag import list_collections

    collections = list_collections()
    detail = ", ".join(f"{c['name']} ({c['chunks']} chunks)" for c in collections[:5]) or "empty"
    return CheckResult(status="info", detail=f"{len(collections)} collection(s): {detail}")


@check("vault", "Vault", REPORT)
def _vault(ctx: CheckContext) -> CheckResult:
    from nvh.integrations.workspace.vault import vault_status

    vault = vault_status()
    return CheckResult(
        status="info",
        detail=f"initialized={vault['initialized']}, {vault['markdown_files']} note(s)",
    )


@check("tools", "Tools", REPORT)
def _tools(ctx: CheckContext) -> CheckResult:
    from nvh.core.tools import ToolRegistry

    names = [t.name for t in ToolRegistry().list_tools()]
    return CheckResult(status="info", detail=f"{len(names)} registered", data={"tools": names})


@check("scheduler", "Scheduler", REPORT)
def _scheduler(ctx: CheckContext) -> CheckResult:
    from nvh.core.scheduler import Scheduler

    return CheckResult(status="info", detail=f"{len(Scheduler().list_tasks())} task(s)")


@check("network", "Network", REPORT)
def _network(ctx: CheckContext) -> list[CheckResult]:
    import httpx

    rows = []
    for url, name in (
        ("https://api.groq.com", "Groq API"),
        ("https://html.duckduckgo.com", "DuckDuckGo"),
        ("https://ollama.com", "Ollama.com"),
    ):
        try:
            resp = httpx.head(url, timeout=5, follow_redirects=True)
            rows.append(
                CheckResult(
                    title=f"Network: {name}",
                    status="pass",
                    detail=f"reachable ({resp.status_code})",
                ),
            )
        except Exception:
            rows.append(CheckResult(title=f"Network: {name}", status="warn", detail="unreachable"))
    return rows


@check("routing_probe", "Routing probe", REPORT)
async def _routing_probe(ctx: CheckContext) -> CheckResult:
    engine = await ctx.engine()
    if engine is None or not ctx.enabled_providers:
        return CheckResult(status="warn", detail="no advisors — cannot test routing")
    decision = engine.router.route("test query hello world")
    return CheckResult(
        status="pass",
        detail=(
            f"{decision.provider}/{decision.model} "
            f"({decision.task_type.value}, confidence {decision.confidence:.2f})"
        ),
        data={"provider": decision.provider, "model": decision.model, "reason": decision.reason},
    )


# ---------------------------------------------------------------------------
# API smoke checks — exercise a running server; skip cleanly when none listens.
# ---------------------------------------------------------------------------


async def _api_request(
    ctx: CheckContext,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> tuple[Any, CheckResult | None]:
    """``(payload, None)`` on 2xx, else ``(None, skip/fail row)``."""
    probe = await ctx.api()
    if not probe.reachable:
        return None, CheckResult(
            status="skip",
            detail=f"no API listening at {probe.url}",
            data={"url": probe.url},
        )
    import httpx

    try:
        async with httpx.AsyncClient(timeout=ctx.api_timeout) as client:
            resp = await client.request(
                method, f"{probe.url}{path}", json=body, headers=probe.headers
            )
    except Exception as exc:
        return None, CheckResult(
            status="fail",
            detail=f"{method} {path}: {exc or type(exc).__name__}",
            data={"url": probe.url},
        )
    if resp.status_code >= 400:
        fix = "Set HIVE_API_KEY to the server's key" if resp.status_code in (401, 403) else ""
        return None, CheckResult(
            status="fail",
            detail=f"{method} {path}: HTTP {resp.status_code} {resp.text[:120]}",
            fix=fix,
            data={"url": probe.url, "status_code": resp.status_code},
        )
    try:
        return _unwrap(resp.json()), None
    except ValueError:
        return None, CheckResult(status="fail", detail=f"{method} {path}: non-JSON response")


@check("api_health", "API /v1/health", SMOKE, REPORT)
async def _api_health(ctx: CheckContext) -> CheckResult:
    probe = await ctx.api()
    if not probe.reachable:
        return CheckResult(
            status="skip", detail=f"no API listening at {probe.url}", data={"url": probe.url}
        )
    if probe.error:
        return CheckResult(status="fail", detail=probe.error, data={"url": probe.url})
    return CheckResult(
        status="pass",
        detail=f"{probe.url} · {probe.health.get('providers_enabled', '?')} advisor(s) enabled",
        data={"url": probe.url, **probe.health},
    )


@check("api_advisors", "API /v1/advisors", SMOKE, REPORT)
async def _api_advisors(ctx: CheckContext) -> CheckResult:
    payload, row = await _api_request(ctx, "GET", "/v1/advisors")
    if row is not None:
        return row
    items = payload if isinstance(payload, list) else (payload or {}).get("advisors") or []
    healthy = sum(1 for a in items if isinstance(a, dict) and a.get("healthy"))
    return CheckResult(
        status="pass" if items else "warn",
        detail=f"{healthy}/{len(items)} healthy" if items else "none configured",
        data={"advisors": items},
    )


@check("api_proxy_health", "API /v1/proxy/health", SMOKE, REPORT)
async def _api_proxy_health(ctx: CheckContext) -> CheckResult:
    payload, row = await _api_request(ctx, "GET", "/v1/proxy/health")
    if row is not None:
        return row
    payload = payload or {}
    return CheckResult(
        status="pass",
        detail=(
            f"{payload.get('providers_enabled', '?')} provider(s), "
            f"local inference {'yes' if payload.get('has_local_inference') else 'no'}"
        ),
        data=payload,
    )


@check("api_quota", "API /v1/quota", SMOKE, REPORT)
async def _api_quota(ctx: CheckContext) -> CheckResult:
    payload, row = await _api_request(ctx, "GET", "/v1/quota")
    if row is not None:
        return row
    quotas = payload if isinstance(payload, list) else (payload or {}).get("quotas") or []
    return CheckResult(
        status="pass", detail=f"{len(quotas)} provider quota(s)", data={"quotas": quotas}
    )


@check("api_query", "API POST /v1/query", SMOKE, REPORT)
async def _api_query(ctx: CheckContext) -> CheckResult:
    payload, row = await _api_request(
        ctx, "POST", "/v1/query", {"prompt": _SMOKE_PROMPT, "max_tokens": 8}
    )
    if row is not None:
        return row
    payload = payload or {}
    return CheckResult(
        status="pass",
        detail=f"{payload.get('provider', '?')}/{payload.get('model', '?')} in {payload.get('latency_ms', '?')}ms",
        data={k: payload.get(k) for k in ("provider", "model", "latency_ms", "cost_usd")},
    )


@check("api_proxy_chat", "API POST /v1/proxy/chat/completions", SMOKE, REPORT)
async def _api_proxy_chat(ctx: CheckContext) -> CheckResult:
    payload, row = await _api_request(
        ctx,
        "POST",
        "/v1/proxy/chat/completions",
        {
            "model": "auto",
            "messages": [{"role": "user", "content": _SMOKE_PROMPT}],
            "max_tokens": 8,
        },
    )
    if row is not None:
        return row
    payload = payload or {}
    choices = payload.get("choices") or []
    return CheckResult(
        status="pass" if choices else "warn",
        detail=f"model {payload.get('model', '?')}, {len(choices)} choice(s)",
        data={"model": payload.get("model"), "choices": len(choices)},
    )


__all__ = [
    "DEEP",
    "GLANCE",
    "PROVIDERS",
    "REGISTRY",
    "REPORT",
    "SMOKE",
    "TIERS",
    "ApiProbe",
    "Check",
    "CheckContext",
    "CheckResult",
    "check",
    "checks_for",
    "nvh_version",
    "platform_summary",
    "probe_ollama_models",
    "run_check",
    "run_checks",
    "run_checks_sync",
    "summarize",
]
