"""Batch 8 coverage — server SSE/webhooks/context/setup/conversations,
proxy streaming/Anthropic format, and all 20 litellm provider health_checks."""
from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import nvh.api.server as server_module
import nvh.storage.repository as repo
from nvh.api.proxy import anthropic_messages_to_nvhive, format_anthropic_response
from nvh.api.server import app
from nvh.config.settings import (
    BudgetConfig,
    CacheConfig,
    CouncilConfig,
    CouncilModeConfig,
    DefaultsConfig,
    ProviderConfig,
    RoutingConfig,
)
from nvh.core.engine import Engine
from nvh.providers.base import (
    CompletionResponse,
    FinishReason,
    HealthStatus,
    ModelInfo,
    StreamChunk,
    Usage,
)
from nvh.providers.registry import ProviderRegistry

# -- Shared helpers ----------------------------------------------------------

class _MP:
    """Mock provider."""
    def __init__(self, name="alpha"): self._name = name
    @property
    def name(self): return self._name
    async def complete(self, messages, model=None, temperature=1.0,
                       max_tokens=4096, system_prompt=None, **kw):
        return CompletionResponse(
            content=f"reply from {self._name}", model=model or "test-model",
            provider=self._name, usage=Usage(input_tokens=5, output_tokens=10, total_tokens=15),
            cost_usd=Decimal("0.0001"), latency_ms=20)
    async def stream(self, messages, model=None, temperature=1.0,
                     max_tokens=4096, system_prompt=None, **kw):
        async def _g() -> AsyncIterator[StreamChunk]:
            yield StreamChunk(delta="streamed", is_final=True, accumulated_content="streamed",
                              model=model or "test-model", provider=self._name,
                              usage=Usage(input_tokens=5, output_tokens=10, total_tokens=15),
                              cost_usd=Decimal("0.0001"), finish_reason=FinishReason.STOP)
        return _g()
    async def list_models(self):
        return [ModelInfo(model_id="test-model", provider=self._name)]
    async def health_check(self):
        return HealthStatus(provider=self._name, healthy=True, latency_ms=1)
    def estimate_tokens(self, text): return len(text) // 4

def _engine(tmp_path):
    cfg = CouncilConfig(
        defaults=DefaultsConfig(provider="alpha", model="test-model", temperature=0.7, max_tokens=128),
        providers={"alpha": ProviderConfig(enabled=True, default_model="test-model")},
        council=CouncilModeConfig(quorum=1, strategy="majority_vote", timeout=30,
                                  default_weights={"alpha": 1.0}, synthesis_provider="alpha"),
        routing=RoutingConfig(), budget=BudgetConfig(),
        cache=CacheConfig(enabled=True, ttl_seconds=3600, max_size=100))
    reg = ProviderRegistry(); reg.register("alpha", _MP("alpha"))
    e = Engine(config=cfg, registry=reg); e._initialized = True; e._context_files = []
    return e

@pytest.fixture()
def test_client(tmp_path):
    repo._engine = repo._session_factory = None
    asyncio.run(repo.init_db(db_path=tmp_path / "b8.db"))
    eng = _engine(tmp_path); orig = server_module._engine; server_module._engine = eng
    yield TestClient(app, raise_server_exceptions=False)
    server_module._engine = orig; repo._engine = repo._session_factory = None

# -- Target 1: server.py endpoints ------------------------------------------

class TestServerSSE:
    def test_query_stream_sse(self, test_client):
        r = test_client.post("/v1/query", json={"prompt": "Hi", "provider": "alpha", "stream": True})
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("content-type", "")
        assert "data:" in r.text or "event:" in r.text

    def test_query_stream_bad_provider(self, test_client):
        r = test_client.post("/v1/query", json={"prompt": "Hi", "provider": "nope", "stream": True})
        assert r.status_code == 200 and "error" in r.text

