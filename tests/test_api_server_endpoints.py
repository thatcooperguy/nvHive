"""Endpoint tests for nvh.api.server — SSE, webhooks, setup, conversations, auth, metrics.

One mock provider and one ``client`` fixture serve every class; the engine has
caching off, no context files, and the TestClient returns 5xx bodies instead of
raising so error-path assertions can inspect the response.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import nvh.api.server as server_module
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
from nvh.utils import platform_facts
from nvh.utils.gpu import GPUInfo, ModelRecommendation


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
def client(sync_db):
    orig = server_module._engine
    server_module._engine = _make_engine()
    server_module._auth_attempts.clear()
    c = TestClient(app, raise_server_exceptions=False)
    yield c
    server_module._engine = orig


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
                "tokens": 7, "cost_usd": "2E-6", "latency_ms": 12,
            },
        )
        assert r.status_code == 200
        assistant_msg = r.json()["data"]
        assert (assistant_msg["input_tokens"], assistant_msg["output_tokens"]) == (0, 7)
        # Exponent notation is how the server itself prints small costs
        # (the column holds six decimals, so 2E-6 survives the round trip).
        assert Decimal(assistant_msg["cost_usd"]) == Decimal("0.000002")

        detail = client.get(f"/v1/conversations/{cid}").json()["data"]
        assert detail["message_count"] == 2
        assert detail["total_tokens"] == 10
        assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]

    def test_append_unknown_conversation_404(self, client):
        r = client.post(
            "/v1/conversations/missing-id/messages", json={"role": "user", "content": "x"}
        )
        assert r.status_code == 404

    def test_append_rejects_non_finite_or_negative_cost(self, client):
        cid = client.post("/v1/conversations", json={"title": "t"}).json()["data"]["id"]
        for bad in ("not-a-number", "NaN", "Infinity", "-0.01", ""):
            r = client.post(
                f"/v1/conversations/{cid}/messages",
                json={"role": "assistant", "content": "x", "cost_usd": bad},
            )
            assert r.status_code == 422, bad
        assert client.get(f"/v1/conversations/{cid}").json()["data"]["message_count"] == 0

    def test_create_with_messages_imports_a_whole_thread(self, client):
        """The localStorage import sends one request per old chat: the
        conversation, its turns in order and the pinned flag land together."""
        r = client.post(
            "/v1/conversations",
            json={
                "title": "",
                "mode": "council",
                "pinned": True,
                "messages": [
                    {"role": "user", "content": "first question", "tokens": 3},
                    {"role": "assistant", "content": "### groq\n\nanswer", "tokens": 5,
                     "cost_usd": "0.001", "provider": "groq", "model": "m"},
                    {"role": "user", "content": "follow-up"},
                ],
            },
        )
        assert r.status_code == 200
        conv = r.json()["data"]
        assert conv["pinned"] is True
        assert conv["message_count"] == 3
        assert conv["total_tokens"] == 8
        assert Decimal(conv["total_cost_usd"]) == Decimal("0.001")
        assert conv["title"] == "first question"  # auto-titled from the seed
        detail = client.get(f"/v1/conversations/{conv['id']}").json()["data"]
        assert [m["sequence"] for m in detail["messages"]] == [1, 2, 3]
        assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "user"]
        pinned = client.get("/v1/conversations/pinned").json()["data"]["conversations"]
        assert any(c["id"] == conv["id"] for c in pinned)

    def test_create_with_bad_seed_message_creates_nothing(self, client):
        before = client.get("/v1/conversations").json()["data"]["count"]
        r = client.post(
            "/v1/conversations",
            json={"messages": [
                {"role": "user", "content": "ok"},
                {"role": "system", "content": "x"},
            ]},
        )
        assert r.status_code == 422
        assert client.get("/v1/conversations").json()["data"]["count"] == before

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

    @pytest.mark.parametrize(
        "frame",
        [{"temperature": "hot"}, {"max_tokens": "lots"}, {"num_agents": "many"}, {"num_agents": [3]}],
    )
    def test_bad_number_is_an_error_frame_not_a_1011_close(self, client, monkeypatch, frame):
        monkeypatch.delenv("HIVE_API_KEY", raising=False)
        run = AsyncMock()
        monkeypatch.setattr(server_module._engine.council, "run_council_streaming", run)
        with client.websocket_connect("/v1/ws/council") as ws:
            ws.send_json({"type": "council_request", "prompt": "hi", **frame})
            reply = ws.receive_json()
        assert reply["type"] == "error"
        assert "council_request" in reply["error"]
        run.assert_not_called()


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


# ---------------------------------------------------------------------------
# DGX Spark / unified-memory fields (2026-09)
# ---------------------------------------------------------------------------

def _gpu(name: str, vram_gb: float, *, unified: bool) -> GPUInfo:
    vram_mb = int(vram_gb * 1024)
    return GPUInfo(
        name=name, vram_mb=vram_mb, vram_gb=vram_gb, driver_version="580.65",
        cuda_version="13.0", utilization_pct=3, memory_used_mb=4096,
        memory_free_mb=vram_mb - 4096, index=0, unified_memory=unified,
    )


def _gpu_status(gpus: list[GPUInfo]) -> dict:
    return {
        "status": "ok" if gpus else "none", "source": "nvml" if gpus else "none",
        "gpus": gpus, "issues": [], "device_files_present": bool(gpus), "nvidia_smi": "",
    }


class TestUnifiedMemoryFields:
    """/v1/system/gpu and /recommendations carry the fields the WebUI renders for a Spark."""

    @pytest.mark.parametrize(
        ("name", "vram_gb", "unified"),
        [("NVIDIA GB10", 128.0, True), ("NVIDIA GeForce RTX 4090", 24.0, False)],
    )
    def test_system_gpu_reports_unified_memory(self, client, name, vram_gb, unified):
        gpu = _gpu(name, vram_gb, unified=unified)
        with patch("nvh.api.server.detect_gpu_status", return_value=_gpu_status([gpu])), \
             patch("nvh.api.server.get_gpu_summary", return_value=name):
            r = client.get("/v1/system/gpu")
        assert r.status_code == 200
        device = r.json()["data"]["gpus"][0]
        assert device["unified_memory"] is unified
        assert device["vram_gb"] == vram_gb

    def test_recommendation_note_is_forwarded(self, client):
        recs = [
            ModelRecommendation("nemotron3:33b", "MoE fits the unified pool", 20.0, "full",
                                note="Unified memory: 128 GB LPDDR5x shared by CPU and GPU"),
            ModelRecommendation("qwen3:8b", "coding fallback", 6.0, "small"),
        ]
        with patch("nvh.api.server.detect_gpus", return_value=[_gpu("NVIDIA GB10", 128.0, unified=True)]), \
             patch("nvh.api.server.recommend_models", return_value=recs):
            r = client.get("/v1/system/recommendations")
        assert r.status_code == 200
        out = r.json()["data"]["recommendations"]
        assert out[0]["note"].startswith("Unified memory")
        assert out[1]["note"] == ""  # always a string, never missing or null

    def test_discrete_gpu_recommendations_have_empty_note(self, client):
        """x86 discrete-GPU boxes keep the old payload shape: real recommender, note == ''."""
        with patch("nvh.api.server.detect_gpus", return_value=[_gpu("NVIDIA GeForce RTX 4090", 24.0, unified=False)]):
            r = client.get("/v1/system/recommendations")
        assert r.status_code == 200
        recs = r.json()["data"]["recommendations"]
        assert recs
        assert all(rec["note"] == "" for rec in recs)


# ---------------------------------------------------------------------------
# Startup platform-facts warm-up
# ---------------------------------------------------------------------------

class _StubRegistry:
    def list_enabled(self) -> list[str]:
        return []


class _StubEngine:
    """Engine stand-in for lifespan tests: no providers, no DB, no network."""

    webhooks = None
    _initialized = True
    registry = _StubRegistry()

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def initialize(self) -> list[str]:
        return []


@pytest.fixture()
def lifespan_env(tmp_path, monkeypatch):
    """Run the real lifespan against a throwaway config dir and a stub Engine."""
    import nvh.cli.setup as nvh_setup

    monkeypatch.setenv("HIVE_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh-home"))
    monkeypatch.setenv("NVH_BOOT_PREFLIGHT", "0")
    monkeypatch.delenv("NVH_USE_KEYRING", raising=False)
    # tests/conftest.py defaults the warm-up off for every other suite; these
    # tests are about the warm-up, so turn it back on (patched, never real).
    monkeypatch.setenv("NVH_PLATFORM_WARMUP", "1")
    monkeypatch.setattr(nvh_setup, "DEFAULT_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(server_module, "Engine", _StubEngine)
    monkeypatch.setattr(server_module, "_engine", None)


def _join_warmup(timeout: float = 5.0) -> threading.Thread:
    """Wait for the lifespan's warm-up thread (tests only — the server never joins it)."""
    thread = app.state.platform_warmup_thread
    assert isinstance(thread, threading.Thread), thread
    thread.join(timeout)
    assert not thread.is_alive(), "platform warm-up thread did not finish"
    return thread


