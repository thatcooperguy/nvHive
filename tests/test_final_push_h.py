"""Final coverage push H — server.py uncovered paths."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import nvh.api.server as server_module
import nvh.storage.repository as repo
from nvh.api.server import (
    _check_auth_rate_limit,
    _prom_escape,
    _validate_webhook_url,
    app,
)
from nvh.config.settings import (
    BudgetConfig,
    CacheConfig,
    CouncilConfig,
    CouncilModeConfig,
    DefaultsConfig,
    ProviderConfig,
    RoutingConfig,
)
from nvh.core.engine import BudgetExceededError, Engine
from nvh.providers.base import (
    CompletionResponse,
    FinishReason,
    HealthStatus,
    ModelInfo,
    StreamChunk,
    Usage,
)
from nvh.providers.registry import ProviderRegistry


class _Prov:
    def __init__(self, name="alpha"):
        self._name = name

    @property
    def name(self):
        return self._name

    async def complete(self, messages, model=None, **kw):
        return CompletionResponse(
            content=f"ok from {self._name}", model=model or "m",
            provider=self._name, usage=Usage(input_tokens=5, output_tokens=10, total_tokens=15),
            cost_usd=Decimal("0.001"), latency_ms=10)

    async def stream(self, messages, model=None, **kw):
        yield StreamChunk(delta="ok", is_final=True, accumulated_content="ok",
                          model=model or "m", provider=self._name,
                          usage=Usage(input_tokens=5, output_tokens=10, total_tokens=15),
                          cost_usd=Decimal("0.001"), finish_reason=FinishReason.STOP)

    async def list_models(self):
        return [ModelInfo(model_id="m", provider=self._name)]

    async def health_check(self):
        return HealthStatus(provider=self._name, healthy=True, latency_ms=1)

    def estimate_tokens(self, text):
        return max(1, len(text) // 4)


def _mk():
    config = CouncilConfig(
        defaults=DefaultsConfig(provider="alpha", model="m"),
        providers={"alpha": ProviderConfig(enabled=True, default_model="m")},
        council=CouncilModeConfig(quorum=1, timeout=5, default_weights={"alpha": 1.0}, synthesis_provider="alpha"),
        routing=RoutingConfig(), budget=BudgetConfig(),
        cache=CacheConfig(enabled=False, ttl_seconds=1, max_size=1),
    )
    reg = ProviderRegistry()
    reg.register("alpha", _Prov("alpha"))
    e = Engine(config=config, registry=reg)
    e._initialized = True
    e._context_files = []
    return e


@pytest.fixture()
def cli(tmp_path):
    db = tmp_path / "h.db"
    repo._engine = None; repo._session_factory = None
    asyncio.run(repo.init_db(db_path=db))
    e = _mk()
    orig = server_module._engine
    server_module._engine = e
    server_module._auth_attempts.clear()
    c = TestClient(app, raise_server_exceptions=False)
    yield c
    server_module._engine = orig
    repo._engine = None; repo._session_factory = None


class TestPromEscape:
    def test_plain_string(self):
        assert _prom_escape("alpha") == "alpha"

    def test_escapes_backslash_and_quote(self):
        assert _prom_escape('a\\b"c\nd') == 'a\\\\b\\"c\\nd'


class TestWebhookSSRF:
    def test_rejects_ftp_scheme(self):
        with pytest.raises(ValueError, match="http"):
            _validate_webhook_url("ftp://example.com/hook")

    def test_rejects_private_ip(self):
        with pytest.raises(ValueError, match="private"):
            _validate_webhook_url("http://192.168.1.1/hook")

    def test_rejects_loopback(self):
        with pytest.raises(ValueError, match="private"):
            _validate_webhook_url("http://127.0.0.1/hook")

    def test_rejects_cloud_metadata(self):
        with pytest.raises(ValueError, match="metadata"):
            _validate_webhook_url("http://169.254.169.254/latest")

    def test_rejects_no_hostname(self):
        with pytest.raises(ValueError, match="Invalid"):
            _validate_webhook_url("http:///no-host")

    def test_accepts_public_url(self):
        _validate_webhook_url("https://hooks.slack.com/services/T/B/X")

    def test_accepts_hostname_not_ip(self):
        _validate_webhook_url("https://example.com/webhook")


class TestSaveKeyValid:
    def test_save_key_success(self, cli):
        mock_kr = MagicMock()
        mock_engine = server_module._engine
        with patch.dict("sys.modules", {"keyring": mock_kr}), \
             patch.object(mock_engine, "initialize", new_callable=AsyncMock, return_value=["alpha"]):
            r = cli.post("/v1/setup/save-key", json={"provider": "groq", "api_key": "gsk_1234567890t"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "success"
        assert "groq" in body["data"]["message"]


class TestContextReload:
    def test_reload_returns_files_loaded(self, cli):
        with patch("nvh.core.context_files.find_context_files", return_value=[]):
            r = cli.post("/v1/context/reload")
        assert r.status_code == 200
        body = r.json()
        assert body["data"]["files_loaded"] == 0
        assert body["data"]["names"] == []


class TestQueryBudgetExceeded:
    def test_budget_exceeded_returns_402(self, cli):
        with patch.object(server_module._engine, "query", new_callable=AsyncMock,
                          side_effect=BudgetExceededError("daily limit reached")):
            r = cli.post("/v1/query", json={"prompt": "hello", "provider": "alpha"})
        assert r.status_code == 402
        assert "daily limit" in r.json()["detail"]


class TestCouncilNoMembers:
    def test_council_no_members_returns_422(self, cli):
        with patch.object(server_module._engine, "run_council", new_callable=AsyncMock,
                          side_effect=ValueError("No council members specified")):
            r = cli.post("/v1/council", json={"prompt": "test", "members": [], "synthesize": False})
        assert r.status_code == 422
        assert "members" in r.json()["detail"].lower()


class TestOpenAPISchema:
    def test_openapi_schema_has_paths(self, cli):
        r = cli.get("/openapi.json")
        assert r.status_code == 200
        schema = r.json()
        assert "paths" in schema
        assert "/v1/health" in schema["paths"]
        assert "/v1/query" in schema["paths"]
        assert "info" in schema
        assert schema["info"]["title"] == "Hive API"


class TestAuthRateLimit:
    def test_allows_under_limit(self):
        server_module._auth_attempts.clear()
        # Should not raise for first call
        _check_auth_rate_limit("10.0.0.99")

    def test_blocks_over_limit(self):
        server_module._auth_attempts.clear()
        ip = "10.0.0.100"
        for _ in range(server_module.AUTH_RATE_LIMIT):
            _check_auth_rate_limit(ip)
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            _check_auth_rate_limit(ip)
        assert exc_info.value.status_code == 429

    def test_old_attempts_expire(self):
        server_module._auth_attempts.clear()
        ip = "10.0.0.101"
        # Insert stale timestamps older than 60 seconds
        server_module._auth_attempts[ip] = [time.time() - 120] * 10
        # Should not raise because all attempts are expired
        _check_auth_rate_limit(ip)


class TestWSAuthOpen:
    def test_ws_open_mode_accepts(self, cli, monkeypatch):
        monkeypatch.delenv("HIVE_API_KEY", raising=False)
        with cli.websocket_connect("/v1/ws/query") as ws:
            ws.close()

    def test_ws_with_valid_bearer_header(self, cli, monkeypatch):
        monkeypatch.setenv("HIVE_API_KEY", "ws-test-key-1234")
        with cli.websocket_connect("/v1/ws/query?token=ws-test-key-1234") as ws:
            ws.close()


class TestPrometheusMetrics:
    def test_metrics_after_query(self, cli):
        # Make a query so there's data in the DB for metrics to aggregate
        cli.post("/v1/query", json={"prompt": "ping", "provider": "alpha"})
        r = cli.get("/metrics")
        assert r.status_code == 200
        body = r.text
        # Should contain at least the HELP/TYPE headers
        assert "# HELP" in body or "# TYPE" in body

    def test_v1_metrics_alias(self, cli):
        r = cli.get("/v1/metrics")
        assert r.status_code == 200
        assert "text/plain" in r.headers.get("content-type", "")