class TestWebhooks:
    def test_bad_scheme(self, test_client):
        r = test_client.post("/v1/webhooks/test", json={"url": "ftp://x.com/h", "secret": ""})
        assert r.status_code == 400

    def test_private_ip(self, test_client):
        r = test_client.post("/v1/webhooks/test", json={"url": "http://169.254.169.254/x", "secret": ""})
        assert r.status_code == 400

    def test_dispatch_success(self, test_client):
        with patch("nvh.core.webhooks.WebhookManager._dispatch", new_callable=AsyncMock, return_value=True):
            r = test_client.post("/v1/webhooks/test", json={"url": "https://example.com/h", "secret": "s"})
        assert r.status_code == 200
        assert r.json()["data"]["delivered"] is True

class TestContext:
    def test_get(self, test_client):
        r = test_client.get("/v1/context")
        assert r.status_code == 200 and r.json()["data"]["total"] == 0

    def test_reload(self, test_client):
        with patch("nvh.core.context_files.find_context_files", return_value=[]):
            r = test_client.post("/v1/context/reload")
        assert r.status_code == 200 and r.json()["data"]["files_loaded"] == 0

class TestSetup:
    def test_status(self, test_client):
        r = test_client.get("/v1/setup/status")
        assert r.status_code == 200
        d = r.json()["data"]
        assert "ready" in d and isinstance(d["enabled_names"], list)

    def test_save_key_bad_provider(self, test_client):
        r = test_client.post("/v1/setup/save-key", json={"provider": "bogus", "api_key": "sk-1234567890"})
        assert r.status_code == 422

    def test_save_key_keyring_error(self, test_client):
        mk = MagicMock(); mk.set_password.side_effect = Exception("no backend")
        with patch.dict("sys.modules", {"keyring": mk}):
            r = test_client.post("/v1/setup/save-key", json={"provider": "groq", "api_key": "gsk_1234567890t"})
        assert r.status_code == 200 and r.json()["status"] == "error"

class TestConversations:
    def test_delete_not_found(self, test_client):
        assert test_client.delete("/v1/conversations/missing-id").status_code == 404

    def test_get_not_found(self, test_client):
        assert test_client.get("/v1/conversations/missing-id").status_code == 404

    def test_list_empty(self, test_client):
        r = test_client.get("/v1/conversations")
        assert r.status_code == 200 and isinstance(r.json()["data"], list)

    def test_query_missing_conv(self, test_client):
        r = test_client.post("/v1/conversations/no-conv/query", json={"prompt": "Hi"})
        assert r.status_code == 500

class TestAutoSetup:
    def test_no_gpus(self, test_client):
        with patch("nvh.api.server.detect_gpus", return_value=[]), \
             patch("nvh.api.server.recommend_models", return_value=[]), \
             patch("nvh.api.server.get_ollama_optimizations") as mo:
            mo.return_value = MagicMock(flash_attention=False, num_parallel=1,
                                        recommended_ctx=2048, recommended_quant="q4_0",
                                        architecture="cpu", notes="")
            r = test_client.post("/v1/system/auto-setup")
        assert r.status_code == 200 and r.json()["data"]["gpu_count"] == 0

# -- Target 2: proxy.py uncovered paths -------------------------------------

class TestAnthropicFormat:
    def test_single_user(self):
        p, s = anthropic_messages_to_nvhive([{"role": "user", "content": "hi"}])
        assert p == "hi" and s is None

    def test_with_system(self):
        p, s = anthropic_messages_to_nvhive([{"role": "user", "content": "hi"}], system="be nice")
        assert s == "be nice"

    def test_structured_content(self):
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "desc"}, {"type": "tool_result", "content": "out"}]}]
        p, _ = anthropic_messages_to_nvhive(msgs)
        assert "desc" in p and "out" in p

    def test_string_blocks(self):
        p, _ = anthropic_messages_to_nvhive([{"role": "user", "content": ["raw"]}])
        assert "raw" in p

    def test_format_response(self):
        r = format_anthropic_response("hi", "claude-3", "anthropic", input_tokens=10, output_tokens=5)
        assert r["type"] == "message" and r["content"][0]["text"] == "hi"
        assert r["id"].startswith("msg_") and r["usage"]["input_tokens"] == 10