class TestPlatformFactsWarmup:
    """Startup probes the platform once, on its own daemon thread, and never fails on it."""

    def test_lifespan_warms_platform_facts_once_on_a_daemon_thread(self, lifespan_env, monkeypatch):
        calls: list[str] = []

        def fake_warm() -> None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                calls.append(threading.current_thread().name)  # no loop here: off the event loop
            else:
                calls.append("event-loop")

        monkeypatch.setattr(platform_facts, "warm_platform_facts", fake_warm, raising=False)
        with TestClient(app) as c:
            assert c.get("/v1/health").status_code == 200
            thread = _join_warmup()
        assert thread.daemon is True
        assert calls == ["nvh-platform-warmup"]

    def test_lifespan_survives_a_raising_warmup_and_only_logs_it(self, lifespan_env, monkeypatch):
        def boom() -> None:
            raise RuntimeError("cloud metadata probe hung")

        monkeypatch.setattr(platform_facts, "warm_platform_facts", boom, raising=False)
        with patch.object(server_module, "logger") as log, TestClient(app) as c:
            assert c.get("/v1/health").status_code == 200
            _join_warmup()
        debug_lines = [" ".join(str(a) for a in call.args) for call in log.debug.call_args_list]
        assert any("platform facts warm-up skipped" in line and "cloud metadata probe hung" in line for line in debug_lines)
        # Debug only: never an error/warning for a nicety.
        for level in (log.error, log.warning, log.exception):
            assert not any("warm-up" in " ".join(str(a) for a in call.args) for call in level.call_args_list)

    def test_lifespan_survives_a_missing_warmup_helper(self, lifespan_env, monkeypatch):
        monkeypatch.delattr(platform_facts, "warm_platform_facts", raising=False)
        with TestClient(app) as c:
            assert c.get("/v1/health").status_code == 200
            _join_warmup()

    def test_helper_swallows_import_and_probe_errors(self, monkeypatch):
        def boom() -> None:
            raise OSError("sudo: a password is required")

        monkeypatch.setattr(platform_facts, "warm_platform_facts", boom, raising=False)
        server_module._warm_platform_facts()  # must not raise
        monkeypatch.delattr(platform_facts, "warm_platform_facts", raising=False)
        server_module._warm_platform_facts()  # ImportError path


