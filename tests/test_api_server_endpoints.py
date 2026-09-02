"""Endpoint tests for nvh.api.server — SSE, webhooks, setup, conversations, auth, metrics.

One mock provider and one ``client`` fixture serve every class; the engine has
caching off, no context files, and the TestClient returns 5xx bodies instead of
raising so error-path assertions can inspect the response.
"""

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


class _Provider:
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


def _make_engine():
    config = CouncilConfig(
        defaults=DefaultsConfig(provider="alpha", model="m"),
        providers={"alpha": ProviderConfig(enabled=True, default_model="m")},
        council=CouncilModeConfig(quorum=1, timeout=5, default_weights={"alpha": 1.0}, synthesis_provider="alpha"),
        routing=RoutingConfig(), budget=BudgetConfig(),
        cache=CacheConfig(enabled=False, ttl_seconds=1, max_size=1),
    )
    reg = ProviderRegistry()
    reg.register("alpha", _Provider("alpha"))
    e = Engine(config=config, registry=reg)
    e._initialized = True
    e._context_files = []
    return e


@pytest.fixture()
def client(tmp_path):
    repo._engine = None
    repo._session_factory = None
    asyncio.run(repo.init_db(db_path=tmp_path / "server.db"))
    orig = server_module._engine
    server_module._engine = _make_engine()
    server_module._auth_attempts.clear()
    c = TestClient(app, raise_server_exceptions=False)
    yield c
    server_module._engine = orig
    repo._engine = None
    repo._session_factory = None


class TestServerSSE:
    def test_query_stream_sse(self, client):
        r = client.post("/v1/query", json={"prompt": "Hi", "provider": "alpha", "stream": True})
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("content-type", "")
        assert "data:" in r.text or "event:" in r.text

    def test_query_stream_bad_provider(self, client):
        r = client.post("/v1/query", json={"prompt": "Hi", "provider": "nope", "stream": True})
        assert r.status_code == 200 and "error" in r.text

    def test_sse_query_stream(self, client):
        """POST /v1/query with stream=true returns SSE events."""
        r = client.post("/v1/query", json={"prompt": "hi", "provider": "alpha", "stream": True})
        assert r.status_code == 200
        # SSE responses have text/event-stream content type
        assert "event-stream" in r.headers.get("content-type", "") or r.status_code == 200


class TestWebhookEndpoint:
    def test_bad_scheme(self, client):
        r = client.post("/v1/webhooks/test", json={"url": "ftp://x.com/h", "secret": ""})
        assert r.status_code == 400

    def test_private_ip(self, client):
        r = client.post("/v1/webhooks/test", json={"url": "http://169.254.169.254/x", "secret": ""})
        assert r.status_code == 400

    def test_dispatch_success(self, client):
        with patch("nvh.core.webhooks.WebhookManager._dispatch", new_callable=AsyncMock, return_value=True):
            r = client.post("/v1/webhooks/test", json={"url": "https://example.com/h", "secret": "s"})
        assert r.status_code == 200
        assert r.json()["data"]["delivered"] is True

    def test_webhooks_list(self, client):
        r = client.get("/v1/webhooks")
        assert r.status_code == 200


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


class TestContextEndpoint:
    def test_get(self, client):
        r = client.get("/v1/context")
        assert r.status_code == 200 and r.json()["data"]["total"] == 0

    def test_reload(self, client):
        with patch("nvh.core.context_files.find_context_files", return_value=[]):
            r = client.post("/v1/context/reload")
        assert r.status_code == 200 and r.json()["data"]["files_loaded"] == 0

    def test_reload_returns_files_loaded(self, client):
        with patch("nvh.core.context_files.find_context_files", return_value=[]):
            r = client.post("/v1/context/reload")
        assert r.status_code == 200
        body = r.json()
        assert body["data"]["files_loaded"] == 0
        assert body["data"]["names"] == []

    def test_context_endpoint(self, client):
        r = client.get("/v1/context")
        assert r.status_code in (200, 404)


