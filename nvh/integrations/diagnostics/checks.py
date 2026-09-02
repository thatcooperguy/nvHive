"""One registry of local health checks.

``nvh status`` renders a tier of it (glance / providers / deep / report) and
``/v1/setup/diagnostics`` can embed the same rows. Every probe that more than
one check needs (config, an initialised Engine, the Ollama tag list) runs
once per ``CheckContext``.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GLANCE = "glance"
PROVIDERS = "providers"
DEEP = "deep"
REPORT = "report"
TIERS = (GLANCE, PROVIDERS, DEEP, REPORT)

OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"

_UNSET = object()


@dataclass
class CheckResult:
    id: str
    title: str
    status: str  # pass | warn | fail | info
    detail: str = ""
    fix: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        # ``check`` first: the WebUI and CI parsers split the JSON on it.
        row: dict[str, Any] = {
            "check": self.title, "status": self.status,
            "detail": self.detail, "fix": self.fix, "id": self.id,
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
    try:
        import httpx

        resp = httpx.get(OLLAMA_TAGS_URL, timeout=timeout)
        if resp.status_code != 200:
            return None
        return [m.get("name", "") for m in resp.json().get("models", [])]
    except Exception:
        return None


class CheckContext:
    def __init__(
        self,
        *,
        home_dir: str | None = None,
        min_free_gb: float = 200.0,
        health_timeout: float = 10.0,
    ) -> None:
        self.home_dir = home_dir
        self.min_free_gb = min_free_gb
        self.health_timeout = health_timeout
        self.config_error: str | None = None
        self.enabled_providers: list[str] = []
        self._config: Any = _UNSET
        self._engine: Any = None
        self._ollama: Any = _UNSET

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

    async def engine(self) -> Any:
        """Initialised Engine, or None when the config did not load."""
        if self._engine is None and self.config is not None:
            from nvh.core.engine import Engine

            engine = Engine(config=self.config)
            self.enabled_providers = list(await engine.initialize())
            self._engine = engine
        return self._engine

    @property
    def ollama_models(self) -> list[str] | None:
        if self._ollama is _UNSET:
            self._ollama = probe_ollama_models()
        return self._ollama

    def reset_ollama(self) -> None:
        self._ollama = _UNSET


async def run_check(check: Check, ctx: CheckContext) -> list[CheckResult]:
    try:
        out = check.run(ctx)
        if inspect.isawaitable(out):
            out = await out
    except Exception as exc:
        return [CheckResult(check.id, check.title, "warn", f"check failed: {exc}")]
    if out is None:
        return []
    return list(out) if isinstance(out, list) else [out]


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
    return asyncio.run(run_checks(tier, ctx))


def summarize(results: list[CheckResult]) -> dict[str, Any]:
    counts = {s: sum(1 for r in results if r.status == s) for s in ("pass", "warn", "fail")}
    return {
        "total": len(results),
        "passed": counts["pass"],
        "warned": counts["warn"],
        "failed": counts["fail"],
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
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


# ---------------------------------------------------------------------------
# Checks — registration order is display order.
# ---------------------------------------------------------------------------


@check("python", "Python version", DEEP)
def _python(ctx: CheckContext) -> CheckResult:
    v = sys.version_info
    text = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 11):
        return CheckResult("python", "Python version", "pass", text)
    return CheckResult(
        "python", "Python version", "fail", f"{text} (need >= 3.11)",
        "Upgrade Python to 3.11+: https://python.org/downloads",
    )


@check("system", "System", REPORT)
def _system(ctx: CheckContext) -> CheckResult:
    data = {
        "platform": platform.platform(),
        "python": sys.version,
        "executable": sys.executable,
        "nvh": nvh_version(),
        "cwd": os.getcwd(),
        "home": str(Path.home()),
        "user": os.environ.get("USER") or os.environ.get("USERNAME", "unknown"),
        "shell": os.environ.get("SHELL", "unknown"),
    }
    return CheckResult("system", "System", "info", f"{data['platform']} · nvh {data['nvh']}", data=data)


@check("storage", "Rootless storage", DEEP)
def _storage(ctx: CheckContext) -> CheckResult:
    from nvh.integrations.workspace.storage import storage_status

    status = storage_status(home_dir=ctx.home_dir, min_free_gb=ctx.min_free_gb)
    free = status.free_gb if status.free_gb is not None else "?"
    detail = f"{status.layout.home} ({free} GB free)"
    if status.ok and status.configured_by != "default":
        return CheckResult("storage", "Rootless storage", "pass", detail, data=status.as_dict())
    return CheckResult(
        "storage", "Rootless storage", "warn",
        "; ".join(status.warnings) or detail,
        "Run `nvh status --deep --storage --home-dir /path/on/mounted/volume/nvhive`",
        data=status.as_dict(),
    )


@check("legacy_knowledge", "Legacy knowledge base", DEEP)
def _legacy_knowledge(ctx: CheckContext) -> CheckResult | None:
    from nvh.integrations.rag import legacy_knowledge_status

    legacy = legacy_knowledge_status(home_dir=ctx.home_dir)
    if legacy["found"] and not legacy["imported"]:
        return CheckResult(
            "legacy_knowledge", "Legacy knowledge base", "warn",
            f"{legacy['documents']} document(s) in {legacy['path']} are not in the RAG index",
            "Run `nvh rag import-legacy` once (needs Ollama for embeddings)",
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
            "receipts", "Install receipts", "warn", detail,
            "Open the setup wizard or rerun the matching `nvh studio` / `nvh workstation` command.",
        )
    return CheckResult("receipts", "Install receipts", "pass", detail)


@check("catalog", "Setup catalog", DEEP)
def _catalog(ctx: CheckContext) -> CheckResult:
    from nvh.integrations.setup_catalog import catalog_status

    catalog = catalog_status(refresh=False)
    detail = (
        f"{catalog.get('source')} catalog, {catalog.get('profile_count', 0)} profiles, "
        f"{catalog.get('model_count', 0)} models"
    )
    if catalog.get("error"):
        return CheckResult("catalog", "Setup catalog", "warn", f"{detail}; {catalog['error']}")
    return CheckResult("catalog", "Setup catalog", "pass", detail)


@check("config", "Config file", DEEP)
def _config(ctx: CheckContext) -> list[CheckResult]:
    from nvh.config.settings import DEFAULT_CONFIG_PATH

    path = DEFAULT_CONFIG_PATH
    if not path.exists():
        return [CheckResult(
            "config", "Config file exists", "fail", str(path),
            "Run `nvh config init` to create a configuration file.",
        )]
    try:
        import yaml

        yaml.safe_load(path.read_text())
    except Exception as exc:
        return [CheckResult("config", "Config file (YAML)", "fail", str(exc), f"Fix YAML syntax in {path}")]
    rows = [CheckResult("config", "Config file (YAML)", "pass", str(path))]
    if ctx.config is not None:
        rows.append(CheckResult("config", "Config schema (Pydantic)", "pass", "HiveConfig validated successfully"))
    else:
        rows.append(CheckResult(
            "config", "Config schema (Pydantic)", "fail",
            ctx.config_error or "failed to load", f"Fix config errors in {path}",
        ))
    return rows


@check("database", "Database", DEEP)
async def _database(ctx: CheckContext) -> CheckResult:
    try:
        from nvh.storage import repository as repo

        await repo.init_db()
    except Exception as exc:
        return CheckResult(
            "database", "Database", "fail", str(exc),
            "Check storage permissions or reinstall: pip install nvhive",
        )
    return CheckResult("database", "Database", "pass", "init_db succeeded")


def _configured_providers(ctx: CheckContext) -> list[tuple[str, Any]]:
    if ctx.config is None:
        return []
    return [(n, p) for n, p in ctx.config.providers.items() if p.enabled]


def _has_api_key(name: str, pconfig: Any) -> bool:
    if name == "ollama":
        return True
    if pconfig.api_key and not str(pconfig.api_key).startswith("${"):
        return True
    if os.environ.get(f"{name.upper()}_API_KEY") or os.environ.get(f"HIVE_{name.upper()}_API_KEY"):
        return True
    try:
        import keyring

        return bool(keyring.get_password("nvhive", f"{name}_api_key"))
    except Exception:
        return False


@check("provider_keys", "Advisor API keys", DEEP)
def _provider_keys(ctx: CheckContext) -> list[CheckResult]:
    rows = []
    for name, pconfig in _configured_providers(ctx):
        if _has_api_key(name, pconfig):
            rows.append(CheckResult("provider_keys", f"Advisor {name}: API key", "pass", "found", data={"provider": name}))
        else:
            rows.append(CheckResult(
                "provider_keys", f"Advisor {name}: API key", "fail", "missing",
                f"Run `nvh advisor login {name}` or set {name.upper()}_API_KEY",
                data={"provider": name},
            ))
    return rows


@check("provider_health", "Advisor health", GLANCE, PROVIDERS, DEEP)
async def _provider_health(ctx: CheckContext) -> list[CheckResult]:
    engine = await ctx.engine()
    if engine is None:
        return [CheckResult(
            "provider_health", "Advisors", "fail",
            ctx.config_error or "config did not load", "Run `nvh setup`",
        )]
    # Config-enabled plus whatever initialize() auto-enabled from env keys.
    names = sorted({name for name, _ in _configured_providers(ctx)} | set(ctx.enabled_providers))
    if not names:
        return [CheckResult(
            "provider_health", "Advisors", "warn", "none enabled",
            "Run `nvh setup` to add providers",
        )]
    primary = ctx.config.defaults.provider or (ctx.enabled_providers or names)[0]
    try:
        chain = list(engine._get_fallback_chain(primary))
    except Exception:
        chain = []
    rows = []
    for name in names:
        title = f"Advisor {name}: health check"
        data: dict[str, Any] = {"provider": name, "healthy": False, "score": 0.5, "chain_position": 0}
        if not engine.registry.has(name):
            rows.append(CheckResult(
                "provider_health", title, "warn", "not registered (check API key)",
                f"Run `nvh advisor login {name}`", data=data,
            ))
            continue
        try:
            health = await asyncio.wait_for(
                engine.registry.get(name).health_check(), timeout=ctx.health_timeout,
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
        rows.append(CheckResult(
            "provider_health", title, "pass" if ok else "warn", detail,
            "" if ok else f"Check your {name} API key and network access.", data=data,
        ))
    return rows


@check("fallback_chain", "Fallback chain", PROVIDERS)
async def _fallback_chain(ctx: CheckContext) -> CheckResult | None:
    engine = await ctx.engine()
    if engine is None or not ctx.enabled_providers:
        return None
    primary = ctx.config.defaults.provider or ctx.enabled_providers[0]
    chain = list(engine._get_fallback_chain(primary))
    return CheckResult("fallback_chain", "Fallback chain", "info", " → ".join(chain[:5]), data={"chain": chain})


@check("retired_models", "Retired models", DEEP)
def _retired_models(ctx: CheckContext) -> list[CheckResult]:
    if ctx.config is None:
        return []
    from nvh.cli.setup import RETIRED_PROVIDERS, rename_retired_model, stale_default_models

    rows = []
    for pname, fld, model in stale_default_models(ctx.config.providers):
        if fld == "provider":
            rows.append(CheckResult(
                "retired_models", f"Advisor {pname}", "warn",
                f"provider retired {RETIRED_PROVIDERS[pname]}",
                "Run `nvh config migrate` to remove it.",
            ))
        else:
            rows.append(CheckResult(
                "retired_models", f"Advisor {pname}: {fld}", "warn",
                f"'{model}' superseded by '{rename_retired_model(pname, model)}'",
                "Run `nvh config migrate` to rewrite retired model IDs.",
            ))
    return rows


@check("ollama", "Ollama", GLANCE, DEEP)
def _ollama(ctx: CheckContext) -> CheckResult:
    models = ctx.ollama_models
    if models is None:
        return CheckResult(
            "ollama", "Ollama", "warn", "not reachable at localhost:11434",
            "Install from https://ollama.com or start with `ollama serve`",
            data={"models": []},
        )
    return CheckResult("ollama", "Ollama", "pass", f"detected, {len(models)} model(s)", data={"models": models})


@check("ollama_models", "Ollama local models", DEEP)
def _ollama_local_models(ctx: CheckContext) -> list[CheckResult]:
    models = ctx.ollama_models or []
    if models:
        rows = [CheckResult(
            "ollama_models", "Ollama local models", "pass",
            ", ".join(models[:5]) + (" ..." if len(models) > 5 else ""),
        )]
    else:
        rows = [CheckResult(
            "ollama_models", "Ollama local models", "warn", "none found",
            "Pull the models for your GPU: `nvh models pull --recommended`",
        )]
    if ctx.ollama_models is not None and ctx.config is not None:
        from nvh.utils.ollama import missing_models, required_ollama_models

        required = required_ollama_models(ctx.config)
        if required:
            missing = missing_models(required, models)
            if missing:
                rows.append(CheckResult(
                    "ollama_required_models", "Ollama required models", "warn",
                    f"{len(missing)}/{len(required)} missing: {', '.join(missing)}",
                    "Pull missing: " + "; ".join(f"ollama pull {m}" for m in missing),
                    data={"missing": missing, "required": required},
                ))
            else:
                rows.append(CheckResult(
                    "ollama_required_models", "Ollama required models", "pass",
                    f"all {len(required)} present: {', '.join(required)}",
                ))
    return rows


@check("cache", "Cache", DEEP)
def _cache(ctx: CheckContext) -> CheckResult | None:
    if ctx.config is None:
        return None
    from nvh.core.engine import Engine

    stats = Engine(config=ctx.config).cache.stats
    detail = f"{stats['entries']} entries / max {stats['max_size']}"
    if ctx.config.cache.enabled:
        return CheckResult("cache", "Cache", "pass", detail)
    return CheckResult(
        "cache", "Cache", "warn", "disabled in config",
        "Set cache.enabled: true in config to improve performance.",
    )


@check("disk", "Disk space", DEEP)
def _disk(ctx: CheckContext) -> CheckResult:
    usage = shutil.disk_usage(Path.home())
    free_gb = usage.free / (1024 ** 3)
    data = {"free_gb": round(free_gb, 1), "total_gb": round(usage.total / (1024 ** 3), 1)}
    if free_gb < 1.0:
        return CheckResult("disk", "Disk space", "fail", f"{free_gb:.1f}GB free", "Free up disk space — less than 1GB available.", data=data)
    if free_gb < 5.0:
        return CheckResult("disk", "Disk space", "warn", f"{free_gb:.1f}GB free", "Disk space is low (< 5GB).", data=data)
    return CheckResult("disk", "Disk space", "pass", f"{free_gb:.1f}GB free", data=data)


@check("gpu", "GPU (nvidia-smi)", GLANCE, DEEP)
def _gpu(ctx: CheckContext) -> list[CheckResult]:
    from nvh.utils.gpu import detect_gpus, get_gpu_summary, recommend_models

    gpus = detect_gpus()
    if not gpus:
        return [CheckResult(
            "gpu", "GPU (nvidia-smi)", "warn",
            "no NVIDIA GPU detected — Ollama will run in CPU mode",
            "Install NVIDIA drivers and nvidia-smi to enable GPU acceleration.",
            data={"gpus": []},
        )]
    data = {"gpus": [
        {"index": g.index, "name": g.name, "vram_gb": g.vram_gb, "utilization_pct": g.utilization_pct,
         "driver_version": g.driver_version, "cuda_version": g.cuda_version}
        for g in gpus
    ]}
    rows = [CheckResult("gpu", "GPU (nvidia-smi)", "pass", get_gpu_summary(), data=data)]
    if len(gpus) > 1:
        rows.extend(
            CheckResult("gpu", f"  GPU {g.index}: {g.name}", "pass", f"{g.vram_gb:.1f} GB VRAM, driver {g.driver_version}")
            for g in gpus
        )
    recs = recommend_models(gpus)
    rows.append(CheckResult(
        "gpu", "GPU model recommendations", "pass",
        (", ".join(r.model for r in recs) + f" — {recs[0].reason}") if recs else "none",
    ))
    return rows


@check("cloud_session", "Linux Desktop", GLANCE, DEEP)
def _cloud_session(ctx: CheckContext) -> CheckResult:
    from nvh.integrations.cloud_session import detect_cloud_session, format_cloud_status

    cloud = detect_cloud_session()
    if not cloud.is_cloud_session:
        return CheckResult("cloud_session", "Linux Desktop", "pass", "not detected (local / native)", data={"cloud": False})
    tier = cloud.tier.capitalize() if cloud.tier else "Unknown"
    session = f" | Session: {cloud.session_id[:8]}..." if cloud.session_id else ""
    return CheckResult(
        "cloud_session", "Linux Desktop", "pass", f"{tier} tier — {cloud.gpu_class}{session}",
        data={"cloud": True, "summary": format_cloud_status(cloud)},
    )


@check("environment", "Environment", DEEP)
def _environment(ctx: CheckContext) -> list[CheckResult]:
    from nvh.utils.environment import detect_environment, get_environment_summary

    env = detect_environment()
    rows = [
        CheckResult("environment", "Environment: platform", "pass", env.platform),
        CheckResult(
            "environment", "Environment: container", "pass",
            "running inside Docker" if env.is_docker else "not in Docker (native)",
        ),
    ]
    if env.is_cloud:
        detail = env.cloud_provider
        if env.instance_type and env.instance_type != "unknown":
            detail += f" / {env.instance_type}"
        if env.public_ip:
            detail += f" / {env.public_ip}"
        rows.append(CheckResult("environment", "Environment: cloud", "pass", detail))
    else:
        rows.append(CheckResult("environment", "Environment: cloud", "pass", "not detected (local / on-prem)"))
    if env.gpu_accessible:
        rows.append(CheckResult("environment", "Environment: GPU accessible", "pass", f"{env.gpu_count} GPU(s) accessible from this process"))
    elif env.has_gpu:
        rows.append(CheckResult(
            "environment", "Environment: GPU accessible", "warn",
            "GPU detected but not accessible (container config?)",
            "Add --gpus all to docker run, or configure NVIDIA Container Toolkit.",
        ))
    else:
        rows.append(CheckResult("environment", "Environment: GPU accessible", "pass", "no GPU present (CPU mode)"))
    if env.has_root:
        rows.append(CheckResult(
            "environment", "Environment: root access", "warn", "running as root",
            "Consider running as a non-root user for improved security.",
        ))
    else:
        rows.append(CheckResult("environment", "Environment: root access", "pass", "non-root user (good)"))
    rows.append(CheckResult("environment", "Environment summary", "info", get_environment_summary(env)))
    return rows


@check("nvh_on_path", "nvh on PATH", DEEP)
def _nvh_on_path(ctx: CheckContext) -> CheckResult:
    from nvh.cli.setup import _check_nvh_on_path

    hint = _check_nvh_on_path()
    if hint is None:
        return CheckResult("nvh_on_path", "nvh on PATH", "pass", "reachable")
    kind = hint["env_kind"]
    if kind in ("conda", "mamba") and hint["env_name"]:
        return CheckResult(
            "nvh_on_path", "nvh on PATH", "warn",
            f"installed in {kind} env '{hint['env_name']}' but not activated",
            f"Activate the env: {hint['activate_cmd']}",
        )
    if kind == "venv" and hint["activate_cmd"]:
        return CheckResult(
            "nvh_on_path", "nvh on PATH", "warn",
            f"installed in venv '{hint['env_name']}' but not activated",
            f"Activate the venv: {hint['activate_cmd']}",
        )
    return CheckResult(
        "nvh_on_path", "nvh on PATH", "warn",
        f"binary at {hint['full_path']} is not on PATH",
        f"Add to PATH: export PATH=\"{hint['bin_dir']}:$PATH\"",
    )


@check("budget", "Budget", GLANCE)
async def _budget(ctx: CheckContext) -> list[CheckResult]:
    engine = await ctx.engine()
    if engine is None:
        return [CheckResult("budget", "Budget", "warn", "unavailable")]
    b = await engine.get_budget_status()
    daily = (
        f"${b['daily_spend']:.2f} / ${b['daily_limit']:.2f} daily"
        if b["daily_limit"] > 0 else f"${b['daily_spend']:.2f} spent today"
    )
    monthly = (
        f"${b['monthly_spend']:.2f} / ${b['monthly_limit']:.2f} monthly"
        if b["monthly_limit"] > 0 else f"${b['monthly_spend']:.2f} spent this month"
    )
    rows = [CheckResult("budget", "Budget", "info", f"{daily} | {monthly}", data=dict(b))]
    local_q = b.get("local_queries", 0)
    monthly_q = b.get("monthly_queries", 0)
    if monthly_q > 0 and local_q > 0:
        # Rough estimate: average cloud query cost × local query count.
        avg_cloud = (
            float(b["monthly_spend"]) / max(monthly_q - local_q, 1)
            if monthly_q > local_q else 0.002
        )
        rows.append(CheckResult(
            "savings", "Savings", "info",
            f"${avg_cloud * local_q:.2f} saved this month ({local_q} local queries)",
        ))
    return rows


@check("services", "Services", GLANCE)
def _services(ctx: CheckContext) -> CheckResult:
    from nvh.cli.services import snapshot

    snap = snapshot()
    states = snap.as_list()
    detail = " · ".join(f"{s.name} :{s.port} {s.status_label}" for s in states)
    return CheckResult(
        "services", "Services", "pass" if snap.all_healthy() else "warn", detail,
        "" if snap.all_healthy() else "Start the pipeline with `nvh webui` or `nvh services start`",
        data={"services": [
            {"name": s.name, "port": s.port, "running": s.running, "healthy": s.healthy, "detail": s.detail}
            for s in states
        ]},
    )


def _tool_version(argv: list[str], timeout: float = 5.0) -> str | None:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    return result.stdout.strip().splitlines()[0] if result.returncode == 0 and result.stdout.strip() else None


@check("dependencies", "Dependencies", REPORT)
def _dependencies(ctx: CheckContext) -> list[CheckResult]:
    import importlib

    rows = []
    for pkg in ("litellm", "fastapi", "rich", "typer", "pydantic", "httpx", "keyring", "tiktoken"):
        try:
            ver = getattr(importlib.import_module(pkg), "__version__", "?")
            rows.append(CheckResult("dependencies", f"Package {pkg}", "pass", str(ver)))
        except ImportError:
            rows.append(CheckResult("dependencies", f"Package {pkg}", "warn", "missing", f"pip install {pkg}"))
    driver = _tool_version(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"])
    rows.append(CheckResult("dependencies", "NVIDIA driver", "pass" if driver else "warn", driver or "not found"))
    for tool, argv, note in (
        ("docker", ["docker", "--version"], ""),
        ("git", ["git", "--version"], ""),
        ("ollama", ["ollama", "--version"], "install with `nvh models pull --recommended`"),
        ("pdftotext", ["pdftotext", "-v"], "optional, for PDF ingestion"),
    ):
        ver = _tool_version(argv)
        if ver is None and shutil.which(tool):
            ver = "found"
        if ver:
            rows.append(CheckResult("dependencies", f"Tool {tool}", "pass", ver))
        else:
            missing = f"not found ({note})" if note else "not found"
            rows.append(CheckResult("dependencies", f"Tool {tool}", "info" if note else "warn", missing))
    return rows


@check("free_tier", "Free-tier advisors", REPORT)
def _free_tier(ctx: CheckContext) -> CheckResult:
    from nvh.core.free_tier import detect_available_free_advisors

    names = [a.name for a in detect_available_free_advisors()]
    return CheckResult("free_tier", "Free-tier advisors", "info", ", ".join(names) or "none detected", data={"available": names})


@check("rag_index", "RAG index", REPORT)
def _rag_index(ctx: CheckContext) -> CheckResult:
    from nvh.integrations.rag import list_collections

    collections = list_collections()
    detail = ", ".join(f"{c['name']} ({c['chunks']} chunks)" for c in collections[:5]) or "empty"
    return CheckResult("rag_index", "RAG index", "info", f"{len(collections)} collection(s): {detail}")


@check("vault", "Vault", REPORT)
def _vault(ctx: CheckContext) -> CheckResult:
    from nvh.integrations.workspace.vault import vault_status

    vault = vault_status()
    return CheckResult(
        "vault", "Vault", "info",
        f"initialized={vault['initialized']}, {vault['markdown_files']} note(s)",
    )


@check("tools", "Tools", REPORT)
def _tools(ctx: CheckContext) -> CheckResult:
    from nvh.core.tools import ToolRegistry

    names = [t.name for t in ToolRegistry().list_tools()]
    return CheckResult("tools", "Tools", "info", f"{len(names)} registered", data={"tools": names})


@check("scheduler", "Scheduler", REPORT)
def _scheduler(ctx: CheckContext) -> CheckResult:
    from nvh.core.scheduler import Scheduler

    return CheckResult("scheduler", "Scheduler", "info", f"{len(Scheduler().list_tasks())} task(s)")


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
            rows.append(CheckResult("network", f"Network: {name}", "pass", f"reachable ({resp.status_code})"))
        except Exception:
            rows.append(CheckResult("network", f"Network: {name}", "warn", "unreachable"))
    return rows


@check("routing_probe", "Routing probe", REPORT)
async def _routing_probe(ctx: CheckContext) -> CheckResult:
    engine = await ctx.engine()
    if engine is None or not ctx.enabled_providers:
        return CheckResult("routing_probe", "Routing probe", "warn", "no advisors — cannot test routing")
    decision = engine.router.route("test query hello world")
    return CheckResult(
        "routing_probe", "Routing probe", "pass",
        f"{decision.provider}/{decision.model} ({decision.task_type.value}, confidence {decision.confidence:.2f})",
        data={"provider": decision.provider, "model": decision.model, "reason": decision.reason},
    )


__all__ = [
    "Check",
    "CheckContext",
    "CheckResult",
    "DEEP",
    "GLANCE",
    "PROVIDERS",
    "REGISTRY",
    "REPORT",
    "TIERS",
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