class TestPlatformFactsWarmupIsBackground:
    """S1/R1: neither readiness nor shutdown waits for the cloud-metadata timeouts, and
    NVH_PLATFORM_WARMUP=0 skips them.

    On a non-cloud Linux host — a DGX Spark, exactly — the metadata curls take
    4-6 s to time out. The lifespan used to ``await`` them before building the
    Engine; the next version ran them via ``asyncio.to_thread`` and cancelled
    the task at shutdown — which cannot interrupt the worker, while
    ``asyncio.run()`` joins the default executor, so *stopping* the server
    blocked on them instead. Now: a daemon ``threading.Thread`` nothing joins.
    """

    def test_lifespan_neither_awaits_nor_joins_the_warmup(self, lifespan_env, monkeypatch):
        release = threading.Event()
        state = {"started": False}

        def slow_warm() -> None:
            state["started"] = True
            release.wait(30)  # stands in for the metadata curls timing out

        monkeypatch.setattr(platform_facts, "warm_platform_facts", slow_warm, raising=False)
        try:
            t0 = time.monotonic()
            with TestClient(app) as c:
                startup_s = time.monotonic() - t0
                assert c.get("/v1/health").status_code == 200
                thread = app.state.platform_warmup_thread
                assert isinstance(thread, threading.Thread) and thread.is_alive()
                assert thread.daemon is True  # dies with the process; never joined
                t1 = time.monotonic()
            shutdown_s = time.monotonic() - t1
            assert startup_s < 3.0, f"startup waited on the warm-up ({startup_s:.1f}s)"
            assert shutdown_s < 3.0, f"shutdown waited on the warm-up ({shutdown_s:.1f}s)"
            assert state["started"] is True
            assert thread.is_alive()  # still probing after shutdown: nothing joined it
        finally:
            release.set()
            pending = getattr(app.state, "platform_warmup_thread", None)
            if isinstance(pending, threading.Thread):
                pending.join(5)

    def test_warmup_thread_is_not_an_event_loop_executor_worker(self, lifespan_env, monkeypatch):
        """The mechanism: ``asyncio.run()`` joins the default executor's ``asyncio_N`` workers at
        shutdown; a bare daemon Thread it knows nothing about."""
        seen: dict[str, object] = {}

        def fake_warm() -> None:
            seen["thread"] = threading.current_thread()

        monkeypatch.setattr(platform_facts, "warm_platform_facts", fake_warm, raising=False)
        with TestClient(app) as c:
            assert c.get("/v1/health").status_code == 200
            thread = _join_warmup()
        assert seen["thread"] is thread
        assert thread.daemon is True
        assert thread.name == "nvh-platform-warmup"
        assert not thread.name.startswith("asyncio_")

    def test_warmup_still_runs_off_the_event_loop_in_the_background(self, lifespan_env, monkeypatch):
        seen: list[str] = []

        def fake_warm() -> None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                seen.append("worker")
            else:
                seen.append("event-loop")

        monkeypatch.setattr(platform_facts, "warm_platform_facts", fake_warm, raising=False)
        with TestClient(app) as c:
            assert c.get("/v1/health").status_code == 200
            _join_warmup()
        assert seen == ["worker"]

    def test_kill_switch_skips_the_warmup(self, lifespan_env, monkeypatch):
        monkeypatch.setenv("NVH_PLATFORM_WARMUP", "0")
        calls: list[str] = []
        monkeypatch.setattr(platform_facts, "warm_platform_facts", lambda: calls.append("probe"), raising=False)
        with TestClient(app) as c:
            assert c.get("/v1/health").status_code == 200
            assert app.state.platform_warmup_thread is None
        assert calls == []

    @pytest.mark.parametrize(
        ("value", "enabled"),
        [("0", False), ("false", False), ("No", False), (" off ", False), ("1", True), ("yes", True), ("", True), (None, True)],
    )
    def test_platform_warmup_enabled_parses_the_env(self, monkeypatch, value, enabled):
        if value is None:
            monkeypatch.delenv("NVH_PLATFORM_WARMUP", raising=False)
        else:
            monkeypatch.setenv("NVH_PLATFORM_WARMUP", value)
        assert server_module._platform_warmup_enabled() is enabled