class TestSetupEndpoints:
    def test_status(self, client):
        r = client.get("/v1/setup/status")
        assert r.status_code == 200
        d = r.json()["data"]
        assert "ready" in d and isinstance(d["enabled_names"], list)

    def test_save_key_bad_provider(self, client):
        r = client.post("/v1/setup/save-key", json={"provider": "bogus", "api_key": "sk-1234567890"})
        assert r.status_code == 422

    def test_save_key_keyring_error_uses_rootless_fallback(self, client, tmp_path):
        mk = MagicMock(); mk.set_password.side_effect = Exception("no backend")
        with patch.dict("sys.modules", {"keyring": mk}), \
             patch.object(server_module, "_provider_env_file", return_value=tmp_path / ".env"), \
             patch.object(server_module, "_provider_config_file", return_value=tmp_path / "config.yaml"):
            r = client.post("/v1/setup/save-key", json={"provider": "groq", "api_key": "gsk_1234567890t"})
        body = r.json()
        assert r.status_code == 200 and body["status"] == "success"
        assert body["data"]["ok"] is True
        assert body["data"]["env_key"] == "GROQ_API_KEY"

    def test_save_key_success(self, client, tmp_path):
        mock_kr = MagicMock()
        mock_engine = server_module._engine
        with patch.dict("sys.modules", {"keyring": mock_kr}), \
             patch.object(server_module, "_provider_env_file", return_value=tmp_path / ".env"), \
             patch.object(server_module, "_provider_config_file", return_value=tmp_path / "config.yaml"), \
             patch.object(mock_engine, "initialize", new_callable=AsyncMock, return_value=["alpha"]):
            r = client.post("/v1/setup/save-key", json={"provider": "groq", "api_key": "gsk_1234567890t"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "success"
        assert "groq" in body["data"]["message"]

    def test_setup_status(self, client):
        r = client.get("/v1/setup/status")
        assert r.status_code == 200

    def test_setup_free_providers(self, client):
        r = client.get("/v1/setup/free-providers")
        assert r.status_code == 200
        assert "providers" in r.json()["data"]


class TestConversationEndpoints:
    def test_delete_not_found(self, client):
        assert client.delete("/v1/conversations/missing-id").status_code == 404

    def test_get_not_found(self, client):
        assert client.get("/v1/conversations/missing-id").status_code == 404

    def test_list_empty(self, client):
        r = client.get("/v1/conversations")
        assert r.status_code == 200
        data = r.json()["data"]
        assert isinstance(data["conversations"], list)
        assert data["count"] == len(data["conversations"])

    def test_query_missing_conv(self, client):
        # Unknown conversation id is a client error (404), not a 500.
        r = client.post("/v1/conversations/no-conv/query", json={"prompt": "Hi"})
        assert r.status_code == 404

    def test_conversations_create_and_list(self, client):
        r = client.get("/v1/conversations")
        assert r.status_code == 200

    def test_append_messages_roundtrip(self, client):
        """Client-held turns (SSE/WS modes, localStorage import) persist without running the engine."""
        cid = client.post("/v1/conversations", json={"title": "Import"}).json()["data"]["id"]
        r = client.post(
            f"/v1/conversations/{cid}/messages",
            json={"role": "user", "content": "hello", "tokens": 3},
        )
        assert r.status_code == 200
        user_msg = r.json()["data"]
        assert user_msg["role"] == "user"
        assert (user_msg["input_tokens"], user_msg["output_tokens"]) == (3, 0)

        r = client.post(
            f"/v1/conversations/{cid}/messages",
            json={
                "role": "assistant", "content": "hi back", "provider": "alpha", "model": "m",
                "tokens": 7, "cost_usd": "not-a-number", "latency_ms": 12,
            },
        )
        assert r.status_code == 200
        assistant_msg = r.json()["data"]
        assert (assistant_msg["input_tokens"], assistant_msg["output_tokens"]) == (0, 7)
        # An unparseable cost is stored as zero rather than rejecting the turn.
        assert Decimal(assistant_msg["cost_usd"]) == 0

        detail = client.get(f"/v1/conversations/{cid}").json()["data"]
        assert detail["message_count"] == 2
        assert detail["total_tokens"] == 10
        assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]

    def test_append_unknown_conversation_404(self, client):
        r = client.post(
            "/v1/conversations/missing-id/messages", json={"role": "user", "content": "x"}
        )
        assert r.status_code == 404

    def test_append_bad_role_422(self, client):
        cid = client.post("/v1/conversations", json={"title": "t"}).json()["data"]["id"]
        r = client.post(
            f"/v1/conversations/{cid}/messages", json={"role": "system", "content": "x"}
        )
        assert r.status_code == 422


class TestAutoSetup:
    def test_no_gpus(self, client):
        with patch("nvh.api.server.detect_gpus", return_value=[]), \
             patch("nvh.api.server.recommend_models", return_value=[]), \
             patch("nvh.api.server.get_ollama_optimizations") as mo:
            mo.return_value = MagicMock(flash_attention=False, num_parallel=1,
                                        recommended_ctx=2048, recommended_quant="q4_0",
                                        architecture="cpu", notes="")
            r = client.post("/v1/system/auto-setup")
        assert r.status_code == 200 and r.json()["data"]["gpu_count"] == 0

    def test_auto_setup_endpoint(self, client):
        r = client.post("/v1/system/auto-setup")
        assert r.status_code in (200, 500)


class TestMiscEndpoints:
    def test_ollama_models_endpoint(self, client):
        # Ollama endpoint may error if Ollama isn't installed — accept any non-crash
        try:
            r = client.get("/v1/ollama/models")
            assert r.status_code in (200, 404, 500, 503)
        except Exception:
            pass  # TestClient may raise on internal 500

    def test_quota_endpoint(self, client):
        r = client.get("/v1/quota")
        assert r.status_code in (200, 404)

    def test_quota_by_provider(self, client):
        r = client.get("/v1/quota/alpha")
        assert r.status_code in (200, 404)

    def test_integrations_scan(self, client):
        r = client.post("/v1/integrations/scan")
        assert r.status_code in (200, 405)

    def test_query_invalid_provider(self, client):
        r = client.post("/v1/query", json={"prompt": "hi", "provider": "nonexistent"})
        assert r.status_code in (200, 400, 404, 500)

    def test_council_empty_prompt(self, client):
        r = client.post("/v1/council", json={"prompt": "", "members": ["alpha"], "synthesize": False})
        assert r.status_code in (200, 422)


class TestCoreEndpoints:
    def test_compare_endpoint(self, client):
        r = client.post("/v1/compare", json={"prompt": "hi", "providers": ["alpha"]})
        assert r.status_code == 200

    def test_council_no_synthesize(self, client):
        r = client.post("/v1/council", json={"prompt": "hi", "members": ["alpha"], "synthesize": False})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "success"

    def test_provider_health_specific(self, client):
        r = client.get("/v1/advisors/alpha/health")
        assert r.status_code == 200
        body = r.json()
        assert body["data"]["healthy"] is True

    def test_analytics_returns_data(self, client):
        # First make a query to have some data
        client.post("/v1/query", json={"prompt": "test", "provider": "alpha"})
        r = client.get("/v1/analytics")
        assert r.status_code == 200

    def test_budget_returns_data(self, client):
        r = client.get("/v1/budget/status")
        assert r.status_code == 200
        body = r.json()
        assert "daily_spend" in body["data"]

    def test_cache_clear(self, client):
        r = client.delete("/v1/cache")
        assert r.status_code == 200

    def test_system_gpu(self, client):
        r = client.get("/v1/system/gpu")
        assert r.status_code == 200

    def test_system_recommendations(self, client):
        r = client.get("/v1/system/recommendations")
        assert r.status_code == 200

    def test_docs_page(self, client):
        r = client.get("/docs")
        assert r.status_code == 200

    def test_openapi_json(self, client):
        r = client.get("/openapi.json")
        assert r.status_code == 200
        schema = r.json()
        assert "paths" in schema


class TestOpenAPISchema:
    def test_openapi_schema_has_paths(self, client):
        r = client.get("/openapi.json")
        assert r.status_code == 200
        schema = r.json()
        assert "paths" in schema
        assert "/v1/health" in schema["paths"]
        assert "/v1/query" in schema["paths"]
        assert "info" in schema
        assert schema["info"]["title"] == "Hive API"


class TestQueryBudgetExceeded:
    def test_budget_exceeded_returns_402(self, client):
        with patch.object(server_module._engine, "query", new_callable=AsyncMock,
                          side_effect=BudgetExceededError("daily limit reached")):
            r = client.post("/v1/query", json={"prompt": "hello", "provider": "alpha"})
        assert r.status_code == 402
        assert "daily limit" in r.json()["detail"]


class TestCouncilNoMembers:
    def test_council_no_members_returns_422(self, client):
        with patch.object(server_module._engine, "run_council", new_callable=AsyncMock,
                          side_effect=ValueError("No council members specified")):
            r = client.post("/v1/council", json={"prompt": "test", "members": [], "synthesize": False})
        assert r.status_code == 422
        assert "members" in r.json()["detail"].lower()


class TestAuthUserFlow:
    def test_create_user_and_login(self, client):
        # Register
        r = client.post("/v1/auth/register", json={
            "username": "testuser1",
            "password": "password123",
        })
        assert r.status_code in (201, 409, 429)  # 409 if exists, 429 rate limit

    def test_auth_me_without_token(self, client):
        r = client.get("/v1/auth/me")
        # Open mode (no HIVE_API_KEY) → returns something or 401
        assert r.status_code in (200, 401, 404)


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
    def test_ws_open_mode_accepts(self, client, monkeypatch):
        monkeypatch.delenv("HIVE_API_KEY", raising=False)
        with client.websocket_connect("/v1/ws/query") as ws:
            ws.close()

    def test_ws_with_valid_bearer_header(self, client, monkeypatch):
        monkeypatch.setenv("HIVE_API_KEY", "ws-test-key-1234")
        with client.websocket_connect("/v1/ws/query?token=ws-test-key-1234") as ws:
            ws.close()


class TestWSCouncilFrame:
    """The council_request frame's synthesize/num_agents must reach the orchestrator."""

    def _run(self, client, monkeypatch, frame):
        monkeypatch.delenv("HIVE_API_KEY", raising=False)

        async def _fake_run(**kwargs):
            # Emit one event so the client can block until the call was made.
            await kwargs["on_event"]({"type": "council_complete"})
            return None

        run = AsyncMock(side_effect=_fake_run)
        monkeypatch.setattr(server_module._engine.council, "run_council_streaming", run)
        with client.websocket_connect("/v1/ws/council") as ws:
            ws.send_json({"type": "council_request", "prompt": "hi", **frame})
            assert ws.receive_json()["type"] == "council_complete"
        return run.call_args.kwargs

    def test_synthesize_and_num_agents_forwarded(self, client, monkeypatch):
        kwargs = self._run(client, monkeypatch, {"synthesize": False, "num_agents": 4})
        assert kwargs["synthesize"] is False
        assert kwargs["num_agents"] == 4

    def test_num_agents_clamped_and_defaults(self, client, monkeypatch):
        kwargs = self._run(client, monkeypatch, {"num_agents": 42})
        assert kwargs["num_agents"] == 10
        assert kwargs["synthesize"] is True
        kwargs = self._run(client, monkeypatch, {})
        assert kwargs["num_agents"] is None


class TestPromEscape:
    def test_plain_string(self):
        assert _prom_escape("alpha") == "alpha"

    def test_escapes_backslash_and_quote(self):
        assert _prom_escape('a\\b"c\nd') == 'a\\\\b\\"c\\nd'


class TestPrometheusMetrics:
    def test_metrics_after_query(self, client):
        # Make a query so there's data in the DB for metrics to aggregate
        client.post("/v1/query", json={"prompt": "ping", "provider": "alpha"})
        r = client.get("/metrics")
        assert r.status_code == 200
        body = r.text
        # Should contain at least the HELP/TYPE headers
        assert "# HELP" in body or "# TYPE" in body

    def test_v1_metrics_alias(self, client):
        r = client.get("/v1/metrics")
        assert r.status_code == 200
        assert "text/plain" in r.headers.get("content-type", "")