class TestProxyStreaming:
    def test_openai_stream(self, test_client):
        r = test_client.post("/v1/proxy/chat/completions", json={
            "model": "auto", "messages": [{"role": "user", "content": "s"}], "stream": True})
        assert r.status_code == 200 and "[DONE]" in r.text

    def test_anthropic_non_stream(self, test_client):
        r = test_client.post("/v1/anthropic/messages", json={
            "model": "auto", "messages": [{"role": "user", "content": "h"}], "max_tokens": 100})
        assert r.status_code == 200 and r.json()["type"] == "message"

    def test_anthropic_stream(self, test_client):
        r = test_client.post("/v1/anthropic/messages", json={
            "model": "auto", "messages": [{"role": "user", "content": "h"}],
            "max_tokens": 100, "stream": True})
        assert r.status_code == 200 and "event: message_start" in r.text

    def test_openai_stream_with_system(self, test_client):
        r = test_client.post("/v1/proxy/chat/completions", json={
            "model": "auto", "stream": True,
            "messages": [{"role": "system", "content": "poet"}, {"role": "user", "content": "go"}]})
        assert r.status_code == 200 and "data:" in r.text

    def test_anthropic_empty_rejected(self, test_client):
        r = test_client.post("/v1/anthropic/messages", json={
            "model": "auto", "messages": [], "max_tokens": 100})
        assert r.status_code == 400

# -- Target 3: parametrized litellm provider health_check --------------------

_PROVIDERS = [
    ("nvh.providers.ai21_provider", "AI21Provider"),
    ("nvh.providers.anthropic_provider", "AnthropicProvider"),
    ("nvh.providers.cerebras_provider", "CerebrasProvider"),
    ("nvh.providers.cohere_provider", "CohereProvider"),
    ("nvh.providers.deepseek_provider", "DeepSeekProvider"),
    ("nvh.providers.fireworks_provider", "FireworksProvider"),
    ("nvh.providers.github_provider", "GitHubProvider"),
    ("nvh.providers.google_provider", "GoogleProvider"),
    ("nvh.providers.grok_provider", "GrokProvider"),
    ("nvh.providers.groq_provider", "GroqProvider"),
    ("nvh.providers.huggingface_provider", "HuggingFaceProvider"),
    ("nvh.providers.llm7_provider", "LLM7Provider"),
    ("nvh.providers.mistral_provider", "MistralProvider"),
    ("nvh.providers.nvidia_provider", "NvidiaProvider"),
    ("nvh.providers.openai_provider", "OpenAIProvider"),
    ("nvh.providers.openrouter_provider", "OpenRouterProvider"),
    ("nvh.providers.perplexity_provider", "PerplexityProvider"),
    ("nvh.providers.sambanova_provider", "SambaNovProvider"),
    ("nvh.providers.siliconflow_provider", "SiliconFlowProvider"),
    ("nvh.providers.together_provider", "TogetherProvider"),
]

def _litellm_resp():
    c = MagicMock(); c.message.content = "pong"; c.finish_reason = "stop"
    u = MagicMock(); u.prompt_tokens = 1; u.completion_tokens = 1; u.total_tokens = 2
    r = MagicMock(); r.choices = [c]; r.usage = u; r.model = "test"
    return r

@pytest.mark.asyncio
@pytest.mark.parametrize("mod_path,cls_name", _PROVIDERS, ids=[p[1] for p in _PROVIDERS])
async def test_provider_health_check_success(mod_path, cls_name):
    """All 20 litellm providers return healthy=True when acompletion succeeds."""
    provider = getattr(importlib.import_module(mod_path), cls_name)()
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=_litellm_resp()):
        result = await provider.health_check()
    assert isinstance(result, HealthStatus) and result.healthy is True