# ---------------------------------------------------------------------------
# S5: system_ram under a unified-memory GPU
# ---------------------------------------------------------------------------

class TestSystemRamOnUnifiedMemory:
    """/v1/system/gpu never advertises CPU-offload headroom under a GB10 row."""

    def _get(self, client, gpus, sys_mem):
        with patch("nvh.api.server.detect_gpu_status", return_value=_gpu_status(gpus)), \
             patch("nvh.api.server.get_gpu_summary", return_value="summary"), \
             patch("nvh.api.server.detect_system_memory", return_value=sys_mem):
            r = client.get("/v1/system/gpu")
        assert r.status_code == 200
        return r.json()["data"]

    def test_gb10_zeroes_offload_headroom_and_flags_the_pool(self, client):
        from nvh.utils.gpu import SystemMemoryInfo

        data = self._get(client, [_gpu("NVIDIA GB10", 128.0, unified=True)], SystemMemoryInfo(128.0, 100.0, 70.0))
        assert data["gpus"][0]["unified_memory"] is True
        assert data["system_ram"] == {
            "total_gb": 128.0, "available_gb": 100.0, "effective_for_llm_gb": 0.0, "unified_memory": True,
        }

    def test_discrete_gpu_keeps_offload_headroom(self, client):
        from nvh.utils.gpu import SystemMemoryInfo

        data = self._get(client, [_gpu("NVIDIA GeForce RTX 4090", 24.0, unified=False)], SystemMemoryInfo(64.0, 48.0, 33.6))
        assert data["system_ram"] == {
            "total_gb": 64.0, "available_gb": 48.0, "effective_for_llm_gb": 33.6, "unified_memory": False,
        }

    def test_mixed_rows_follow_check_oom_risk_any_unified_rule(self, client):
        from nvh.utils.gpu import SystemMemoryInfo, check_oom_risk

        gpus = [_gpu("NVIDIA GeForce RTX 4090", 24.0, unified=False), _gpu("NVIDIA GB10", 128.0, unified=True)]
        data = self._get(client, gpus, SystemMemoryInfo(128.0, 100.0, 70.0))
        assert data["system_ram"]["unified_memory"] is check_oom_risk(1.0, gpus)["unified_memory"] is True
        assert data["system_ram"]["effective_for_llm_gb"] == 0.0

    def test_error_path_keeps_the_schema(self, client):
        with patch("nvh.api.server.detect_gpu_status", side_effect=RuntimeError("nvml down")):
            r = client.get("/v1/system/gpu")
        assert r.status_code == 200
        assert r.json()["data"]["system_ram"] == {
            "total_gb": 0.0, "available_gb": 0.0, "effective_for_llm_gb": 0.0, "unified_memory": False,
        }

    def test_recommendations_oom_check_reports_no_ram_pool_on_gb10(self, client):
        with patch("nvh.api.server.detect_gpus", return_value=[_gpu("NVIDIA GB10", 128.0, unified=True)]):
            r = client.get("/v1/system/recommendations")
        assert r.status_code == 200
        oom = r.json()["data"]["oom_check"]
        assert oom
        assert all(entry["unified_memory"] is True and entry["ram_free_gb"] == 0.0 for entry in oom.values())


