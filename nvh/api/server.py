"""Council REST API server.

Dependencies (add to pyproject.toml):
    fastapi>=0.115
    uvicorn[standard]>=0.30
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import time
from collections import defaultdict
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Path,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

from nvh.core.agents import generate_agents, get_preset_agents, list_presets
from nvh.core.engine import BudgetExceededError, Engine
from nvh.providers.base import (
    CompletionResponse,
    ProviderError,
)
from nvh.utils.gpu import (
    check_oom_risk,
    detect_gpus,
    detect_system_memory,
    get_gpu_summary,
    get_ollama_optimizations,
    recommend_models,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional API key authentication
# ---------------------------------------------------------------------------

_bearer_scheme = HTTPBearer(auto_error=False)


def _get_council_api_key() -> str | None:
    """Return the HIVE_API_KEY env var if set, otherwise None (open mode)."""
    return os.environ.get("HIVE_API_KEY") or None


async def _get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None,
    x_key: str | None,
) -> Any | None:
    """Try to resolve a User from a bearer token if user auth is enabled.

    Returns the User object on success, None if user auth is not active or the
    token does not resolve to a user-based token.
    """
    from nvh.auth.auth import get_user_by_token, get_user_count
    try:
        if await get_user_count() == 0:
            return None  # no users registered — fall through to API key mode
    except Exception:
        return None

    token: str | None = None
    if credentials:
        token = credentials.credentials
    elif x_key:
        token = x_key

    if token is None:
        return None

    return await get_user_by_token(token)


async def require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> Any:
    """FastAPI dependency that enforces authentication.

    Priority:
    1. User-based auth (when users table has entries): validates API tokens
       created via the /v1/auth/* endpoints.
    2. Simple HIVE_API_KEY env var (single-user, legacy).
    3. Open/local mode — when neither users nor HIVE_API_KEY are configured.

    Accepts credentials via:
    - ``Authorization: Bearer <token>`` header
    - ``X-Hive-API-Key: <key>`` header
    """
    x_key = request.headers.get("X-Hive-API-Key")

    # Try user-based auth first
    user = await _get_current_user_optional(credentials, x_key)
    if user is not None:
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User account is disabled.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return user

    # Fall back to simple API key
    expected = _get_council_api_key()
    if expected is None:
        # Open mode — no auth required
        return None

    # Check Bearer token
    if credentials and hmac.compare_digest(credentials.credentials, expected):
        return None

    # Check X-Hive-API-Key header
    if x_key and hmac.compare_digest(x_key, expected):
        return None

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=(
            "Authentication required. Provide a valid API key via "
            "'Authorization: Bearer <key>' or 'X-Hive-API-Key: <key>' header."
        ),
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_user_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> Any:
    """Strict user-based auth dependency — requires a valid user API token.

    Used for /v1/auth/* endpoints that manage users and tokens.
    """
    x_key = request.headers.get("X-Hive-API-Key")
    user = await _get_current_user_optional(credentials, x_key)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid user authentication token required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_scope(scope: str):
    """Dependency factory that enforces a required OAuth-style scope on the user."""
    async def _check(user: Any = Depends(require_user_auth)) -> Any:
        if hasattr(user, '_scopes') and scope not in user._scopes and "admin" not in user._scopes:
            raise HTTPException(status_code=403, detail=f"Insufficient scope: requires '{scope}'")
        return user
    return _check


# ---------------------------------------------------------------------------
# Rate limiting for auth endpoints
# ---------------------------------------------------------------------------

_auth_attempts: dict[str, list[float]] = defaultdict(list)
AUTH_RATE_LIMIT = 5  # attempts per minute


def _check_auth_rate_limit(client_ip: str) -> None:
    """Raise HTTP 429 if the client has exceeded the auth rate limit."""
    now = time.time()
    attempts = _auth_attempts[client_ip]
    # Remove attempts older than 60 seconds
    _auth_attempts[client_ip] = [t for t in attempts if now - t < 60]
    if len(_auth_attempts[client_ip]) >= AUTH_RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many attempts. Try again in 60 seconds.")
    _auth_attempts[client_ip].append(now)


# ---------------------------------------------------------------------------
# WebSocket authentication helper
# ---------------------------------------------------------------------------

async def _authenticate_websocket(websocket: WebSocket) -> bool:
    """Validate WebSocket auth via query param or Authorization header."""
    # Check query param: ?token=xxx
    token = websocket.query_params.get("token")
    if not token:
        # Also check Authorization header (some WS clients support it)
        auth_header = websocket.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

    if not token:
        # Check HIVE_API_KEY
        expected = os.environ.get("HIVE_API_KEY", "")
        if not expected:
            return True  # Open mode
        await websocket.close(code=4001, reason="Authentication required")
        return False

    # Validate against simple API key
    expected = os.environ.get("HIVE_API_KEY", "")
    if expected and hmac.compare_digest(token, expected):
        return True

    # Try user token
    try:
        from nvh.auth.auth import get_user_by_token
        user = await get_user_by_token(token)
        if user:
            return True
    except Exception:
        logger.warning("WebSocket auth: token validation failed", exc_info=True)

    await websocket.close(code=4003, reason="Invalid token")
    return False


# ---------------------------------------------------------------------------
# SSRF protection for webhook URLs
# ---------------------------------------------------------------------------

def _validate_webhook_url(url: str) -> None:
    """Raise ValueError if the URL is unsafe (private/loopback IPs, bad scheme, etc.)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL must use http or https")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Invalid URL")
    # Block cloud metadata endpoints (check before IP parse)
    if hostname in ("169.254.169.254", "metadata.google.internal"):
        raise ValueError("Cannot send webhooks to cloud metadata endpoints")
    # Block internal/private IPs
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise ValueError("Cannot send webhooks to private/internal addresses")
    except ValueError as exc:
        # Re-raise only our own explicit errors; a non-IP hostname is fine
        if "Cannot send webhooks" in str(exc):
            raise


# ---------------------------------------------------------------------------
# Shared engine instance
# ---------------------------------------------------------------------------

_engine: Engine | None = None


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Engine not initialized. Server startup failed.")
    return _engine


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _engine
    from nvh.utils.logging import setup_logging
    json_mode = os.environ.get("HIVE_LOG_FORMAT", "text") == "json"
    setup_logging(level=os.environ.get("HIVE_LOG_LEVEL", "INFO"), json_format=json_mode)
    logger.info("Hive API: initializing engine...")
    try:
        _engine = Engine()
        enabled = await _engine.initialize()
        logger.info("Hive API: engine ready. Advisors: %s", ", ".join(enabled) or "none")
        yield
    except Exception as exc:
        logger.error("Hive API: engine initialization error: %s", exc)
        # Don't crash — partial init is fine; requests will fail gracefully.
        yield
    finally:
        logger.info("Hive API: shutting down.")
        if _engine:
            if hasattr(_engine, 'webhooks') and _engine.webhooks:
                await _engine.webhooks.stop()
        from nvh.storage.repository import close_db
        await close_db()
        _engine = None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Hive API",
    description="REST API for the Hive multi-LLM orchestration platform.",
    version="1.0.0",
    lifespan=lifespan,
)

_DEFAULT_CORS_ORIGINS = [
    # Common Next.js dev ports
    "http://localhost:3000", "http://127.0.0.1:3000",
    "http://localhost:3001", "http://127.0.0.1:3001",
    "http://localhost:3002", "http://127.0.0.1:3002",
    "http://localhost:8080", "http://127.0.0.1:8080",
    # `nvh webui` may bind port 80 and advertise the `nvhive` hostname
    "http://localhost", "http://127.0.0.1",
    "http://nvhive", "http://nvhive:3000", "http://nvhive:3001",
    "http://nvhive:3002", "http://nvhive:8080",
]
_cors_env = os.environ.get("HIVE_CORS_ORIGINS")
ALLOWED_ORIGINS = (
    [o.strip() for o in _cors_env.split(",") if o.strip()]
    if _cors_env
    else _DEFAULT_CORS_ORIGINS
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decimal_to_str(value: Decimal | None) -> str | None:
    """Serialize Decimal to string to preserve precision."""
    return str(value) if value is not None else None


def _response_envelope(data: Any) -> dict[str, Any]:
    return {"status": "success", "data": data}


def _serialize_completion(resp: CompletionResponse) -> dict[str, Any]:
    return {
        "content": resp.content,
        "model": resp.model,
        "provider": resp.provider,
        "usage": resp.usage.model_dump(),
        "cost_usd": _decimal_to_str(resp.cost_usd),
        "latency_ms": resp.latency_ms,
        "finish_reason": resp.finish_reason.value,
        "cache_hit": resp.cache_hit,
        "fallback_from": resp.fallback_from,
        "metadata": resp.metadata,
    }


# ---------------------------------------------------------------------------
# Request / Response Schemas
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=500_000)
    provider: str | None = None
    model: str | None = None
    system_prompt: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)
    stream: bool = False


class CouncilRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=500_000)
    members: list[str] | None = None
    weights: dict[str, float] | None = None
    strategy: str | None = None
    auto_agents: bool = False
    preset: str | None = None
    num_agents: int | None = Field(default=None, gt=0, le=10)
    synthesize: bool = True
    system_prompt: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)


class CompareRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=500_000)
    providers: list[str] | None = None
    system_prompt: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)


class AgentAnalyzeRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=500_000)
    num_agents: int = Field(default=3, gt=0, le=10)
    preset: str | None = None


# ---------------------------------------------------------------------------
# Utilities — streaming
# ---------------------------------------------------------------------------

async def _sse_query_stream(
    engine: Engine,
    request: QueryRequest,
) -> AsyncGenerator:
    """Async generator that yields SSE-formatted bytes for a streaming query."""
    from nvh.providers.base import StreamChunk

    config = engine.config
    temp = request.temperature if request.temperature is not None else config.defaults.temperature
    max_tok = request.max_tokens or config.defaults.max_tokens
    sys_prompt = request.system_prompt or config.defaults.system_prompt or None

    # Budget and routing (reuse engine internals via a lightweight path)
    try:
        await engine._check_budget()
    except BudgetExceededError as exc:
        payload = json.dumps({"error": str(exc)})
        yield f"event: error\ndata: {payload}\n\n".encode()
        return

    decision = engine.router.route(
        query=request.prompt,
        provider_override=request.provider,
        model_override=request.model,
    )

    if not engine.registry.has(decision.provider):
        payload = json.dumps({"error": f"Provider '{decision.provider}' not available."})
        yield f"event: error\ndata: {payload}\n\n".encode()
        return

    provider = engine.registry.get(decision.provider)
    from nvh.providers.base import Message

    messages: list[Message] = []
    if sys_prompt:
        messages.append(Message(role="system", content=sys_prompt))
    messages.append(Message(role="user", content=request.prompt))

    accumulated = ""
    last_chunk: StreamChunk | None = None

    # Per-chunk stall timeout — a silent provider is worse than a failed one
    # because it freezes the UI. 45s is generous enough for slow cloud
    # providers but short enough to surface errors before users give up.
    CHUNK_STALL_TIMEOUT = 45.0  # noqa: N806 — local constant, intentional uppercase

    try:
        aiter = provider.stream(
            messages=messages,
            model=decision.model or None,
            temperature=temp,
            max_tokens=max_tok,
            system_prompt=sys_prompt,
        ).__aiter__()

        while True:
            try:
                chunk = await asyncio.wait_for(
                    aiter.__anext__(),
                    timeout=CHUNK_STALL_TIMEOUT,
                )
            except StopAsyncIteration:
                break
            except TimeoutError as exc:
                raise TimeoutError(
                    f"Provider '{decision.provider}' stalled — no tokens for "
                    f"{CHUNK_STALL_TIMEOUT:.0f}s"
                ) from exc

            last_chunk = chunk
            if chunk.delta:
                accumulated += chunk.delta
                payload = json.dumps({
                    "delta": chunk.delta,
                    "accumulated": accumulated,
                })
                yield f"event: chunk\ndata: {payload}\n\n".encode()

            if chunk.is_final:
                break

        # Final done event
        done_payload: dict[str, Any] = {
            "content": accumulated,
            "provider": decision.provider,
            "model": decision.model,
        }
        if last_chunk:
            if last_chunk.usage:
                done_payload["usage"] = last_chunk.usage.model_dump()
            if last_chunk.cost_usd is not None:
                done_payload["cost_usd"] = _decimal_to_str(last_chunk.cost_usd)
            if last_chunk.finish_reason:
                done_payload["finish_reason"] = last_chunk.finish_reason.value

        yield f"event: done\ndata: {json.dumps(done_payload)}\n\n".encode()

    except Exception as exc:
        payload = json.dumps({"error": str(exc)})
        yield f"event: error\ndata: {payload}\n\n".encode()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

# -- /v1/health ---------------------------------------------------------------

@app.get("/v1/health", summary="API health check")
async def health_check() -> dict[str, Any]:
    engine = get_engine()
    enabled = engine.registry.list_enabled()
    return _response_envelope({
        "status": "ok",
        "engine_initialized": engine._initialized,
        "providers_enabled": len(enabled),
    })


# -- /metrics (Prometheus) ----------------------------------------------------


def _prom_escape(value: str) -> str:
    """Escape a label value for Prometheus text exposition format."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


async def _build_prometheus_metrics() -> str:
    """Build Prometheus text exposition format from nvHive internal data."""
    from sqlalchemy import func as _fn
    from sqlalchemy import select as _sel

    from nvh.storage import repository as _metrics_repo
    from nvh.storage.models import QueryLog

    lines: list[str] = []
    engine = get_engine()

    # ------------------------------------------------------------------
    # 1) nvhive_queries_total — counter by provider, model, task_type (mode), status
    # ------------------------------------------------------------------
    try:
        from nvh.storage.repository import get_session as _get_sess

        async with _get_sess() as session:
            rows = (await session.execute(
                _sel(
                    QueryLog.provider,
                    QueryLog.model,
                    QueryLog.mode,
                    QueryLog.status,
                    _fn.count(QueryLog.id),
                ).group_by(QueryLog.provider, QueryLog.model, QueryLog.mode, QueryLog.status)
            )).all()

        lines.append("# HELP nvhive_queries_total Total queries processed")
        lines.append("# TYPE nvhive_queries_total counter")
        for prov, model, mode, st, cnt in rows:
            labels = (
                f'provider="{_prom_escape(prov)}",'
                f'model="{_prom_escape(model)}",'
                f'task_type="{_prom_escape(mode)}",'
                f'status="{_prom_escape(st)}"'
            )
            lines.append(f"nvhive_queries_total{{{labels}}} {cnt}")
    except Exception:
        logger.debug("metrics: failed to collect nvhive_queries_total", exc_info=True)

    # ------------------------------------------------------------------
    # 2) nvhive_query_latency_seconds — histogram approximation per provider
    #    We expose the sum and count; real histogram buckets would need
    #    in-process instrumentation.  Sum+count is still useful for rate().
    # ------------------------------------------------------------------
    try:
        async with _get_sess() as session:
            lat_rows = (await session.execute(
                _sel(
                    QueryLog.provider,
                    _fn.count(QueryLog.id),
                    _fn.sum(QueryLog.latency_ms),
                ).group_by(QueryLog.provider)
            )).all()

        lines.append("")
        lines.append("# HELP nvhive_query_latency_seconds Query latency in seconds")
        lines.append("# TYPE nvhive_query_latency_seconds histogram")
        for prov, cnt, total_ms in lat_rows:
            total_s = (total_ms or 0) / 1000.0
            lbl = f'provider="{_prom_escape(prov)}"'
            lines.append(f"nvhive_query_latency_seconds_count{{{lbl}}} {cnt}")
            lines.append(f"nvhive_query_latency_seconds_sum{{{lbl}}} {total_s:.3f}")
    except Exception:
        logger.debug("metrics: failed to collect nvhive_query_latency_seconds", exc_info=True)

    # ------------------------------------------------------------------
    # 3) nvhive_query_cost_usd — counter per provider
    # ------------------------------------------------------------------
    try:
        async with _get_sess() as session:
            cost_rows = (await session.execute(
                _sel(
                    QueryLog.provider,
                    _fn.coalesce(_fn.sum(QueryLog.cost_usd), 0),
                ).group_by(QueryLog.provider)
            )).all()

        lines.append("")
        lines.append("# HELP nvhive_query_cost_usd Total cost in USD")
        lines.append("# TYPE nvhive_query_cost_usd counter")
        for prov, cost in cost_rows:
            lbl = f'provider="{_prom_escape(prov)}"'
            lines.append(f"nvhive_query_cost_usd{{{lbl}}} {float(cost):.6f}")
    except Exception:
        logger.debug("metrics: failed to collect nvhive_query_cost_usd", exc_info=True)

    # ------------------------------------------------------------------
    # 4) nvhive_provider_health — gauge per provider (0.0-1.0)
    # ------------------------------------------------------------------
    try:
        enabled = engine.registry.list_enabled()
        lines.append("")
        lines.append("# HELP nvhive_provider_health Current health score per provider")
        lines.append("# TYPE nvhive_provider_health gauge")
        for prov_name in enabled:
            score = engine.rate_manager.get_health_score(prov_name)
            lbl = f'provider="{_prom_escape(prov_name)}"'
            lines.append(f"nvhive_provider_health{{{lbl}}} {score:.2f}")
    except Exception:
        logger.debug("metrics: failed to collect nvhive_provider_health", exc_info=True)

    # ------------------------------------------------------------------
    # 5) nvhive_council_queries_total — counter by strategy (mode=council)
    # ------------------------------------------------------------------
    try:
        async with _get_sess() as session:
            council_rows = (await session.execute(
                _sel(
                    QueryLog.mode,
                    _fn.count(QueryLog.id),
                ).where(QueryLog.mode == "council")
                .group_by(QueryLog.mode)
            )).all()

        lines.append("")
        lines.append("# HELP nvhive_council_queries_total Council queries processed")
        lines.append("# TYPE nvhive_council_queries_total counter")
        for strategy, cnt in council_rows:
            lbl = f'strategy="{_prom_escape(strategy)}"'
            lines.append(f"nvhive_council_queries_total{{{lbl}}} {cnt}")
        if not council_rows:
            lines.append('nvhive_council_queries_total{strategy="council"} 0')
    except Exception:
        logger.debug("metrics: failed to collect nvhive_council_queries_total", exc_info=True)

    # ------------------------------------------------------------------
    # 6) nvhive_fallback_total — counter by from_provider, to_provider
    # ------------------------------------------------------------------
    try:
        async with _get_sess() as session:
            fb_rows = (await session.execute(
                _sel(
                    QueryLog.fallback_from,
                    QueryLog.provider,
                    _fn.count(QueryLog.id),
                ).where(QueryLog.status == "fallback", QueryLog.fallback_from != "")
                .group_by(QueryLog.fallback_from, QueryLog.provider)
            )).all()

        lines.append("")
        lines.append("# HELP nvhive_fallback_total Fallback events")
        lines.append("# TYPE nvhive_fallback_total counter")
        for from_prov, to_prov, cnt in fb_rows:
            labels = (
                f'from_provider="{_prom_escape(from_prov)}",'
                f'to_provider="{_prom_escape(to_prov)}"'
            )
            lines.append(f"nvhive_fallback_total{{{labels}}} {cnt}")
        if not fb_rows:
            lines.append('nvhive_fallback_total{from_provider="",to_provider=""} 0')
    except Exception:
        logger.debug("metrics: failed to collect nvhive_fallback_total", exc_info=True)

    # ------------------------------------------------------------------
    # 7) nvhive_learning_observations_total — gauge
    # ------------------------------------------------------------------
    try:
        obs_count = await _metrics_repo.get_outcome_count()
        lines.append("")
        lines.append(
            "# HELP nvhive_learning_observations_total Total learning observations"
        )
        lines.append("# TYPE nvhive_learning_observations_total gauge")
        lines.append(f"nvhive_learning_observations_total {obs_count}")
    except Exception:
        logger.debug(
            "metrics: failed to collect nvhive_learning_observations_total",
            exc_info=True,
        )

    lines.append("")
    return "\n".join(lines)


@app.get("/metrics", summary="Prometheus metrics endpoint")
async def prometheus_metrics() -> Response:
    """Return nvHive metrics in Prometheus text exposition format."""
    body = await _build_prometheus_metrics()
    return Response(
        content=body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.get("/v1/metrics", summary="Prometheus metrics endpoint (v1 alias)")
async def prometheus_metrics_v1() -> Response:
    """Alias for /metrics under the /v1/ prefix."""
    body = await _build_prometheus_metrics()
    return Response(
        content=body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


# -- /v1/system/gpu -----------------------------------------------------------

def _serialize_gpu_data() -> dict[str, Any]:
    """Detect GPUs and return serialisable dict. Never raises — returns empty on error."""
    try:
        gpus = detect_gpus()
        sys_mem = detect_system_memory()
        summary = get_gpu_summary()
        total_vram_gb = round(sum(g.vram_mb for g in gpus) / 1024, 1) if gpus else 0.0

        gpu_list = [
            {
                "name": g.name,
                "vram_mb": g.vram_mb,
                "vram_gb": g.vram_gb,
                "memory_used_mb": g.memory_used_mb,
                "memory_free_mb": g.memory_free_mb,
                "utilization_pct": g.utilization_pct,
                "driver_version": g.driver_version,
                "cuda_version": g.cuda_version,
                "index": g.index,
            }
            for g in gpus
        ]

        return {
            "gpus": gpu_list,
            "summary": summary,
            "total_vram_gb": total_vram_gb,
            "system_ram": {
                "total_gb": sys_mem.total_ram_gb,
                "available_gb": sys_mem.available_ram_gb,
                "effective_for_llm_gb": sys_mem.effective_for_llm_gb,
            },
        }
    except Exception as exc:
        logger.warning("GPU detection failed: %s", exc)
        return {
            "gpus": [],
            "summary": "GPU detection unavailable",
            "total_vram_gb": 0.0,
            "system_ram": {"total_gb": 0.0, "available_gb": 0.0, "effective_for_llm_gb": 0.0},
        }


@app.get("/v1/system/gpu", summary="Detect NVIDIA GPUs and return hardware info")
async def system_gpu() -> dict[str, Any]:
    """Return detected GPU hardware info. Returns empty GPU list gracefully when
    nvidia-smi is not available (CPU-only host)."""
    return _response_envelope(_serialize_gpu_data())


# -- /v1/system/recommendations -----------------------------------------------

@app.get("/v1/system/recommendations", summary="Model recommendations based on detected GPU")
async def system_recommendations() -> dict[str, Any]:
    """Return Nemotron model recommendations and Ollama optimisation settings for
    the detected GPU. Safe to call on CPU-only hosts."""
    try:
        gpus = detect_gpus()
        recs = recommend_models(gpus)
        opts = get_ollama_optimizations(gpus)

        # OOM check for the three main Nemotron model sizes
        oom_models = {
            "nemotron-mini": 2.0,
            "nemotron-small": 5.0,
            "nemotron": 40.0,
        }
        oom_results = {name: check_oom_risk(vram, gpus) for name, vram in oom_models.items()}

        rec_data = [
            {
                "model": r.model,
                "reason": r.reason,
                "vram_required_gb": r.vram_required_gb,
                "tier": r.tier,
            }
            for r in recs
        ]

        opt_data = {
            "flash_attention": opts.flash_attention,
            "num_parallel": opts.num_parallel,
            "recommended_ctx": opts.recommended_ctx,
            "recommended_quant": opts.recommended_quant,
            "architecture": opts.architecture,
            "compute_capability": list(opts.compute_capability),
            "notes": opts.notes,
        }

        return _response_envelope({
            "recommendations": rec_data,
            "optimizations": opt_data,
            "oom_check": oom_results,
        })
    except Exception as exc:
        logger.warning("Recommendations endpoint error: %s", exc)
        return _response_envelope({
            "recommendations": [],
            "optimizations": {
                "flash_attention": False,
                "num_parallel": 1,
                "recommended_ctx": 2048,
                "recommended_quant": "Q4_K_M",
                "architecture": "Unknown",
                "compute_capability": [0, 0],
                "notes": ["GPU detection unavailable"],
            },
            "oom_check": {},
        })


# -- /v1/system/info ----------------------------------------------------------

@app.get("/v1/system/info", summary="Combined system status — GPU + providers + budget in one call")
async def system_info(_auth: None = Depends(require_auth)) -> dict[str, Any]:
    """Dashboard-optimised endpoint: returns GPU info, provider counts, budget and
    cache stats in a single round-trip."""
    engine = get_engine()

    # GPU (never raises)
    gpu_data = _serialize_gpu_data()

    # Providers — best-effort, don't crash the whole endpoint on timeouts
    providers_online = 0
    providers_total = 0
    ollama_status = "disconnected"
    try:
        enabled = engine.registry.list_enabled()
        providers_total = len(enabled)

        async def _quick_check(name: str) -> bool:
            try:
                provider = engine.registry.get(name)
                hs = await asyncio.wait_for(provider.health_check(), timeout=5.0)
                return hs.healthy
            except Exception:
                return False

        results = await asyncio.gather(*[_quick_check(n) for n in enabled])
        providers_online = sum(results)

        if engine.registry.has("ollama"):
            try:
                ollama_provider = engine.registry.get("ollama")
                hs = await asyncio.wait_for(ollama_provider.health_check(), timeout=5.0)
                ollama_status = "connected" if hs.healthy else "disconnected"
            except Exception:
                ollama_status = "disconnected"
    except Exception:
        pass

    # Budget — best-effort
    budget_data: dict[str, Any] = {}
    try:
        raw_budget = await engine.get_budget_status()
        budget_data = {
            "daily_spend": str(raw_budget["daily_spend"]),
            "daily_limit": str(raw_budget["daily_limit"]),
            "monthly_spend": str(raw_budget["monthly_spend"]),
            "monthly_limit": str(raw_budget["monthly_limit"]),
            "daily_queries": raw_budget["daily_queries"],
            "monthly_queries": raw_budget["monthly_queries"],
        }
    except Exception:
        pass

    # Cache — best-effort
    cache_data: dict[str, Any] = {}
    try:
        cache_data = engine.cache.stats
    except Exception:
        pass

    return _response_envelope({
        "version": "0.1.0",
        "gpu": gpu_data,
        "providers_online": providers_online,
        "providers_total": providers_total,
        "budget": budget_data,
        "cache": cache_data,
        "ollama_status": ollama_status,
    })


# -- /v1/query ----------------------------------------------------------------

@app.post("/v1/query", summary="Single provider query")
async def query(request: QueryRequest, _auth: None = Depends(require_auth)) -> Any:
    engine = get_engine()

    if request.stream:
        return StreamingResponse(
            _sse_query_stream(engine, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        response = await engine.query(
            prompt=request.prompt,
            provider=request.provider,
            model=request.model,
            system_prompt=request.system_prompt,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            stream=False,
        )
    except BudgetExceededError as exc:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc))
    except ProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except Exception:
        logger.exception("Unexpected error in /v1/query")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

    return _response_envelope(_serialize_completion(response))


# -- /v1/council --------------------------------------------------------------

@app.post("/v1/council", summary="Council mode — multi-LLM orchestration")
async def council_query(request: CouncilRequest, _auth: None = Depends(require_auth)) -> dict[str, Any]:
    engine = get_engine()

    try:
        result = await engine.run_council(
            prompt=request.prompt,
            members=request.members,
            weights=request.weights,
            strategy=request.strategy,
            system_prompt=request.system_prompt,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            synthesize=request.synthesize,
            auto_agents=request.auto_agents,
            agent_preset=request.preset,
            num_agents=request.num_agents,
        )
    except BudgetExceededError as exc:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except Exception:
        logger.exception("Unexpected error in /v1/council")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

    member_data = {
        label: _serialize_completion(resp)
        for label, resp in result.member_responses.items()
    }

    data: dict[str, Any] = {
        "member_responses": member_data,
        "failed_members": result.failed_members,
        "strategy": result.strategy,
        "total_cost_usd": _decimal_to_str(result.total_cost_usd),
        "total_latency_ms": result.total_latency_ms,
        "quorum_met": result.quorum_met,
        "agents_used": result.agents_used,
        "synthesis": _serialize_completion(result.synthesis) if result.synthesis else None,
        "members": [
            {
                "provider": m.provider,
                "model": m.model,
                "weight": m.weight,
                "persona": m.persona,
            }
            for m in result.members
        ],
    }

    return _response_envelope(data)


# -- /v1/compare --------------------------------------------------------------

@app.post("/v1/compare", summary="Compare multiple providers on the same prompt")
async def compare(request: CompareRequest, _auth: None = Depends(require_auth)) -> dict[str, Any]:
    engine = get_engine()

    try:
        results = await engine.compare(
            prompt=request.prompt,
            providers=request.providers,
            system_prompt=request.system_prompt,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
        )
    except BudgetExceededError as exc:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc))
    except ProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except Exception:
        logger.exception("Unexpected error in /v1/compare")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

    return _response_envelope({
        provider: _serialize_completion(resp)
        for provider, resp in results.items()
    })


# -- /v1/advisors -------------------------------------------------------------

@app.get("/v1/advisors", summary="List configured advisors with health status")
async def list_providers(_auth: None = Depends(require_auth)) -> dict[str, Any]:
    engine = get_engine()
    enabled = engine.registry.list_enabled()

    async def _check(name: str) -> dict[str, Any]:
        try:
            provider = engine.registry.get(name)
            hs = await asyncio.wait_for(provider.health_check(), timeout=10.0)
            return {
                "name": name,
                "healthy": hs.healthy,
                "latency_ms": hs.latency_ms,
                "models_available": hs.models_available,
                "error": hs.error,
            }
        except Exception as exc:
            return {
                "name": name,
                "healthy": False,
                "latency_ms": None,
                "models_available": 0,
                "error": str(exc),
            }

    checks = await asyncio.gather(*[_check(n) for n in enabled])
    return _response_envelope({"providers": list(checks)})


@app.get("/v1/advisors/{name}/health", summary="Health check for a specific advisor")
async def provider_health(
    name: str = Path(..., description="Advisor name, e.g. 'openai'"),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    engine = get_engine()

    if not engine.registry.has(name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Advisor '{name}' is not configured or enabled.",
        )

    try:
        provider = engine.registry.get(name)
        hs = await asyncio.wait_for(provider.health_check(), timeout=10.0)
    except TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"Health check for '{name}' timed out.",
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    return _response_envelope({
        "name": hs.provider,
        "healthy": hs.healthy,
        "latency_ms": hs.latency_ms,
        "models_available": hs.models_available,
        "error": hs.error,
    })


# -- /v1/models ---------------------------------------------------------------

# TTL cache of live model IDs per provider. The static capability catalog
# in capabilities.yaml drifts out of sync with what providers actually
# serve (e.g. Groq deprecated gemma2-9b-it but it still sits in the
# catalog), so we intersect against the provider's current list_models()
# result. Cached because hitting every provider on every dropdown open
# is both slow and rude.
_LIVE_MODELS_CACHE: dict[str, tuple[float, set[str] | None]] = {}
_LIVE_MODELS_TTL = 300.0  # 5 minutes


async def _get_live_model_ids(engine, provider_name: str) -> set[str] | None:
    """Return the set of live model IDs for a provider, or None if unknown.

    None means "we couldn't determine the live list" (timeout, error, no
    implementation) — callers should treat that as "don't filter this
    provider's catalog entries" so we never hide models because of a
    transient fetch failure.
    """
    now = time.monotonic()
    cached = _LIVE_MODELS_CACHE.get(provider_name)
    if cached is not None and (now - cached[0]) < _LIVE_MODELS_TTL:
        return cached[1]

    try:
        provider = engine.registry.get(provider_name)
    except Exception:
        return None

    try:
        live = await asyncio.wait_for(provider.list_models(), timeout=5.0)
    except Exception:
        # Negative cache so we don't keep retrying a broken provider —
        # shorter TTL (30s) so it recovers quickly once the provider
        # comes back.
        _LIVE_MODELS_CACHE[provider_name] = (now - _LIVE_MODELS_TTL + 30.0, None)
        return None

    ids = {m.model_id for m in live if getattr(m, "model_id", None)}
    _LIVE_MODELS_CACHE[provider_name] = (now, ids)
    return ids


@app.get("/v1/models", summary="List available models from the capability catalog")
async def list_models(provider: str | None = None, _auth: None = Depends(require_auth)) -> dict[str, Any]:
    engine = get_engine()
    models = engine.registry.list_models(provider=provider)

    # Group catalog entries by provider, then fetch live model IDs for
    # each provider in parallel (cached). Filter the catalog to only
    # include models the provider still serves; providers whose live
    # list we couldn't fetch pass through unfiltered so we never
    # blank out the dropdown on a transient error.
    catalog_providers: set[str] = {m.provider for m in models}
    live_sets: dict[str, set[str] | None] = {}
    if catalog_providers:
        results = await asyncio.gather(
            *[_get_live_model_ids(engine, p) for p in catalog_providers],
            return_exceptions=True,
        )
        for p, r in zip(catalog_providers, results):
            live_sets[p] = r if isinstance(r, set) else None

    def _is_available(m) -> bool:
        live = live_sets.get(m.provider)
        if live is None:
            return True  # unknown — keep it
        return m.model_id in live

    filtered = [m for m in models if _is_available(m)]

    model_data = [
        {
            "model_id": m.model_id,
            "provider": m.provider,
            "display_name": m.display_name,
            "context_window": m.context_window,
            "max_output_tokens": m.max_output_tokens,
            "supports_streaming": m.supports_streaming,
            "supports_tools": m.supports_tools,
            "supports_vision": m.supports_vision,
            "supports_json_mode": m.supports_json_mode,
            "input_cost_per_1m_tokens": _decimal_to_str(m.input_cost_per_1m_tokens),
            "output_cost_per_1m_tokens": _decimal_to_str(m.output_cost_per_1m_tokens),
            "typical_latency_ms": m.typical_latency_ms,
            "capability_scores": m.capability_scores,
            "status": m.status,
        }
        for m in filtered
    ]

    return _response_envelope({"models": model_data, "count": len(model_data)})


# -- /v1/budget/status --------------------------------------------------------

@app.get("/v1/budget/status", summary="Current budget usage and limits")
async def budget_status(_auth: None = Depends(require_auth)) -> dict[str, Any]:
    engine = get_engine()
    try:
        data = await engine.get_budget_status()
    except Exception:
        logger.exception("Error fetching budget status")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

    # Serialize Decimal values
    serialized = {
        "daily_spend": str(data["daily_spend"]),
        "daily_limit": str(data["daily_limit"]),
        "monthly_spend": str(data["monthly_spend"]),
        "monthly_limit": str(data["monthly_limit"]),
        "daily_queries": data["daily_queries"],
        "monthly_queries": data["monthly_queries"],
        "by_provider": {k: str(v) for k, v in (data.get("by_provider") or {}).items()},
    }
    return _response_envelope(serialized)


# -- /v1/cache/stats & /v1/cache ----------------------------------------------

@app.get("/v1/cache/stats", summary="In-memory response cache statistics")
async def cache_stats(_auth: None = Depends(require_auth)) -> dict[str, Any]:
    engine = get_engine()
    return _response_envelope(engine.cache.stats)


@app.delete("/v1/cache", summary="Clear the response cache")
async def clear_cache(provider: str | None = None, _auth: None = Depends(require_auth)) -> dict[str, Any]:
    engine = get_engine()
    cleared = await engine.cache.clear(provider=provider)
    return _response_envelope({"cleared": cleared, "provider": provider})


# -- /v1/analytics ------------------------------------------------------------

@app.get("/v1/analytics", summary="Usage analytics — query counts, cost breakdown, latency, savings")
async def analytics(_auth: None = Depends(require_auth)) -> dict[str, Any]:
    from nvh.storage import repository as repo
    try:
        data = await repo.get_analytics()
    except Exception:
        logger.exception("Error fetching analytics")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")
    return _response_envelope(data)


# -- /v1/agents ---------------------------------------------------------------

@app.get("/v1/agents/presets", summary="List available agent presets and their roles")
async def agent_presets(_auth: None = Depends(require_auth)) -> dict[str, Any]:
    return _response_envelope({"presets": list_presets()})


@app.post("/v1/agents/analyze", summary="Preview which agents would be generated for a query")
async def agents_analyze(request: AgentAnalyzeRequest, _auth: None = Depends(require_auth)) -> dict[str, Any]:
    try:
        if request.preset:
            personas = get_preset_agents(request.preset, request.prompt)
        else:
            personas = generate_agents(request.prompt, num_agents=request.num_agents)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    agent_data = [
        {
            "role": p.role,
            "expertise": p.expertise,
            "perspective": p.perspective,
            "system_prompt": p.system_prompt,
            "weight_boost": p.weight_boost,
        }
        for p in personas
    ]

    return _response_envelope({
        "agents": agent_data,
        "count": len(agent_data),
        "prompt_preview": request.prompt[:200] + ("..." if len(request.prompt) > 200 else ""),
    })


# -- /v1/ws/query -------------------------------------------------------------

@app.websocket("/v1/ws/query")
async def ws_query(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time single-provider streaming.

    Client sends one JSON message:
      {"type": "query_request", "prompt": "...", "provider": "...", ...}

    Server streams:
      {"type": "chunk", "delta": "...", "accumulated": "..."}
      {"type": "complete", "content": "...", "provider": "...", ...}
      {"type": "error", "error": "..."}
    """
    if not await _authenticate_websocket(websocket):
        return
    await websocket.accept()
    engine = get_engine()

    try:
        raw = await websocket.receive_json()
    except WebSocketDisconnect:
        return
    except Exception:
        await websocket.send_json({"type": "error", "error": "Invalid message format"})
        await websocket.close()
        return

    if raw.get("type") != "query_request":
        await websocket.send_json({"type": "error", "error": "Expected type 'query_request'"})
        await websocket.close()
        return

    from nvh.providers.base import Message, StreamChunk

    prompt = raw.get("prompt", "")
    provider_name = raw.get("provider")
    model_name = raw.get("model")
    sys_prompt = raw.get("system_prompt") or engine.config.defaults.system_prompt or None
    temperature = raw.get("temperature") or engine.config.defaults.temperature
    max_tokens = raw.get("max_tokens") or engine.config.defaults.max_tokens

    try:
        await engine._check_budget()
    except BudgetExceededError as exc:
        await websocket.send_json({"type": "error", "error": str(exc)})
        await websocket.close()
        return

    decision = engine.router.route(
        query=prompt,
        provider_override=provider_name,
        model_override=model_name,
    )

    if not engine.registry.has(decision.provider):
        await websocket.send_json({"type": "error", "error": f"Provider '{decision.provider}' not available."})
        await websocket.close()
        return

    provider = engine.registry.get(decision.provider)
    messages: list[Message] = []
    if sys_prompt:
        messages.append(Message(role="system", content=sys_prompt))
    messages.append(Message(role="user", content=prompt))

    accumulated = ""
    last_chunk: StreamChunk | None = None
    stream_succeeded = False

    try:
        async for chunk in provider.stream(
            messages=messages,
            model=decision.model or None,
            temperature=temperature,
            max_tokens=max_tokens,
            system_prompt=sys_prompt,
        ):
            last_chunk = chunk
            if chunk.delta:
                accumulated += chunk.delta
                await websocket.send_json({
                    "type": "chunk",
                    "delta": chunk.delta,
                    "accumulated": accumulated,
                })
            if chunk.is_final:
                break

        stream_succeeded = True
        complete_payload: dict[str, Any] = {
            "type": "complete",
            "content": accumulated,
            "provider": decision.provider,
            "model": decision.model,
        }
        if last_chunk:
            if last_chunk.usage:
                complete_payload["usage"] = last_chunk.usage.model_dump()
            if last_chunk.cost_usd is not None:
                complete_payload["cost_usd"] = _decimal_to_str(last_chunk.cost_usd)
            if last_chunk.finish_reason:
                complete_payload["finish_reason"] = last_chunk.finish_reason.value

        await websocket.send_json(complete_payload)

        # Observability: tell the rate manager about the success so
        # circuit breakers track WS traffic, and log the query into
        # the analytics DB so it shows up in /v1/analytics and
        # /v1/budget/status. Without this the WebSocket path was a
        # blind spot in every dashboard.
        try:
            engine.rate_manager.record_success(decision.provider)
        except Exception as exc:
            logger.debug("rate_manager.record_success failed: %s", exc)

        if last_chunk:
            from nvh.providers.base import CompletionResponse, FinishReason, Usage
            resp = CompletionResponse(
                content=accumulated,
                model=last_chunk.model or decision.model,
                provider=decision.provider,
                usage=last_chunk.usage if last_chunk.usage else Usage(),
                cost_usd=last_chunk.cost_usd if last_chunk.cost_usd is not None else Decimal("0"),
                latency_ms=0,
                finish_reason=last_chunk.finish_reason or FinishReason.STOP,
            )
            try:
                await engine._log_query(resp, mode="simple")
            except Exception as exc:
                logger.debug("engine._log_query failed for ws_query: %s", exc)

    except WebSocketDisconnect:
        # Client hung up mid-stream. Still record the failure so the
        # rate manager sees it — a disconnect mid-stream is almost
        # always caused by provider stall, not a clean client exit.
        try:
            engine.rate_manager.record_failure(
                decision.provider,
                Exception("client disconnected mid-stream"),
            )
        except Exception as exc:
            logger.debug("rate_manager.record_failure failed: %s", exc)
        return
    except Exception as exc:
        logger.exception("Unexpected error in /v1/ws/query")
        try:
            engine.rate_manager.record_failure(decision.provider, exc)
        except Exception as rec_exc:
            logger.debug("rate_manager.record_failure failed: %s", rec_exc)
        try:
            await websocket.send_json({"type": "error", "error": "Internal server error"})
        except Exception as send_exc:
            logger.debug("error-send failed in ws_query: %s", send_exc)
    finally:
        if not stream_succeeded:
            logger.info(
                "ws_query terminated without success (provider=%s)",
                decision.provider,
            )
        try:
            await websocket.close()
        except Exception as exc:
            logger.debug("websocket.close failed in ws_query: %s", exc)


# -- /v1/ws/council -----------------------------------------------------------

@app.websocket("/v1/ws/council")
async def ws_council(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time council streaming.

    Client sends one JSON message:
      {
        "type": "council_request",
        "prompt": "...",
        "auto_agents": true,
        "preset": null,
        "strategy": "weighted_consensus",
        "members": null,
        "weights": null,
        "temperature": 1.0,
        "max_tokens": 4096,
        "system_prompt": null
      }

    Server streams a sequence of typed events (see CouncilOrchestrator.run_council_streaming).
    """
    if not await _authenticate_websocket(websocket):
        return
    await websocket.accept()
    engine = get_engine()

    try:
        raw = await websocket.receive_json()
    except WebSocketDisconnect:
        return
    except Exception:
        await websocket.send_json({"type": "error", "error": "Invalid message format"})
        await websocket.close()
        return

    if raw.get("type") != "council_request":
        await websocket.send_json({"type": "error", "error": "Expected type 'council_request'"})
        await websocket.close()
        return

    prompt = raw.get("prompt", "")
    auto_agents = bool(raw.get("auto_agents", False))
    preset = raw.get("preset") or None
    strategy = raw.get("strategy") or None
    members_override = raw.get("members") or None
    weights_override = raw.get("weights") or None
    temperature = float(raw.get("temperature") or engine.config.defaults.temperature)
    max_tokens = int(raw.get("max_tokens") or engine.config.defaults.max_tokens)
    system_prompt = raw.get("system_prompt") or engine.config.defaults.system_prompt or None

    try:
        await engine._check_budget()
    except BudgetExceededError as exc:
        await websocket.send_json({"type": "error", "error": str(exc)})
        await websocket.close()
        return

    async def _send(event: dict) -> None:
        """Send a single event over the WebSocket."""
        try:
            await websocket.send_json(event)
        except WebSocketDisconnect:
            pass  # client disconnected, expected
        except Exception:
            logger.debug("WebSocket send failed for event type=%s", event.get("type"))

    try:
        council_result = await engine.council.run_council_streaming(
            query=prompt,
            on_event=_send,
            members_override=members_override,
            weights_override=weights_override,
            strategy=strategy,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            synthesize=True,
            auto_agents=auto_agents,
            agent_preset=preset,
            budget_check=engine._check_budget,
        )

        # Observability: log every member response and the synthesis
        # into the analytics DB so WS councils show up in
        # /v1/analytics, /v1/budget/status, and conversation history.
        # Before this, council sessions run via WebSocket were
        # invisible to every dashboard — the HTTP council path logged,
        # but the WS path was a total blind spot.
        if council_result is not None:
            for resp in council_result.member_responses.values():
                try:
                    await engine._log_query(resp, mode="council")
                except Exception as log_exc:
                    logger.debug("engine._log_query (council member) failed: %s", log_exc)
            if council_result.synthesis is not None:
                try:
                    await engine._log_query(council_result.synthesis, mode="council")
                except Exception as log_exc:
                    logger.debug("engine._log_query (council synthesis) failed: %s", log_exc)

    except ValueError as exc:
        await _send({"type": "error", "error": str(exc)})
    except BudgetExceededError as exc:
        await _send({"type": "error", "error": str(exc)})
    except WebSocketDisconnect:
        return
    except Exception:
        logger.exception("Unexpected error in /v1/ws/council")
        await _send({"type": "error", "error": "Internal server error"})
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# -- /v1/webhooks -------------------------------------------------------------

@app.get("/v1/webhooks", summary="List configured webhooks (secrets masked)")
async def list_webhooks(_auth: None = Depends(require_auth)) -> dict[str, Any]:
    engine = get_engine()
    return _response_envelope({"webhooks": engine.webhooks.list_hooks()})


class WebhookTestRequest(BaseModel):
    url: str
    secret: str = ""


@app.post("/v1/webhooks/test", summary="Send a test event to a webhook URL")
async def test_webhook(request: WebhookTestRequest, _auth: None = Depends(require_auth)) -> dict[str, Any]:
    from nvh.core.webhooks import WebhookConfig, WebhookEvent, WebhookManager

    try:
        _validate_webhook_url(request.url)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    manager = WebhookManager()
    manager.register(WebhookConfig(
        url=request.url,
        events=[],  # empty = receive all
        secret=request.secret,
    ))

    try:
        payload_data = {"message": "This is a test webhook from Hive.", "url": request.url}
        await manager.emit(WebhookEvent.QUERY_COMPLETE, payload_data)
        # Drain immediately without starting background worker
        hook = manager._hooks[0]
        import time

        from nvh.core.webhooks import WebhookPayload
        wh_payload = WebhookPayload(event=WebhookEvent.QUERY_COMPLETE, timestamp=time.time(), data=payload_data)
        success = await manager._dispatch(hook, wh_payload)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    if not success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Webhook delivery to '{request.url}' failed after retries.",
        )
    return _response_envelope({"delivered": True, "url": request.url})


# -- /v1/auth -----------------------------------------------------------------

class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8)
    email: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateTokenRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    scopes: str = "query,council,compare"


@app.post("/v1/auth/register", summary="Register a new user (first user becomes admin)", status_code=status.HTTP_201_CREATED)
async def auth_register(request: RegisterRequest, req: Request) -> dict[str, Any]:
    """Create a new user. The very first user automatically receives the admin role."""
    from nvh.auth.auth import create_user, get_user_count

    _check_auth_rate_limit(req.client.host if req.client else "unknown")

    try:
        count = await get_user_count()
        role = "admin" if count == 0 else "user"
        user = await create_user(
            username=request.username,
            password=request.password,
            role=role,
            email=request.email,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except Exception:
        logger.exception("Error creating user")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

    return _response_envelope({
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "created_at": user.created_at.isoformat(),
    })


@app.post("/v1/auth/login", summary="Authenticate and receive an API token")
async def auth_login(request: LoginRequest, req: Request) -> dict[str, Any]:
    """Verify credentials and create a new API session token."""
    from nvh.auth.auth import authenticate_user, create_token_for_user

    _check_auth_rate_limit(req.client.host if req.client else "unknown")

    user = await authenticate_user(request.username, request.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    raw_token, token_record = await create_token_for_user(
        user_id=user.id,
        name="login_session",
        scopes="query,council,compare,admin" if user.role == "admin" else "query,council,compare",
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )

    return _response_envelope({
        "token": raw_token,
        "token_id": token_record.id,
        "user": {
            "id": user.id,
            "username": user.username,
            "role": user.role,
        },
        "note": "Save this token — it will not be shown again.",
    })


ALLOWED_SCOPES = {"ask", "convene", "poll", "read", "query", "council", "compare"}  # accept both old and new names

@app.post("/v1/auth/tokens", summary="Create a new API token", status_code=status.HTTP_201_CREATED)
async def auth_create_token(
    request: CreateTokenRequest,
    current_user: Any = Depends(require_scope("query")),
) -> dict[str, Any]:
    from nvh.auth.auth import create_token_for_user

    requested = set(request.scopes.split(","))
    is_admin = getattr(current_user, "role", None) == "admin"
    if not requested.issubset(ALLOWED_SCOPES | {"admin"}):
        raise HTTPException(400, "Invalid scopes. Allowed: query, council, compare, read")
    if "admin" in requested and not is_admin:
        raise HTTPException(403, "Only admins can create tokens with 'admin' scope")

    raw_token, token_record = await create_token_for_user(
        user_id=current_user.id,
        name=request.name,
        scopes=request.scopes,
    )
    return _response_envelope({
        "token": raw_token,
        "id": token_record.id,
        "name": token_record.name,
        "scopes": token_record.scopes,
        "created_at": token_record.created_at.isoformat(),
        "note": "Save this token — it will not be shown again.",
    })


@app.get("/v1/auth/tokens", summary="List user's API tokens")
async def auth_list_tokens(current_user: Any = Depends(require_user_auth)) -> dict[str, Any]:
    from nvh.auth.auth import list_user_tokens

    tokens = await list_user_tokens(current_user.id)
    return _response_envelope({
        "tokens": [
            {
                "id": t.id,
                "name": t.name,
                "scopes": t.scopes,
                "created_at": t.created_at.isoformat(),
                "last_used": t.last_used.isoformat() if t.last_used else None,
                "expires_at": t.expires_at.isoformat() if t.expires_at else None,
                "is_active": t.is_active,
            }
            for t in tokens
        ]
    })


@app.delete("/v1/auth/tokens/{token_id}", summary="Revoke an API token")
async def auth_revoke_token(
    token_id: str = Path(..., description="Token ID to revoke"),
    current_user: Any = Depends(require_user_auth),
) -> dict[str, Any]:
    from nvh.auth.auth import list_user_tokens, revoke_token

    # Verify the token belongs to the current user (unless admin)
    if current_user.role != "admin":
        user_tokens = await list_user_tokens(current_user.id)
        if not any(t.id == token_id for t in user_tokens):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only revoke your own tokens.",
            )

    revoked = await revoke_token(token_id)
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Token '{token_id}' not found.",
        )
    return _response_envelope({"revoked": True, "token_id": token_id})


@app.get("/v1/auth/me", summary="Get current authenticated user info")
async def auth_me(current_user: Any = Depends(require_user_auth)) -> dict[str, Any]:
    return _response_envelope({
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "role": current_user.role,
        "is_active": current_user.is_active,
        "created_at": current_user.created_at.isoformat(),
        "last_login": current_user.last_login.isoformat() if current_user.last_login else None,
    })


# ---------------------------------------------------------------------------
# Part 1: Ollama management endpoints
# ---------------------------------------------------------------------------

def _get_ollama_base_url() -> str:
    """Return the Ollama base URL from environment or default."""
    return os.environ.get("OLLAMA_HOST", "http://localhost:11434")


class OllamaPullRequest(BaseModel):
    model: str = Field(..., min_length=1, description="Model name to pull, e.g. 'nemotron-small'")


async def _ollama_pull_stream(model: str) -> AsyncGenerator:
    """Stream SSE events for an Ollama model pull."""
    import httpx

    base_url = _get_ollama_base_url()
    url = f"{base_url}/api/pull"

    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                url,
                json={"name": model, "stream": True},
            ) as response:
                if response.status_code != 200:
                    err = await response.aread()
                    payload = json.dumps({"error": err.decode(errors="replace")[:500]})
                    yield f"event: error\ndata: {payload}\n\n".encode()
                    return

                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    ollama_status = data.get("status", "")

                    # Ollama sends a final {"status": "success"} when done
                    if ollama_status == "success":
                        payload = json.dumps({"status": "success", "model": model})
                        yield f"event: complete\ndata: {payload}\n\n".encode()
                        return

                    # Progress events carry completed/total byte counts
                    completed = data.get("completed")
                    total = data.get("total")
                    percent: float | None = None
                    if completed is not None and total and total > 0:
                        percent = round(completed / total * 100, 1)

                    progress_payload: dict[str, Any] = {
                        "status": "pulling",
                        "digest": data.get("digest", ""),
                        "ollama_status": ollama_status,
                    }
                    if completed is not None:
                        progress_payload["completed"] = completed
                    if total is not None:
                        progress_payload["total"] = total
                    if percent is not None:
                        progress_payload["percent"] = percent

                    yield f"event: progress\ndata: {json.dumps(progress_payload)}\n\n".encode()

    except Exception as exc:
        payload = json.dumps({"error": str(exc)})
        yield f"event: error\ndata: {payload}\n\n".encode()


@app.post("/v1/ollama/pull", summary="Pull an Ollama model (SSE progress stream)")
async def ollama_pull(
    request: OllamaPullRequest,
    _auth: None = Depends(require_auth),
) -> StreamingResponse:
    """Trigger an Ollama model pull.  Response is a Server-Sent Events stream.

    Events emitted:
    - ``event: progress`` — download progress with completed/total bytes and percent
    - ``event: complete`` — pull finished successfully
    - ``event: error``    — pull failed
    """
    return StreamingResponse(
        _ollama_pull_stream(request.model),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/v1/ollama/models", summary="List installed Ollama models with sizes")
async def ollama_list_models(_auth: None = Depends(require_auth)) -> dict[str, Any]:
    """Return all models currently installed in Ollama with name, size, and digest."""
    import httpx

    base_url = _get_ollama_base_url()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{base_url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not reach Ollama at {base_url}: {exc}",
        )

    models = []
    for m in data.get("models", []):
        models.append({
            "name": m.get("name", ""),
            "size_bytes": m.get("size", 0),
            "size_gb": round(m.get("size", 0) / (1024 ** 3), 2),
            "digest": m.get("digest", ""),
            "modified_at": m.get("modified_at", ""),
            "details": m.get("details", {}),
        })

    return _response_envelope({"models": models, "count": len(models)})


@app.delete("/v1/ollama/models/{name:path}", summary="Delete an installed Ollama model")
async def ollama_delete_model(
    name: str = Path(..., description="Model name, e.g. 'nemotron-small'"),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    """Remove a model from Ollama's local store."""
    import httpx

    base_url = _get_ollama_base_url()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                "DELETE",
                f"{base_url}/api/delete",
                json={"name": name},
            )
            if resp.status_code == 404:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Model '{name}' not found in Ollama.",
                )
            resp.raise_for_status()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not reach Ollama at {base_url}: {exc}",
        )

    return _response_envelope({"deleted": True, "model": name})


@app.post("/v1/system/auto-setup", summary="Generate a setup plan: what models to pull, sizes, and estimated time")
async def system_auto_setup(_auth: None = Depends(require_auth)) -> dict[str, Any]:
    """One-shot setup planning endpoint for the web UI setup wizard.

    1. Detects GPU hardware via ``detect_gpus()``
    2. Gets model recommendations via ``recommend_models()``
    3. Checks which recommended models are already installed in Ollama
    4. Returns the plan: models to pull, estimated sizes, and estimated download time

    The UI can call this once, show the plan, then call ``POST /v1/ollama/pull``
    for each missing model to perform the actual downloads.
    """
    import httpx

    # --- GPU detection ---
    gpus = detect_gpus()
    recs = recommend_models(gpus)
    opts = get_ollama_optimizations(gpus)

    # --- Installed models ---
    base_url = _get_ollama_base_url()
    installed_names: set[str] = set()
    ollama_reachable = False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base_url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            for m in data.get("models", []):
                raw = m.get("name", "")
                # Strip tag for loose matching: "nemotron-small:latest" -> "nemotron-small"
                installed_names.add(raw)
                installed_names.add(raw.split(":")[0])
            ollama_reachable = True
    except Exception:
        pass

    # --- Build plan ---
    # Rough size estimates in GB (used for ETA when exact size is unknown)
    size_estimates: dict[str, float] = {
        "nemotron-mini": 2.0,
        "nemotron-small": 4.7,
        "nemotron": 40.0,
        "nemotron:120b": 67.0,
        "codellama": 3.8,
        "llama3.2:3b": 2.0,
        "llama3.3:70b-instruct-q4_K_M": 40.0,
    }
    # Assume ~200 Mbps download (typical cloud / consumer broadband)
    download_mbps = 200.0

    to_pull = []
    already_installed = []

    for rec in recs:
        model_name = rec.model
        is_installed = model_name in installed_names or model_name.split(":")[0] in installed_names

        size_gb = size_estimates.get(model_name, rec.vram_required_gb or 4.0)
        size_bytes = int(size_gb * 1024 ** 3)
        eta_seconds = int((size_gb * 8 * 1024) / download_mbps)  # GB -> Gb -> Mb -> s

        entry: dict[str, Any] = {
            "model": model_name,
            "reason": rec.reason,
            "tier": rec.tier,
            "estimated_size_gb": size_gb,
            "estimated_size_bytes": size_bytes,
            "estimated_download_seconds": eta_seconds,
            "already_installed": is_installed,
        }

        if is_installed:
            already_installed.append(entry)
        else:
            to_pull.append(entry)

    gpu_data = [
        {
            "name": g.name,
            "vram_gb": g.vram_gb,
            "driver_version": g.driver_version,
            "cuda_version": g.cuda_version,
            "index": g.index,
        }
        for g in gpus
    ]

    return _response_envelope({
        "gpus": gpu_data,
        "gpu_count": len(gpus),
        "ollama_reachable": ollama_reachable,
        "ollama_url": base_url,
        "optimizations": {
            "flash_attention": opts.flash_attention,
            "num_parallel": opts.num_parallel,
            "recommended_ctx": opts.recommended_ctx,
            "recommended_quant": opts.recommended_quant,
            "architecture": opts.architecture,
            "notes": opts.notes,
        },
        "plan": {
            "to_pull": to_pull,
            "already_installed": already_installed,
            "total_to_pull": len(to_pull),
            "total_estimated_gb": round(sum(m["estimated_size_gb"] for m in to_pull), 1),
            "total_estimated_download_seconds": sum(m["estimated_download_seconds"] for m in to_pull),
        },
    })


# ---------------------------------------------------------------------------
# Part 2: Sandbox execution endpoints
# ---------------------------------------------------------------------------

# Module-level singleton so the Docker availability check is cached across
# requests for the lifetime of the process.
_sandbox_executor: Any = None


def _get_sandbox() -> Any:
    global _sandbox_executor
    if _sandbox_executor is None:
        from nvh.sandbox.executor import SandboxConfig, SandboxExecutor
        _sandbox_executor = SandboxExecutor(SandboxConfig())
    return _sandbox_executor


class SandboxExecuteRequest(BaseModel):
    code: str = Field(..., description="Source code to execute")
    language: str = Field(default="python", description="python, javascript, or bash")
    files: dict[str, str] | None = Field(
        default=None,
        description="Optional extra files: filename -> content (available alongside main code)",
    )


@app.post("/v1/sandbox/execute", summary="Execute code in an isolated sandbox container")
async def sandbox_execute(
    request: SandboxExecuteRequest,
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    """Execute arbitrary code inside a sandboxed Docker container.

    - Network is disabled (``--network none``)
    - Memory is capped at 512 MB
    - Execution is time-limited to 30 s
    - Container runs as uid 1000, read-only filesystem with a small /tmp tmpfs
    - Falls back to a plain subprocess if Docker is unavailable
    """
    sandbox = _get_sandbox()

    try:
        result = await sandbox.execute(
            code=request.code,
            language=request.language,
            files=request.files,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Sandbox execution error: {exc}",
        )

    return _response_envelope({
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "execution_time_ms": result.execution_time_ms,
        "timed_out": result.timed_out,
        "error": result.error,
        "files_created": result.files_created,
    })


@app.get("/v1/sandbox/status", summary="Sandbox availability and configuration")
async def sandbox_status(_auth: None = Depends(require_auth)) -> dict[str, Any]:
    """Return whether Docker is available for sandboxed execution and the active config."""
    sandbox = _get_sandbox()
    docker_available = await sandbox._check_docker()

    return _response_envelope({
        "docker_available": docker_available,
        "isolation_mode": "docker" if docker_available else "subprocess",
        "config": {
            "timeout_seconds": sandbox.config.timeout_seconds,
            "memory_limit_mb": sandbox.config.memory_limit_mb,
            "network_enabled": sandbox.config.network_enabled,
            "max_output_bytes": sandbox.config.max_output_bytes,
            "allowed_languages": sandbox.config.allowed_languages,
        },
    })


# ---------------------------------------------------------------------------
# File Lock Coordination
# ---------------------------------------------------------------------------

@app.get("/v1/locks", summary="Current file lock status")
async def get_lock_status(_auth: None = Depends(require_auth)):
    """Show all active file locks and which agents hold them."""
    from nvh.core.file_lock import get_file_lock_coordinator
    coordinator = get_file_lock_coordinator()
    status = await coordinator.get_status()
    return {"status": "success", "data": status}


class ConflictCheckRequest(BaseModel):
    changes: dict[str, str] = Field(
        ..., description="Mapping of agent_id to file_path they want to modify"
    )


@app.post("/v1/locks/check-conflicts", summary="Check for file modification conflicts")
async def check_conflicts(
    request: ConflictCheckRequest,
    _auth: None = Depends(require_auth),
):
    """Check if proposed file changes would conflict with existing locks."""
    from nvh.core.file_lock import get_file_lock_coordinator
    coordinator = get_file_lock_coordinator()
    conflicts = await coordinator.check_conflicts(request.changes)
    return {
        "status": "success",
        "data": {
            "has_conflicts": len(conflicts) > 0,
            "conflicts": [
                {
                    "file": c.file_path,
                    "holder": c.agent_a,
                    "requester": c.agent_b,
                    "lock_held": c.lock_type_held.value,
                    "lock_requested": c.lock_type_requested.value,
                }
                for c in conflicts
            ],
        },
    }


# ---------------------------------------------------------------------------
# Part 3: Conversations management endpoints
# ---------------------------------------------------------------------------

def _serialize_conversation(conv: Any) -> dict[str, Any]:
    return {
        "id": conv.id,
        "title": conv.title,
        "provider": conv.provider,
        "model": conv.model,
        "message_count": conv.message_count,
        "total_tokens": conv.total_tokens,
        "total_cost_usd": str(conv.total_cost_usd),
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
        "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
    }


def _serialize_message(msg: Any) -> dict[str, Any]:
    return {
        "id": msg.id,
        "conversation_id": msg.conversation_id,
        "sequence": msg.sequence,
        "role": msg.role,
        "content": msg.content,
        "provider": msg.provider,
        "model": msg.model,
        "input_tokens": msg.input_tokens,
        "output_tokens": msg.output_tokens,
        "cost_usd": str(msg.cost_usd),
        "latency_ms": msg.latency_ms,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
    }


class ConversationQueryRequest(BaseModel):
    prompt: str
    provider: str | None = None
    model: str | None = None
    system_prompt: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)


from nvh.storage import (
    repository as repo,  # noqa: E402 — after middleware setup to avoid circular import
)


@app.get("/v1/conversations", summary="List conversations with pagination")
async def list_conversations(
    limit: int = 20,
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    """Return recent conversations ordered by last update, newest first."""
    try:
        convs = await repo.list_conversations(limit=min(limit, 200))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )
    return _response_envelope([_serialize_conversation(c) for c in convs])


@app.get("/v1/conversations/{conversation_id}", summary="Get a single conversation with all messages")
async def get_conversation(
    conversation_id: str = Path(..., description="Conversation UUID"),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    """Return a conversation and the full ordered message history."""
    try:
        conv = await repo.get_conversation(conversation_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )

    if conv is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversation '{conversation_id}' not found.",
        )

    try:
        messages = await repo.get_messages(conversation_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )

    data = _serialize_conversation(conv)
    data["messages"] = [_serialize_message(m) for m in messages]
    return _response_envelope(data)


@app.delete("/v1/conversations/{conversation_id}", summary="Delete a conversation and all its messages")
async def delete_conversation(
    conversation_id: str = Path(..., description="Conversation UUID"),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    """Permanently remove a conversation and every message in it."""
    try:
        deleted = await repo.delete_conversation(conversation_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversation '{conversation_id}' not found.",
        )

    return _response_envelope({"deleted": True, "conversation_id": conversation_id})


@app.post(
    "/v1/conversations/{conversation_id}/query",
    summary="Continue a conversation — send a message and receive a reply",
)
async def conversation_query(
    request: ConversationQueryRequest,
    conversation_id: str = Path(..., description="Conversation UUID"),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    """Send a new user message to an existing conversation and return the
    assistant response.  The full conversation history is included as context.

    If the ``conversation_id`` does not yet exist in the database it will be
    created automatically (useful for the UI which may generate the ID
    client-side before the first message).
    """
    engine = get_engine()

    try:
        response = await engine.query(
            prompt=request.prompt,
            provider=request.provider,
            model=request.model,
            system_prompt=request.system_prompt,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            stream=False,
            conversation_id=conversation_id,
        )
    except BudgetExceededError as exc:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc))
    except ProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in /v1/conversations/{id}/query")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    return _response_envelope({
        "conversation_id": conversation_id,
        "response": _serialize_completion(response),
    })


# ---------------------------------------------------------------------------
# Entry point — `council serve`
# ---------------------------------------------------------------------------

def run_server(host: str = "0.0.0.0", port: int = 8000, reload: bool = False) -> None:
    """Start the Council API server with uvicorn.

    Called by the CLI via `council serve --host HOST --port PORT`.
    """
    import uvicorn

    uvicorn.run(
        "nvh.api.server:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


# ---------------------------------------------------------------------------
# Context Files API
# ---------------------------------------------------------------------------

@app.get("/v1/context")
async def get_context_files(_auth: None = Depends(require_auth)):
    """List loaded COUNCIL.md context files."""
    from nvh.core.context_files import get_context_summary
    engine = get_engine()
    return {
        "status": "success",
        "data": {
            "files": get_context_summary(engine._context_files),
            "total": len(engine._context_files),
        },
    }


@app.post("/v1/context/reload")
async def reload_context(_auth: None = Depends(require_auth)):
    """Reload context files from disk."""
    from nvh.core.context_files import find_context_files
    engine = get_engine()
    engine._context_files = find_context_files()
    return {
        "status": "success",
        "data": {
            "files_loaded": len(engine._context_files),
            "names": [f.name for f in engine._context_files],
        },
    }


# ---------------------------------------------------------------------------
# Free Tier Setup API (for web UI setup wizard)
# ---------------------------------------------------------------------------

@app.get("/v1/setup/free-providers")
async def get_free_providers(_auth: None = Depends(require_auth)):
    """List all free-tier providers and their setup status."""
    from nvh.core.advisor_profiles import ADVISOR_PROFILES
    from nvh.core.free_tier import FREE_TIER_ADVISORS, detect_available_free_advisors

    available = detect_available_free_advisors()
    available_names = {a.name for a in available}

    # Placeholder hints per provider
    key_placeholders = {
        "openai": "sk-...",
        "anthropic": "sk-ant-...",
        "google": "AIza...",
        "groq": "gsk_...",
        "mistral": "your-key...",
        "cohere": "your-key...",
        "github": "ghp_...",
        "grok": "xai-...",
    }

    providers = []
    for advisor in FREE_TIER_ADVISORS:
        profile = ADVISOR_PROFILES.get(advisor.name, None)
        requires_key = advisor.check_fn == "env"
        signup_tier = (
            "none" if not requires_key
            else "email" if advisor.priority <= 8
            else "account"
        )
        providers.append({
            "id": advisor.name,
            "name": profile.display_name if profile else advisor.name,
            "display_name": profile.display_name if profile else advisor.name,
            "daily_limit": advisor.daily_limit,
            "priority": advisor.priority,
            "configured": advisor.name in available_names,
            "requires_signup": requires_key,
            "requires_key": requires_key,
            "signup_tier": signup_tier,
            "signup_url": _get_signup_url(advisor.name),
            "env_var": advisor.env_var,
            "env_key": advisor.env_var,
            "placeholder": key_placeholders.get(advisor.name, "your-key..."),
            "strengths": profile.strengths[:3] if profile else [],
            "free_tier_limits": profile.free_tier_limits if profile else "",
        })

    # Add zero-signup providers
    zero_signup = [
        {
            "id": "ollama",
            "name": "Ollama (Local AI)",
            "display_name": "Ollama (Local AI)",
            "daily_limit": "Unlimited (local GPU)",
            "priority": 1,
            "configured": "ollama" in available_names,
            "requires_signup": False,
            "requires_key": False,
            "signup_tier": "none",
            "signup_url": "",
            "env_var": "",
            "env_key": "",
            "placeholder": "",
            "strengths": ["Free", "Private", "Runs on your GPU"],
            "free_tier_limits": "Unlimited",
        },
        {
            "id": "llm7",
            "name": "LLM7 (Anonymous)",
            "display_name": "LLM7 (Anonymous)",
            "daily_limit": "30 RPM, no signup",
            "priority": 9,
            "configured": True,  # always available
            "requires_signup": False,
            "requires_key": False,
            "signup_tier": "none",
            "signup_url": "",
            "env_var": "",
            "env_key": "",
            "placeholder": "",
            "strengths": ["No signup needed", "Anonymous access", "DeepSeek-R1 free"],
            "free_tier_limits": "30 RPM anonymous, 120 RPM with token",
        },
    ]

    # Add paid providers (not free-tier but users want to configure them)
    import os
    paid_providers = [
        {
            "id": "openai", "name": "OpenAI", "display_name": "OpenAI",
            "daily_limit": "Pay-as-you-go", "priority": 0,
            "configured": bool(os.environ.get("OPENAI_API_KEY")),
            "requires_signup": True, "requires_key": True,
            "signup_tier": "account", "signup_url": "https://platform.openai.com/api-keys",
            "env_var": "OPENAI_API_KEY", "env_key": "OPENAI_API_KEY",
            "placeholder": "sk-...",
            "strengths": ["GPT-4o", "Best all-around", "Function calling"],
            "free_tier_limits": "",
        },
        {
            "id": "anthropic", "name": "Anthropic", "display_name": "Anthropic",
            "daily_limit": "Pay-as-you-go", "priority": 0,
            "configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "requires_signup": True, "requires_key": True,
            "signup_tier": "account", "signup_url": "https://console.anthropic.com/settings/keys",
            "env_var": "ANTHROPIC_API_KEY", "env_key": "ANTHROPIC_API_KEY",
            "placeholder": "sk-ant-...",
            "strengths": ["Claude Sonnet", "Best for code", "200K context"],
            "free_tier_limits": "",
        },
        {
            "id": "deepseek", "name": "DeepSeek", "display_name": "DeepSeek",
            "daily_limit": "Pay-as-you-go (very cheap)", "priority": 0,
            "configured": bool(os.environ.get("DEEPSEEK_API_KEY")),
            "requires_signup": True, "requires_key": True,
            "signup_tier": "account", "signup_url": "https://platform.deepseek.com",
            "env_var": "DEEPSEEK_API_KEY", "env_key": "DEEPSEEK_API_KEY",
            "placeholder": "sk-...",
            "strengths": ["DeepSeek R1", "$0.07/M tokens", "Top reasoning"],
            "free_tier_limits": "",
        },
    ]

    # Merge, dedup by id, and sort
    seen = set()
    all_providers = []
    for p in zero_signup + providers + paid_providers:
        if p["id"] not in seen:
            seen.add(p["id"])
            all_providers.append(p)
    all_providers.sort(key=lambda p: p["priority"])

    return {
        "status": "success",
        "data": {
            "providers": all_providers,
            "configured_count": sum(1 for p in all_providers if p["configured"]),
            "total_count": len(all_providers),
        },
    }


_ALLOWED_PROVIDERS = {
    "openai", "anthropic", "google", "groq", "mistral",
    "cohere", "deepseek", "github", "nvidia", "fireworks",
    "cerebras", "sambanova", "huggingface", "ai21",
    "siliconflow", "grok", "perplexity", "together",
    "openrouter", "ollama",
}


class SaveKeyRequest(BaseModel):
    provider: str = Field(..., min_length=1, max_length=32)
    api_key: str = Field(..., min_length=10, max_length=500)

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, v: str) -> str:
        if v not in _ALLOWED_PROVIDERS:
            raise ValueError(
                f"Unknown provider '{v}'. Allowed: "
                f"{', '.join(sorted(_ALLOWED_PROVIDERS))}"
            )
        return v


@app.post("/v1/setup/save-key")
async def save_provider_key(
    request: SaveKeyRequest,
    _auth: None = Depends(require_auth),
):
    """Save an API key for a provider (stores in OS keychain + env var).

    Also reinitializes the engine so the new provider is available
    immediately without restarting the server.
    """
    try:
        import keyring
        keyring.set_password("nvhive", f"{request.provider}_api_key", request.api_key)

        # Set the env var so the provider picks it up on reinit
        env_var_map = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "google": "GOOGLE_API_KEY",
            "groq": "GROQ_API_KEY",
            "mistral": "MISTRAL_API_KEY",
            "cohere": "COHERE_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "github": "GITHUB_TOKEN",
            "nvidia": "NVIDIA_API_KEY",
            "fireworks": "FIREWORKS_API_KEY",
            "cerebras": "CEREBRAS_API_KEY",
            "sambanova": "SAMBANOVA_API_KEY",
            "huggingface": "HUGGINGFACE_API_KEY",
            "ai21": "AI21_API_KEY",
            "siliconflow": "SILICONFLOW_API_KEY",
            "grok": "XAI_API_KEY",
            "perplexity": "PERPLEXITY_API_KEY",
            "together": "TOGETHER_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        }
        env_key = env_var_map.get(request.provider)
        if env_key:
            os.environ[env_key] = request.api_key

        # Reinitialize engine to register the new provider
        global _engine
        if _engine is not None:
            _engine._initialized = False
            await _engine.initialize()

        return {
            "status": "success",
            "data": {
                "provider": request.provider,
                "message": f"API key saved for {request.provider}. Provider is now active.",
            },
        }
    except Exception as exc:
        logger.warning(f"Failed to save key for {request.provider}: {exc}")
        return {
            "status": "error",
            "data": {"message": f"Keychain unavailable. Set {request.provider.upper()}_API_KEY environment variable."},
        }


@app.get("/v1/setup/status")
async def get_setup_status(_auth: None = Depends(require_auth)):
    """Check overall setup status — how many providers are ready."""
    from nvh.core.free_tier import detect_available_free_advisors

    available = detect_available_free_advisors()
    engine = get_engine()
    enabled = engine.registry.list_enabled() if engine else []

    return {
        "status": "success",
        "data": {
            "free_advisors_available": len(available),
            "total_advisors_enabled": len(enabled),
            "enabled_names": enabled,
            "free_names": [a.name for a in available],
            "ready": len(available) > 0 or len(enabled) > 0,
            "has_local": "ollama" in enabled or any(a.name == "ollama" for a in available),
        },
    }


def _get_signup_url(provider_name: str) -> str:
    """Get the signup URL for a provider."""
    urls = {
        "groq": "https://console.groq.com/keys",
        "cerebras": "https://cloud.cerebras.ai/",
        "fireworks": "https://fireworks.ai/",
        "siliconflow": "https://cloud.siliconflow.cn/",
        "cohere": "https://dashboard.cohere.com/api-keys",
        "ai21": "https://studio.ai21.com/",
        "sambanova": "https://cloud.sambanova.ai/",
        "huggingface": "https://huggingface.co/settings/tokens",
        "google": "https://aistudio.google.com/apikey",
        "github": "https://github.com/settings/tokens",
        "nvidia": "https://build.nvidia.com/",
        "mistral": "https://console.mistral.ai/api-keys",
    }
    return urls.get(provider_name, "")


# ---------------------------------------------------------------------------
# OpenAI-Compatible Proxy  (/v1/proxy/*)
# ---------------------------------------------------------------------------
# Any tool or SDK that supports a custom OpenAI base URL can point at:
#   http://localhost:8000/v1/proxy
# and NVHive will handle routing, fallback, and cost optimisation transparently.
# ---------------------------------------------------------------------------

class _ProxyChatMessage(BaseModel):
    role: str
    content: Any  # str or list[dict] for vision


class _ProxyChatRequest(BaseModel):
    model: str = "auto"
    messages: list[_ProxyChatMessage]
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)
    # Legacy / passthrough fields — accepted but not forwarded
    top_p: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    stop: Any = None
    n: int | None = None
    user: str | None = None


class _ProxyCompletionsRequest(BaseModel):
    """Legacy /completions endpoint (text-in, text-out)."""
    model: str = "auto"
    prompt: str | list[str]
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)
    stop: Any = None
    user: str | None = None


@app.post(
    "/v1/proxy/chat/completions",
    summary="OpenAI-compatible chat completions",
    tags=["proxy"],
)
async def proxy_chat_completions(
    request: _ProxyChatRequest,
    raw_request: Request,
    _auth: Any = Depends(require_auth),
):
    """Drop-in replacement for POST https://api.openai.com/v1/chat/completions.

    Set ``base_url="http://localhost:8000/v1/proxy"`` in any OpenAI client and
    all requests will be routed through NVHive's multi-provider engine.

    Supported ``model`` values:
    - ``"auto"`` / ``"nvhive"`` — smart routing (default)
    - ``"safe"`` / ``"local"`` — Ollama only, stays on-device
    - ``"council"`` / ``"council:N"`` — multi-LLM consensus (N members)
    - ``"throwdown"`` — two-pass deep analysis with critique
    - Any real model ID (``"gpt-4o"``, ``"claude-3-5-sonnet-20241022"``, …)

    NemoClaw integration:
    - Set ``x-nvhive-privacy: local-only`` header to force local routing.
    """
    from nvh.api.proxy import (
        format_openai_response,
        is_throwdown_model,
        openai_messages_to_nvhive,
        openai_stream_generator,
        parse_council_model,
        resolve_provider_from_model,
    )

    engine = get_engine()

    # Convert messages list to plain dicts for the helper
    messages_dicts = [{"role": m.role, "content": m.content} for m in request.messages]
    prompt, system_prompt = openai_messages_to_nvhive(messages_dicts)

    if not prompt:
        raise HTTPException(status_code=400, detail="No user message content found.")

    # --- Privacy header (NemoClaw integration) ---
    # x-nvhive-privacy: local-only forces all routing through Ollama
    privacy_header = raw_request.headers.get("x-nvhive-privacy", "").lower().strip()
    force_local = privacy_header in ("local-only", "local", "private")

    if force_local:
        provider_override, model_override = "ollama", None
    else:
        provider_override, model_override = resolve_provider_from_model(request.model)

    # --- Council path ---
    council_size = parse_council_model(request.model)
    if council_size is not None and not force_local:
        from nvh.api.proxy import council_stream_generator

        # Streaming council
        if request.stream:
            return StreamingResponse(
                council_stream_generator(
                    engine=engine,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    temperature=request.temperature,
                    max_tokens=request.max_tokens,
                    council_size=council_size,
                    requested_model=request.model,
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        # Non-streaming council
        try:
            await engine._check_budget()
        except BudgetExceededError as exc:
            raise HTTPException(status_code=402, detail=str(exc))

        try:
            result = await engine.run_council(
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                num_agents=council_size,
                auto_agents=True,
                privacy=force_local,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except BudgetExceededError as exc:
            raise HTTPException(status_code=402, detail=str(exc))
        except Exception as exc:
            logger.error("proxy council error: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail=str(exc))

        content = ""
        if result.synthesis:
            content = result.synthesis.content if hasattr(result.synthesis, "content") else str(result.synthesis)
        elif result.member_responses:
            first = next(iter(result.member_responses.values()))
            content = first.content if hasattr(first, "content") else str(first)

        responses = list(result.member_responses.values())
        total_input = sum(r.usage.input_tokens for r in responses if r.usage)
        total_output = sum(r.usage.output_tokens for r in responses if r.usage)

        return format_openai_response(
            content=content,
            model=f"council:{council_size}",
            provider="nvhive-council",
            prompt_tokens=total_input,
            completion_tokens=total_output,
            finish_reason="stop",
        )

    # --- Throwdown path ---
    if is_throwdown_model(request.model) and not force_local:
        from nvh.api.proxy import throwdown_stream_generator

        # Streaming throwdown
        if request.stream:
            return StreamingResponse(
                throwdown_stream_generator(
                    engine=engine,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    temperature=request.temperature,
                    max_tokens=request.max_tokens,
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        # Non-streaming throwdown
        try:
            await engine._check_budget()
        except BudgetExceededError as exc:
            raise HTTPException(status_code=402, detail=str(exc))

        try:
            result = await engine.run_council(
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                auto_agents=True,
                strategy="throwdown",
                privacy=force_local,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except BudgetExceededError as exc:
            raise HTTPException(status_code=402, detail=str(exc))
        except Exception as exc:
            logger.error("proxy throwdown error: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail=str(exc))

        content = ""
        if result.synthesis:
            content = result.synthesis.content if hasattr(result.synthesis, "content") else str(result.synthesis)
        elif result.member_responses:
            first = next(iter(result.member_responses.values()))
            content = first.content if hasattr(first, "content") else str(first)

        responses = list(result.member_responses.values())
        total_input = sum(r.usage.input_tokens for r in responses if r.usage)
        total_output = sum(r.usage.output_tokens for r in responses if r.usage)

        return format_openai_response(
            content=content,
            model="throwdown",
            provider="nvhive-throwdown",
            prompt_tokens=total_input,
            completion_tokens=total_output,
            finish_reason="stop",
        )

    # --- Streaming path ---
    if request.stream:
        return StreamingResponse(
            openai_stream_generator(
                engine=engine,
                prompt=prompt,
                provider_override=provider_override,
                model_override=model_override,
                system_prompt=system_prompt,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                requested_model=request.model,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # --- Non-streaming path ---
    try:
        await engine._check_budget()
    except BudgetExceededError as exc:
        raise HTTPException(status_code=402, detail=str(exc))

    try:
        resp = await engine.query(
            prompt=prompt,
            provider=provider_override,
            model=model_override,
            system_prompt=system_prompt,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            stream=False,
        )
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.error("proxy_chat_completions error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    from nvh.api.proxy import format_openai_response
    return format_openai_response(
        content=resp.content,
        model=resp.model or request.model,
        provider=resp.provider or "nvhive",
        prompt_tokens=resp.usage.input_tokens if resp.usage else 0,
        completion_tokens=resp.usage.output_tokens if resp.usage else 0,
        finish_reason=resp.finish_reason.value if resp.finish_reason else "stop",
    )


@app.post(
    "/v1/proxy/completions",
    summary="OpenAI-compatible legacy text completions",
    tags=["proxy"],
)
async def proxy_completions(
    request: _ProxyCompletionsRequest,
    _auth: Any = Depends(require_auth),
):
    """Legacy text completions endpoint (OpenAI v1 style).

    Some older tools (LangChain text-based chains, scripts using the legacy
    ``openai.Completion.create`` API) hit ``/completions`` rather than
    ``/chat/completions``.  This endpoint bridges the gap.
    """
    from nvh.api.proxy import resolve_provider_from_model

    engine = get_engine()

    # Normalise prompt — can be str or list[str]
    prompt_text: str
    if isinstance(request.prompt, list):
        prompt_text = "\n".join(request.prompt)
    else:
        prompt_text = request.prompt

    provider_override, model_override = resolve_provider_from_model(request.model)

    try:
        await engine._check_budget()
    except BudgetExceededError as exc:
        raise HTTPException(status_code=402, detail=str(exc))

    try:
        resp = await engine.query(
            prompt=prompt_text,
            provider=provider_override,
            model=model_override,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            stream=False,
        )
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.error("proxy_completions error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    import time as _time
    import uuid as _uuid
    return {
        "id": f"cmpl-{_uuid.uuid4().hex[:24]}",
        "object": "text_completion",
        "created": int(_time.time()),
        "model": resp.model or request.model,
        "choices": [
            {
                "text": resp.content,
                "index": 0,
                "logprobs": None,
                "finish_reason": resp.finish_reason.value if resp.finish_reason else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": resp.usage.input_tokens if resp.usage else 0,
            "completion_tokens": resp.usage.output_tokens if resp.usage else 0,
            "total_tokens": (resp.usage.total_tokens if resp.usage else 0),
        },
    }


@app.get(
    "/v1/proxy/models",
    summary="OpenAI-compatible models list",
    tags=["proxy"],
)
async def proxy_models(
    _auth: Any = Depends(require_auth),
):
    """Return available models in OpenAI's ``GET /v1/models`` format.

    Includes NVHive virtual routing models (``auto``, ``safe``, ``council``,
    ``throwdown``) and the real models exposed by each enabled provider.
    """
    from nvh.api.proxy import build_models_list
    engine = get_engine()
    return build_models_list(engine.registry)


@app.get(
    "/v1/proxy/health",
    summary="Proxy health check for NemoClaw/OpenShell integration",
    tags=["proxy"],
)
async def proxy_health():
    """Health endpoint for external orchestrators (NemoClaw, OpenShell, etc.).

    Returns provider availability, routing status, and supported virtual models
    so NemoClaw can verify this inference provider is alive and capable.
    """
    engine = get_engine()
    enabled = engine.registry.list_enabled() if hasattr(engine.registry, "list_enabled") else []

    # Check if local inference is available
    has_local = "ollama" in enabled

    return {
        "status": "ok",
        "provider": "nvhive",
        "version": engine.config.version if hasattr(engine.config, "version") else "1.0.0",
        "engine_initialized": engine._initialized,
        "providers_enabled": len(enabled),
        "providers": list(enabled),
        "has_local_inference": has_local,
        "supported_models": [
            "auto", "safe", "local",
            "council", "council:3", "council:5",
            "throwdown",
        ],
        "features": {
            "smart_routing": True,
            "council_consensus": True,
            "throwdown_analysis": True,
            "privacy_header": True,
            "streaming": True,
        },
    }


# ---------------------------------------------------------------------------
# Anthropic/Claude API Proxy  (/v1/anthropic/*)
# ---------------------------------------------------------------------------
# Drop-in replacement for the Anthropic Messages API.
# Set ANTHROPIC_BASE_URL=http://localhost:8000/v1/anthropic in any
# Anthropic SDK client and nvHive handles routing, fallback, and
# cost optimization.
#
# This is the zero-friction migration path for OpenClaw users.
# ---------------------------------------------------------------------------


class AnthropicMessageRequest(BaseModel):
    model: str = "auto"
    messages: list[dict[str, Any]]
    system: str | None = None
    max_tokens: int = Field(default=4096, gt=0)
    temperature: float | None = Field(default=None, ge=0.0, le=1.0)
    stream: bool = False
    metadata: dict[str, Any] | None = None


@app.post(
    "/v1/anthropic/messages",
    summary="Anthropic Messages API proxy",
    tags=["anthropic-proxy"],
)
async def anthropic_messages(
    request: AnthropicMessageRequest,
    raw_request: Request,
):
    """Anthropic Messages API compatible endpoint.

    Drop-in replacement for https://api.anthropic.com/v1/messages.
    Routes through nvHive's smart router with automatic fallback.

    Model routing:
      "auto" / "nvhive"  -> smart routing
      "claude-*"          -> Anthropic provider
      "council" / "council:N" -> multi-model consensus
      "throwdown"         -> two-pass deep analysis
      Any other model     -> nvHive routes to best provider
    """
    from nvh.api.proxy import (
        anthropic_messages_to_nvhive,
        anthropic_stream_generator,
        format_anthropic_response,
        is_throwdown_model,
        parse_council_model,
        resolve_provider_from_model,
    )

    engine = get_engine()
    prompt, system_prompt = anthropic_messages_to_nvhive(
        request.messages, request.system,
    )

    if not prompt:
        raise HTTPException(
            status_code=400,
            detail="No user message content found.",
        )

    # Check for council/throwdown virtual models
    council_size = parse_council_model(request.model)
    is_throwdown = is_throwdown_model(request.model)

    if request.stream and not council_size and not is_throwdown:
        provider_override, model_override = (
            resolve_provider_from_model(request.model)
        )
        return StreamingResponse(
            anthropic_stream_generator(
                engine=engine,
                prompt=prompt,
                provider_override=provider_override,
                model_override=model_override,
                system_prompt=system_prompt,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                requested_model=request.model,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming (or council/throwdown)
    try:
        if council_size:
            result = await engine.run_council(
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                auto_agents=True,
                num_agents=council_size,
            )
            content = (
                result.synthesis.content
                if result.synthesis
                else "Council produced no synthesis."
            )
            return format_anthropic_response(
                content=content,
                model=f"council({council_size})",
                provider="council",
                input_tokens=sum(
                    r.usage.input_tokens
                    for r in result.member_responses.values()
                ),
                output_tokens=sum(
                    r.usage.output_tokens
                    for r in result.member_responses.values()
                ),
            )

        if is_throwdown:
            pass1 = await engine.run_council(
                prompt=prompt,
                auto_agents=True,
                synthesize=True,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
            )
            critique = (
                f"Original: {prompt}\n\n"
                f"Analysis:\n"
                f"{pass1.synthesis.content if pass1.synthesis else ''}"
                f"\n\nCritique and improve this analysis."
            )
            pass2 = await engine.run_council(
                prompt=critique,
                auto_agents=True,
                synthesize=True,
            )
            final = await engine.query(
                prompt=(
                    f"Original: {prompt}\n\n"
                    f"Pass 1:\n"
                    f"{pass1.synthesis.content if pass1.synthesis else ''}"
                    f"\n\nPass 2:\n"
                    f"{pass2.synthesis.content if pass2.synthesis else ''}"
                    f"\n\nFinal definitive answer:"
                ),
            )
            return format_anthropic_response(
                content=final.content,
                model="throwdown",
                provider=final.provider,
                input_tokens=final.usage.input_tokens,
                output_tokens=final.usage.output_tokens,
            )

        # Standard single query with smart routing
        provider_override, model_override = (
            resolve_provider_from_model(request.model)
        )
        resp = await engine.query(
            prompt=prompt,
            provider=provider_override,
            model=model_override,
            system_prompt=system_prompt,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
        )
        return format_anthropic_response(
            content=resp.content,
            model=resp.model,
            provider=resp.provider,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "type": "api_error",
                "message": f"nvHive routing error: {type(exc).__name__}",
            },
        )


# ---------------------------------------------------------------------------
# Integrations API  (/v1/integrations/*)
# ---------------------------------------------------------------------------

@app.get(
    "/v1/integrations/scan",
    summary="Scan for installed AI platforms",
    tags=["integrations"],
)
async def integrations_scan():
    """Detect installed AI platforms and their connection status."""
    from nvh.integrations.detector import detect_platforms

    platforms = detect_platforms()
    results = []
    for p in platforms:
        results.append({
            "name": p.name,
            "display_name": p.display_name,
            "detected": p.detected,
            "already_configured": p.already_configured,
            "detection_method": p.detection_method,
            "config_path": p.config_path,
            "integration_type": p.integration_type,
            "notes": p.notes,
        })

    return _response_envelope({
        "platforms": results,
        "detected_count": sum(1 for p in platforms if p.detected),
        "configured_count": sum(
            1 for p in platforms if p.already_configured
        ),
        "total_count": len(platforms),
    })


class _ConnectRequest(BaseModel):
    platform: str
    host: str = "127.0.0.1"
    port: int = 8000


@app.post(
    "/v1/integrations/connect",
    summary="Connect nvHive to a platform",
    tags=["integrations"],
)
async def integrations_connect(
    request: _ConnectRequest,
    _auth: Any = Depends(require_auth),
):
    """Register nvHive with a detected platform."""
    from nvh.integrations.detector import (
        register_claude_code,
        register_claude_desktop,
        register_cursor,
        register_nemoclaw,
        register_openclaw,
    )

    handlers = {
        "nemoclaw": lambda: register_nemoclaw(
            request.host, request.port
        ),
        "openclaw": lambda: register_openclaw(),
        "claude_code": lambda: register_claude_code(),
        "cursor": lambda: register_cursor(),
        "claude_desktop": lambda: register_claude_desktop(),
    }

    handler = handlers.get(request.platform)
    if not handler:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown platform: {request.platform}",
        )

    success, message = handler()
    return _response_envelope({
        "platform": request.platform,
        "success": success,
        "message": message,
    })


class _ConnectAllRequest(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


@app.post(
    "/v1/integrations/connect-all",
    summary="Auto-connect all detected platforms",
    tags=["integrations"],
)
async def integrations_connect_all(
    request: _ConnectAllRequest,
    _auth: Any = Depends(require_auth),
):
    """Scan and connect all detected platforms in one call."""
    from nvh.integrations.detector import (
        detect_platforms,
        register_claude_code,
        register_claude_desktop,
        register_cursor,
        register_nemoclaw,
        register_openclaw,
    )

    handlers = {
        "nemoclaw": lambda: register_nemoclaw(
            request.host, request.port
        ),
        "openclaw": lambda: register_openclaw(),
        "claude_code": lambda: register_claude_code(),
        "cursor": lambda: register_cursor(),
        "claude_desktop": lambda: register_claude_desktop(),
    }

    platforms = detect_platforms()
    results = []

    for p in platforms:
        if not p.detected or p.already_configured:
            results.append({
                "platform": p.name,
                "display_name": p.display_name,
                "action": "skipped",
                "reason": (
                    "already configured" if p.already_configured
                    else "not detected"
                ),
                "success": p.already_configured,
            })
            continue

        handler = handlers.get(p.name)
        if handler:
            success, message = handler()
            results.append({
                "platform": p.name,
                "display_name": p.display_name,
                "action": "connected" if success else "failed",
                "message": message,
                "success": success,
            })

    connected = sum(1 for r in results if r["success"])
    return _response_envelope({
        "results": results,
        "connected": connected,
        "total": len(results),
    })


# ---------------------------------------------------------------------------
# Quota Info API  (/v1/quota/*)
# ---------------------------------------------------------------------------

@app.get(
    "/v1/quota/{provider_name}",
    summary="Get quota and rate limit info for a provider",
    tags=["quota"],
)
async def get_provider_quota(provider_name: str):
    """Return quota details, rate limits, reset times, and upgrade links."""
    from nvh.providers.quota_info import get_quota_info

    info = get_quota_info(provider_name)
    return _response_envelope({
        "provider": info.provider,
        "tier": info.tier,
        "limit": info.limit_description,
        "reset": info.reset_hint,
        "upgrade_url": info.upgrade_url,
        "upgrade_hint": info.upgrade_hint,
    })


@app.get(
    "/v1/quota",
    summary="Get quota info for all configured providers",
    tags=["quota"],
)
async def get_all_quotas():
    """Return quota details for every enabled provider."""
    from nvh.providers.quota_info import get_quota_info

    engine = get_engine()
    enabled = (
        engine.registry.list_enabled()
        if hasattr(engine.registry, "list_enabled")
        else []
    )

    quotas = []
    for name in enabled:
        info = get_quota_info(name)
        quotas.append({
            "provider": info.provider,
            "tier": info.tier,
            "limit": info.limit_description,
            "reset": info.reset_hint,
            "upgrade_url": info.upgrade_url,
            "upgrade_hint": info.upgrade_hint,
        })

    return _response_envelope({"quotas": quotas})