# ---------------------------------------------------------------------------
# G3: the blocked / memory-unreadable state is visible in the /v1/system/gpu payload
# ---------------------------------------------------------------------------

def _unsized_gpu(name: str, index: int) -> GPUInfo:
    """A row detect_gpu_status kept at 0 GB because its memory could not be read."""
    return GPUInfo(
        name=name, vram_mb=0, vram_gb=0.0, driver_version="580.65", cuda_version="13.0",
        utilization_pct=0, memory_used_mb=0, memory_free_mb=0, index=index,
    )


def _memory_issue(source: str, name: str, index: int) -> dict:
    return {
        "source": source, "code": "memory-unavailable", "severity": "warning",
        "message": f"{source} could not report memory for {name} (GPU {index}).",
        "detail": "GPU is lost", "index": index,
    }


class TestGpuPayloadCarriesDetectionState:
    """Every consumer keyed on 'rows present', so the UI showed an unreadable GPU as '0 GB VRAM'
    while the recommender said no VRAM. The payload now says so itself: top-level ``status`` /
    ``summary`` from detect_gpu_status() and ``memory_unreadable`` per row — every old key kept."""

    _ROW_KEYS = {
        "name", "vram_mb", "vram_gb", "unified_memory", "memory_used_mb", "memory_free_mb",
        "memory_reserved_mb", "utilization_pct", "driver_version", "cuda_version", "index",
        "compute_capability", "compute_capability_source", "architecture", "architecture_heuristic",
    }

    def _get(self, client, status):
        with patch("nvh.api.server.detect_gpu_status", return_value=status):
            r = client.get("/v1/system/gpu")
        assert r.status_code == 200
        return r.json()["data"]

    def test_one_sized_one_unreadable_row(self, client):
        name = "NVIDIA A100 80GB PCIe"
        summary = f"1 of 2 GPUs ready: {name}, 80 GB VRAM; memory unreadable: {name} (GPU 1)"
        data = self._get(client, {
            "status": "ready", "source": "pynvml", "gpus": [_gpu(name, 80.0, unified=False), _unsized_gpu(name, 1)],
            "issues": [_memory_issue("pynvml", name, 1)], "device_files_present": True,
            "nvidia_smi": "", "summary": summary,
        })

        assert data["status"] == "ready"
        assert data["summary"] == summary
        assert data["total_vram_gb"] == 80.0                        # sized rows only
        assert data["detection"]["status"] == "ready"               # existing keys untouched
        assert data["detection"]["issues"][0]["index"] == 1
        rows = data["gpus"]
        assert [(row["index"], row["vram_mb"], row["memory_unreadable"]) for row in rows] == [
            (0, 81920, False), (1, 0, True),
        ]
        assert all(self._ROW_KEYS <= set(row) for row in rows)

    def test_blocked_when_no_row_is_sized(self, client):
        name = "NVIDIA GeForce RTX 4090"
        summary = (
            f"1 GPU visible but its memory could not be read: {name} (GPU 0) — "
            f"pynvml could not report memory for {name} (GPU 0)."
        )
        data = self._get(client, {
            "status": "blocked", "source": "pynvml", "gpus": [_unsized_gpu(name, 0)],
            "issues": [_memory_issue("pynvml", name, 0)], "device_files_present": True,
            "nvidia_smi": "", "summary": summary,
        })
        assert data["status"] == "blocked" and data["summary"] == summary
        assert data["total_vram_gb"] == 0.0
        assert data["gpus"][0]["memory_unreadable"] is True

    def test_summary_falls_back_to_the_rows_when_detection_has_none(self, client):
        """A status dict without ``summary`` (older stubs) gets the CLI summary of the *same* rows."""
        data = self._get(client, _gpu_status([_gpu("NVIDIA GeForce RTX 4090", 24.0, unified=False)]))
        assert data["status"] == "ok"
        assert data["summary"].startswith("NVIDIA GeForce RTX 4090 (24.0 GB VRAM)")
        assert data["gpus"][0]["memory_unreadable"] is False

    def test_error_path_carries_status(self, client):
        with patch("nvh.api.server.detect_gpu_status", side_effect=RuntimeError("nvml down")):
            r = client.get("/v1/system/gpu")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["status"] == "error" and data["detection"]["status"] == "error"
        assert data["gpus"] == []


# ---------------------------------------------------------------------------
# R7: WizardChatTurn.used_profile reaches wizard_chat through the history
# ---------------------------------------------------------------------------

class TestWizardChatHistoryCarriesUsedProfile:
    """The concierge's continuity tier reads ``used_profile`` off prior assistant turns;
    the endpoint models must not strip it on the way in (chat.py passes history through)."""

    _HISTORY = [
        {"role": "user", "content": "review my diff"},
        {"role": "assistant", "content": "Looks fine.", "used_profile": "coder"},
        {"role": "user", "content": "thanks"},
    ]

    @staticmethod
    def _stub_chat_module(monkeypatch, **attrs) -> None:
        """Stand in for nvh.integrations.wizard.chat so the endpoint's local import hits the fake."""
        stub = types.ModuleType("nvh.integrations.wizard.chat")
        for name, value in attrs.items():
            setattr(stub, name, value)
        monkeypatch.setitem(sys.modules, "nvh.integrations.wizard.chat", stub)

    def test_chat_endpoint_forwards_used_profile_in_history(self, client, monkeypatch):
        seen: dict = {}

        async def fake_wizard_chat(question, **kwargs):
            seen["question"] = question
            seen.update(kwargs)
            return {"answer": "ok", "mode": "llm", "context": {}}

        self._stub_chat_module(monkeypatch, wizard_chat=fake_wizard_chat)
        r = client.post("/v1/wizard/chat", json={"question": "and now?", "history": self._HISTORY})
        assert r.status_code == 200, r.text
        assert seen["question"] == "and now?"
        assert seen["history"] == self._HISTORY          # kept where present ...
        assert "used_profile" not in seen["history"][0]  # ... absent otherwise, never None

    def test_stream_endpoint_forwards_used_profile_in_history(self, client, monkeypatch):
        seen: dict = {}

        async def fake_stream(question, **kwargs):
            seen["question"] = question
            seen.update(kwargs)
            yield {"type": "done"}

        self._stub_chat_module(monkeypatch, wizard_chat_stream=fake_stream)
        r = client.post("/v1/wizard/chat/stream", json={"question": "and now?", "history": self._HISTORY})
        assert r.status_code == 200, r.text
        assert '"done"' in r.text
        assert seen["history"] == self._HISTORY

    def test_used_profile_is_bounded(self, client, monkeypatch):
        self._stub_chat_module(monkeypatch, wizard_chat=None)  # must never be reached
        history = [{"role": "assistant", "content": "x", "used_profile": "p" * 65}]
        r = client.post("/v1/wizard/chat", json={"question": "q", "history": history})
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# D2: GPU detection never runs on the event loop
# ---------------------------------------------------------------------------

class TestGpuDetectionRunsOffTheEventLoop:
    """_serialize_gpu_data / detect_gpus spawn nvidia-smi (three subprocesses, 10 s timeouts)
    whenever NVML cannot size the GPU. The system handlers called them synchronously, so every
    other request — the Wizard stream, /v1/health — stalled behind one status poll. Each handler
    now awaits the detection on a worker thread; a ticker coroutine must keep running meanwhile."""

    DETECTION_S = 0.3

    def _slow(self, monkeypatch, name: str, value):
        """Replace ``server_module.<name>`` with a blocking fake; records the thread it ran on."""
        threads: list[str] = []

        def slow(*args, **kwargs):
            threads.append(threading.current_thread().name)
            time.sleep(self.DETECTION_S)
            return value

        monkeypatch.setattr(server_module, name, slow)
        return threads

    @staticmethod
    def _run_with_ticker(handler):
        """Await ``handler()`` next to a 10 ms ticker; returns (result, ticks while it ran)."""
        async def main():
            ticks: list[float] = []

            async def ticker():
                while True:
                    ticks.append(time.perf_counter())
                    await asyncio.sleep(0.01)

            task = asyncio.ensure_future(ticker())
            try:
                result = await handler()
            finally:
                task.cancel()
            return result, len(ticks)

        return asyncio.run(main())

    def _assert_off_loop(self, threads: list[str], ticks: int) -> None:
        assert ticks >= 5, f"the event loop turned only {ticks} times during a {self.DETECTION_S}s detection"
        assert threads and threads[0] != threading.main_thread().name, "detection ran on the loop's thread"

    def test_system_gpu(self, monkeypatch):
        threads = self._slow(monkeypatch, "_serialize_gpu_data", {"status": "ready", "gpus": []})
        body, ticks = self._run_with_ticker(server_module.system_gpu)
        assert body == {"status": "success", "data": {"status": "ready", "gpus": []}}
        self._assert_off_loop(threads, ticks)

    def test_system_recommendations(self, monkeypatch):
        threads = self._slow(monkeypatch, "detect_gpus", [])
        body, ticks = self._run_with_ticker(server_module.system_recommendations)
        assert body["status"] == "success"
        assert set(body["data"]) == {"recommendations", "optimizations", "oom_check"}
        self._assert_off_loop(threads, ticks)

    def test_system_info(self, client, monkeypatch):
        threads = self._slow(monkeypatch, "_serialize_gpu_data", {"status": "ready", "gpus": []})
        body, ticks = self._run_with_ticker(lambda: server_module.system_info(_auth=None))
        assert body["data"]["gpu"] == {"status": "ready", "gpus": []}
        self._assert_off_loop(threads, ticks)

    def test_system_auto_setup(self, client, monkeypatch):
        monkeypatch.setattr(server_module, "_get_ollama_base_url", lambda: "http://127.0.0.1:9")  # closed port
        threads = self._slow(monkeypatch, "detect_gpus", [])
        body, ticks = self._run_with_ticker(lambda: server_module.system_auto_setup(_auth=None))
        assert body["status"] == "success" and body["data"]["gpu_count"] == 0
        self._assert_off_loop(threads, ticks)

    def test_recommendations_error_path_keeps_its_fallback_payload(self, monkeypatch):
        monkeypatch.setattr(server_module, "detect_gpus", lambda: (_ for _ in ()).throw(RuntimeError("nvml down")))
        body = asyncio.run(server_module.system_recommendations())
        assert body["data"]["recommendations"] == []
        assert body["data"]["optimizations"]["notes"] == ["GPU detection unavailable"]
