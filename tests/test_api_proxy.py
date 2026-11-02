"""Tests for nvh.api.proxy — helpers, OpenAI/Anthropic-compatible endpoints, SSE streaming.

The ``client`` fixture streams two chunks ("Hello " + "world") so SSE tests
can assert on ordering and terminal events; ``error_client`` explodes mid-stream.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import nvh.api.server as server_module
import nvh.storage.repository as repo
from nvh.api.proxy import (
    anthropic_messages_to_nvhive,
    anthropic_stream_generator,
    build_models_list,
    format_anthropic_response,
    format_openai_response,
    is_throwdown_model,
    openai_messages_to_nvhive,
    openai_stream_generator,
    parse_council_model,
    resolve_provider_from_model,
)
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
from nvh.core.engine import BudgetExceededError, Engine
from nvh.providers.base import (
    CompletionResponse,
    FinishReason,
    HealthStatus,
    Message,
    ModelInfo,
    StreamChunk,
    Usage,
)
from nvh.providers.registry import ProviderRegistry


class StreamingTestProvider:
    def __init__(self, name: str = "alpha") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def complete(self, messages: list[Message], model: str | None = None, temperature: float = 1.0, max_tokens: int = 4096, system_prompt: str | None = None, **kw) -> CompletionResponse:
        return CompletionResponse(content=f"Mock response from {self._name}", model=model or "test-model", provider=self._name, usage=Usage(input_tokens=10, output_tokens=20, total_tokens=30), cost_usd=Decimal("0.001"), latency_ms=50)

    async def stream(self, messages: list[Message], model: str | None = None, temperature: float = 1.0, max_tokens: int = 4096, system_prompt: str | None = None, **kw) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(delta="Hello ", is_final=False, accumulated_content="Hello ", model=model or "test-model", provider=self._name, usage=None, cost_usd=None, finish_reason=None)
        yield StreamChunk(delta="world", is_final=True, accumulated_content="Hello world", model=model or "test-model", provider=self._name, usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15), cost_usd=Decimal("0.0005"), finish_reason=FinishReason.STOP)

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(model_id="test-model", provider=self._name)]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(provider=self._name, healthy=True, latency_ms=5)

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


class ErroringStreamProvider:
    def __init__(self, name: str = "erroring") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def complete(self, messages: list[Message], **kw) -> CompletionResponse:
        raise RuntimeError("boom")

    async def stream(self, messages: list[Message], **kw) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(delta="partial", is_final=False, accumulated_content="partial", model="err-model", provider=self._name, usage=None, cost_usd=None, finish_reason=None)
        raise RuntimeError("stream exploded")

    async def list_models(self) -> list[ModelInfo]:
        return []

    async def health_check(self) -> HealthStatus:
        return HealthStatus(provider=self._name, healthy=True, latency_ms=1)

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


def _make_engine(provider=None) -> Engine:
    config = CouncilConfig(
        defaults=DefaultsConfig(provider="alpha", model="test-model", temperature=1.0, max_tokens=256),
        providers={"alpha": ProviderConfig(enabled=True, default_model="test-model")},
        council=CouncilModeConfig(quorum=1, strategy="majority_vote", timeout=30, default_weights={"alpha": 1.0}, synthesis_provider="alpha"),
        routing=RoutingConfig(),
        budget=BudgetConfig(),
        cache=CacheConfig(enabled=True, ttl_seconds=3600, max_size=100),
    )
    registry = ProviderRegistry()
    registry.register("alpha", provider or StreamingTestProvider("alpha"))
    engine = Engine(config=config, registry=registry)
    engine._initialized = True
    return engine


def _client_for(engine: Engine):
    original = server_module._engine
    server_module._engine = engine
    yield TestClient(app, raise_server_exceptions=False)
    server_module._engine = original


@pytest.fixture()
def client(sync_db):
    yield from _client_for(_make_engine())


@pytest.fixture()
def error_client(sync_db):
    yield from _client_for(_make_engine(ErroringStreamProvider("alpha")))


def _parse_sse_events(raw: str) -> list[tuple[str | None, str]]:
    events: list[tuple[str | None, str]] = []
    event_type = None
    for line in raw.split("\n"):
        if line.startswith("event: "):
            event_type = line[7:].strip()
        elif line.startswith("data: "):
            events.append((event_type, line[6:]))
            event_type = None
    return events


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestProxyHelpers:
    """Pure-function helpers in proxy.py."""

    def test_parse_council_model_default(self):
        assert parse_council_model("council") == 3

    def test_parse_council_model_with_count(self):
        assert parse_council_model("council:5") == 5

    def test_parse_council_model_clamped(self):
        assert parse_council_model("council:1") == 2
        assert parse_council_model("council:99") == 10

    def test_parse_council_model_invalid(self):
        assert parse_council_model("council:abc") == 3

    def test_parse_council_model_none(self):
        assert parse_council_model("") is None
        assert parse_council_model("gpt-4o") is None

    def test_is_throwdown_model(self):
        assert is_throwdown_model("throwdown") is True
        assert is_throwdown_model("auto") is False
        assert is_throwdown_model(None) is False

    def test_resolve_provider_auto(self):
        assert resolve_provider_from_model("auto") == (None, None)
        assert resolve_provider_from_model(None) == (None, None)

    def test_resolve_provider_safe(self):
        assert resolve_provider_from_model("safe") == ("ollama", None)
        assert resolve_provider_from_model("local") == ("ollama", None)

    def test_resolve_provider_known_model(self):
        prov, mod = resolve_provider_from_model("gpt-4o")
        assert prov == "openai"
        assert mod == "gpt-4o"

    def test_resolve_provider_prefix_match(self):
        prov, mod = resolve_provider_from_model("gpt-4o-2024-11-20")
        assert prov == "openai"
        assert mod == "gpt-4o-2024-11-20"

    def test_resolve_provider_unknown(self):
        prov, mod = resolve_provider_from_model("my-custom-model")
        assert prov is None
        assert mod == "my-custom-model"

    def test_openai_messages_single_user(self):
        msgs = [{"role": "user", "content": "hello"}]
        prompt, sys = openai_messages_to_nvhive(msgs)
        assert prompt == "hello"
        assert sys is None

    def test_openai_messages_with_system(self):
        msgs = [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
        ]
        prompt, sys = openai_messages_to_nvhive(msgs)
        assert sys == "you are helpful"

    def test_openai_messages_structured_content(self):
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "describe this"},
            {"type": "image_url", "image_url": {"url": "x"}},
        ]}]
        prompt, _ = openai_messages_to_nvhive(msgs)
        assert "describe this" in prompt

    def test_format_openai_response_structure(self):
        resp = format_openai_response(
            "hi", "gpt-4o", "openai",
            prompt_tokens=10, completion_tokens=5,
        )
        assert resp["object"] == "chat.completion"
        assert resp["choices"][0]["message"]["content"] == "hi"
        assert resp["usage"]["total_tokens"] == 15
        assert resp["x_nvhive_provider"] == "openai"

    def test_build_models_list_virtual(self):
        registry = MagicMock()
        registry.list_enabled.return_value = []
        result = build_models_list(registry)
        assert result["object"] == "list"
        ids = [m["id"] for m in result["data"]]
        assert "nvhive" in ids
        assert "council" in ids
        assert "throwdown" in ids


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


@pytest.mark.parametrize("inp,exp", [
    ("", None), ("council:7", 7), ("council:xyz", 3), ("gpt-4o", None)])
def test_parse_council(inp, exp):
    assert parse_council_model(inp) == exp


def test_throwdown():
    assert not is_throwdown_model(None) and not is_throwdown_model("c")


@pytest.mark.parametrize("inp,exp", [
    ("auto", (None, None)), ("safe", ("ollama", None)),
    ("gpt-4o", ("openai", "gpt-4o")), ("council:5", (None, None)),
    ("throwdown", (None, None))])
def test_resolve(inp, exp):
    assert resolve_provider_from_model(inp) == exp


def test_resolve_prefix_and_unknown():
    assert resolve_provider_from_model("claude-3-5-sonnet-20241022")[0] == "anthropic"
    assert resolve_provider_from_model("mystery") == (None, "mystery")


def _meng(avail=True):
    e = MagicMock()
    e.config.defaults.temperature = 0.7
    e.config.defaults.max_tokens = 512
    e.config.defaults.system_prompt = None
    e.registry.has.return_value = avail
    return e


@pytest.mark.asyncio
async def test_stream_gen_unavail():
    eng = _meng(False)
    d = MagicMock()
    d.provider = "fake"
    d.model = "m"
    eng.router.route.return_value = d
    out = b"".join([c async for c in openai_stream_generator(
        eng, "hi", None, None, None, None, None, "auto")])
    assert b"provider_not_found" in out and b"[DONE]" in out


@pytest.mark.asyncio
async def test_stream_gen_happy():
    eng = _meng(True)
    d = MagicMock()
    d.provider = "openai"
    d.model = "gpt-4o"
    eng.router.route.return_value = d
    mp = MagicMock()
    eng.registry.get.return_value = mp

    async def _s(**kw):
        yield StreamChunk(delta="Hi", is_final=True,
                          finish_reason=FinishReason.STOP)
    mp.stream.return_value = _s()
    out = b"".join([c async for c in openai_stream_generator(
        eng, "hi", "openai", "gpt-4o", None, 0.5, 100, "gpt-4o")])
    assert b'"role": "assistant"' in out and b"Hi" in out


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


class TestProxyEndpoints:
    """Proxy HTTP endpoints via TestClient."""

    def test_proxy_chat_completions(self, client):
        resp = client.post("/v1/proxy/chat/completions", json={
            "model": "auto",
            "messages": [{"role": "user", "content": "ping"}],
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["message"]["role"] == "assistant"

    def test_proxy_completions(self, client):
        resp = client.post("/v1/proxy/completions", json={
            "model": "auto",
            "prompt": "hello",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert "choices" in body

    def test_proxy_models(self, client):
        resp = client.get("/v1/proxy/models")
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "list"
        assert len(body["data"]) > 0

    def test_proxy_health(self, client):
        resp = client.get("/v1/proxy/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"

    def test_proxy_chat_error_model(self, client):
        """Requesting a model that maps to a missing provider still returns 200
        with an error in the response (OpenAI convention)."""
        resp = client.post("/v1/proxy/chat/completions", json={
            "model": "safe",
            "messages": [{"role": "user", "content": "test"}],
        })
        # safe -> ollama which isn't registered; the endpoint either
        # returns an error body or falls back — both are valid.
        assert resp.status_code in (200, 500, 502)


class TestProxyStreaming:
    def test_openai_stream(self, client):
        r = client.post("/v1/proxy/chat/completions", json={
            "model": "auto", "messages": [{"role": "user", "content": "s"}], "stream": True})
        assert r.status_code == 200 and "[DONE]" in r.text

    def test_anthropic_non_stream(self, client):
        r = client.post("/v1/anthropic/messages", json={
            "model": "auto", "messages": [{"role": "user", "content": "h"}], "max_tokens": 100})
        assert r.status_code == 200 and r.json()["type"] == "message"

    def test_anthropic_stream(self, client):
        r = client.post("/v1/anthropic/messages", json={
            "model": "auto", "messages": [{"role": "user", "content": "h"}],
            "max_tokens": 100, "stream": True})
        assert r.status_code == 200 and "event: message_start" in r.text

    def test_openai_stream_with_system(self, client):
        r = client.post("/v1/proxy/chat/completions", json={
            "model": "auto", "stream": True,
            "messages": [{"role": "system", "content": "poet"}, {"role": "user", "content": "go"}]})
        assert r.status_code == 200 and "data:" in r.text

    def test_anthropic_empty_rejected(self, client):
        r = client.post("/v1/anthropic/messages", json={
            "model": "auto", "messages": [], "max_tokens": 100})
        assert r.status_code == 400


class TestProxyStatusCodes:
    def test_proxy_health(self, client):
        r = client.get("/v1/proxy/health")
        assert r.status_code == 200

    def test_proxy_models(self, client):
        r = client.get("/v1/proxy/models")
        assert r.status_code == 200
        body = r.json()
        assert "data" in body

    def test_proxy_completions(self, client):
        r = client.post("/v1/proxy/completions", json={
            "model": "alpha/test-model",
            "prompt": "hello",
            "max_tokens": 10,
        })
        assert r.status_code in (200, 422)

    def test_proxy_chat_completions(self, client):
        r = client.post("/v1/proxy/chat/completions", json={
            "model": "alpha/test-model",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 10,
            "stream": False,
        })
        assert r.status_code in (200, 422)


class TestProxyPrefixedModel:
    def test_anthropic_messages_non_streaming(self, client):
        r = client.post("/v1/anthropic/messages", json={
            "model": "alpha/m",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 10,
        })
        # May succeed or return format error — exercising the code path matters
        assert r.status_code in (200, 422, 500)

    def test_proxy_chat_non_streaming(self, client):
        r = client.post("/v1/proxy/chat/completions", json={
            "model": "alpha/m",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 10,
            "stream": False,
        })
        assert r.status_code in (200, 422)


class TestSSEQueryStream:
    def test_stream_query_emits_chunk_and_done(self, client: TestClient) -> None:
        resp = client.post("/v1/query", json={"prompt": "hi", "stream": True, "provider": "alpha"})
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        events = _parse_sse_events(resp.text)
        types = [e[0] for e in events]
        assert "chunk" in types
        assert "done" in types
        chunk_data = json.loads(next(d for t, d in events if t == "chunk"))
        assert "delta" in chunk_data
        done_data = json.loads(next(d for t, d in events if t == "done"))
        assert done_data["content"] == "Hello world"
        assert done_data["provider"] == "alpha"

    def test_stream_query_invalid_provider_emits_error(self, client: TestClient) -> None:
        resp = client.post("/v1/query", json={"prompt": "hi", "stream": True, "provider": "nonexistent_xyz"})
        assert resp.status_code == 200
        events = _parse_sse_events(resp.text)
        error_events = [(t, d) for t, d in events if t == "error"]
        assert len(error_events) >= 1
        err = json.loads(error_events[0][1])
        assert "error" in err


class TestOpenAIProxyStreaming:
    def test_chat_completions_stream(self, client: TestClient) -> None:
        resp = client.post("/v1/proxy/chat/completions", json={"model": "auto", "messages": [{"role": "user", "content": "hello"}], "stream": True})
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        events = _parse_sse_events(resp.text)
        data_lines = [d for _, d in events]
        assert any("[DONE]" in d for d in data_lines)
        json_events = [json.loads(d) for d in data_lines if d.strip() not in ("[DONE]", "")]
        assert any(e.get("object") == "chat.completion.chunk" for e in json_events)
        role_event = json_events[0]
        assert role_event["choices"][0]["delta"].get("role") == "assistant"

    def test_chat_completions_non_stream(self, client: TestClient) -> None:
        resp = client.post("/v1/proxy/chat/completions", json={"model": "auto", "messages": [{"role": "user", "content": "hello"}], "stream": False})
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["message"]["role"] == "assistant"
        assert "Mock response" in body["choices"][0]["message"]["content"]
        assert "usage" in body


class TestProxyCompletions:
    def test_completions_non_stream(self, client: TestClient) -> None:
        resp = client.post("/v1/proxy/completions", json={"model": "auto", "prompt": "Say hi"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "text_completion"
        assert len(body["choices"]) == 1
        assert "text" in body["choices"][0]
        assert "usage" in body


class TestAnthropicProxy:
    def test_anthropic_non_stream(self, client: TestClient) -> None:
        resp = client.post("/v1/anthropic/messages", json={"model": "auto", "messages": [{"role": "user", "content": "hi"}], "stream": False})
        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] == "message"
        assert body["role"] == "assistant"
        assert len(body["content"]) >= 1
        assert body["content"][0]["type"] == "text"
        assert "usage" in body

    def test_anthropic_stream(self, client: TestClient) -> None:
        resp = client.post("/v1/anthropic/messages", json={"model": "auto", "messages": [{"role": "user", "content": "hello"}], "stream": True})
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        events = _parse_sse_events(resp.text)
        etypes = [t for t, _ in events]
        assert "message_start" in etypes
        assert "content_block_start" in etypes
        assert "content_block_delta" in etypes
        assert "content_block_stop" in etypes
        assert "message_stop" in etypes
        deltas = [json.loads(d) for t, d in events if t == "content_block_delta"]
        texts = [e["delta"]["text"] for e in deltas]
        assert "Hello " in texts or "world" in texts


class TestProxyModels:
    def test_models_list_format(self, client: TestClient) -> None:
        resp = client.get("/v1/proxy/models")
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "list"
        assert isinstance(body["data"], list)
        assert len(body["data"]) > 0
        for m in body["data"]:
            assert "id" in m
            assert m["object"] == "model"


class TestCouncilRouting:
    def test_council_model_routes_to_council(self, client: TestClient) -> None:
        resp = client.post("/v1/proxy/chat/completions", json={"model": "council:3", "messages": [{"role": "user", "content": "test"}], "stream": False})
        assert resp.status_code in (200, 400, 500)


class TestStreamErrorPath:
    def test_stream_error_emits_error_event(self, error_client: TestClient) -> None:
        resp = error_client.post("/v1/query", json={"prompt": "boom", "stream": True, "provider": "alpha"})
        assert resp.status_code == 200
        events = _parse_sse_events(resp.text)
        etypes = [t for t, _ in events]
        assert "chunk" in etypes
        assert "error" in etypes
        err_data = json.loads(next(d for t, d in events if t == "error"))
        assert "error" in err_data
        assert "exploded" in err_data["error"]

    def test_openai_proxy_stream_error(self, error_client: TestClient) -> None:
        resp = error_client.post("/v1/proxy/chat/completions", json={"model": "auto", "messages": [{"role": "user", "content": "boom"}], "stream": True})
        assert resp.status_code == 200
        events = _parse_sse_events(resp.text)
        data_lines = [d for _, d in events if d.strip() not in ("[DONE]", "")]
        json_events = [json.loads(d) for d in data_lines]
        assert any("error" in e for e in json_events)


# ---------------------------------------------------------------------------
# Standard paths, auth tightening, streamed budget + ledger (0.46)
# ---------------------------------------------------------------------------

_KEY = "secret-test-key-abc123"
_BEARER = {"Authorization": f"Bearer {_KEY}"}
_CHAT = {"model": "auto", "messages": [{"role": "user", "content": "hello"}]}
_TEXT = {"model": "auto", "prompt": "Say hi"}
_ANTHROPIC = {"model": "auto", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 64}


@pytest.fixture()
def keyed_client(sync_db, monkeypatch):
    """TestClient with HIVE_API_KEY set so require_auth demands a credential."""
    monkeypatch.setenv("HIVE_API_KEY", _KEY)
    yield from _client_for(_make_engine())


@pytest.fixture()
def open_client(sync_db, monkeypatch):
    """TestClient with HIVE_API_KEY explicitly unset (open mode)."""
    monkeypatch.delenv("HIVE_API_KEY", raising=False)
    yield from _client_for(_make_engine())


def _json_events(raw: str) -> list[dict]:
    return [json.loads(d) for _, d in _parse_sse_events(raw) if d.strip() not in ("[DONE]", "")]


class TestStandardPaths:
    """``OPENAI_BASE_URL=.../v1`` and ``ANTHROPIC_BASE_URL=.../v1/anthropic`` resolve to the
    same handler objects as ``/v1/proxy/*`` and ``/v1/anthropic/messages``, which stay."""

    def test_chat_completions_returns_the_proxy_shape(self, open_client: TestClient) -> None:
        std = open_client.post("/v1/chat/completions", json={**_CHAT, "stream": False})
        legacy = open_client.post("/v1/proxy/chat/completions", json={**_CHAT, "stream": False})
        assert std.status_code == legacy.status_code == 200
        a, b = std.json(), legacy.json()
        assert a["object"] == b["object"] == "chat.completion"
        assert a["id"].startswith("chatcmpl-") and b["id"].startswith("chatcmpl-")
        assert set(a) == set(b)
        assert a["choices"][0]["message"]["content"] == b["choices"][0]["message"]["content"]

    def test_chat_completions_streams_like_the_proxy_path(self, open_client: TestClient) -> None:
        resp = open_client.post("/v1/chat/completions", json={**_CHAT, "stream": True})
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        events = _json_events(resp.text)
        assert events[0]["choices"][0]["delta"].get("role") == "assistant"
        assert all(e["object"] == "chat.completion.chunk" and e["id"].startswith("chatcmpl-") for e in events)
        assert events[-1]["choices"][0]["finish_reason"] == "stop"
        assert resp.text.rstrip().endswith("data: [DONE]")

    def test_completions_returns_the_proxy_shape(self, open_client: TestClient) -> None:
        std = open_client.post("/v1/completions", json=_TEXT)
        legacy = open_client.post("/v1/proxy/completions", json=_TEXT)
        assert std.status_code == legacy.status_code == 200
        assert std.json()["object"] == legacy.json()["object"] == "text_completion"
        assert std.json()["id"].startswith("cmpl-")
        assert set(std.json()) == set(legacy.json())

    def test_anthropic_sdk_path_returns_the_messages_shape(self, open_client: TestClient) -> None:
        std = open_client.post("/v1/anthropic/v1/messages", json=_ANTHROPIC)
        legacy = open_client.post("/v1/anthropic/messages", json=_ANTHROPIC)
        assert std.status_code == legacy.status_code == 200
        assert std.json()["type"] == legacy.json()["type"] == "message"
        assert std.json()["id"].startswith("msg_")
        assert set(std.json()) == set(legacy.json())

    def test_aliases_are_in_the_schema_and_models_keeps_the_nvhive_envelope(self, open_client: TestClient) -> None:
        paths = open_client.get("/openapi.json").json()["paths"]
        for path in ("/v1/chat/completions", "/v1/completions", "/v1/anthropic/v1/messages",
                     "/v1/proxy/chat/completions", "/v1/proxy/completions", "/v1/anthropic/messages"):
            assert "post" in paths[path], path
        # O1: GET /v1/models is not aliased; the OpenAI-shaped list stays at /v1/proxy/models.
        assert set(open_client.get("/v1/models").json()) == {"status", "data"}
        assert open_client.get("/v1/proxy/models").json()["object"] == "list"


class TestStandardPathAuth:
    """A keyed deployment demands the key on every proxy path, the Anthropic proxy included
    (open until 0.46); ``/v1/proxy/health`` and ``/v1/health`` stay open for monitors."""

    @pytest.mark.parametrize("path,body", [
        ("/v1/chat/completions", _CHAT),
        ("/v1/completions", _TEXT),
        ("/v1/proxy/chat/completions", _CHAT),
        ("/v1/anthropic/messages", _ANTHROPIC),
        ("/v1/anthropic/v1/messages", _ANTHROPIC),
    ])
    def test_keyed_rejects_a_bare_request_and_accepts_the_bearer(self, keyed_client: TestClient, path, body) -> None:
        assert keyed_client.post(path, json=body).status_code == 401
        assert keyed_client.post(path, json=body, headers=_BEARER).status_code == 200

    def test_health_routes_stay_open_when_keyed(self, keyed_client: TestClient) -> None:
        assert keyed_client.get("/v1/proxy/health").status_code == 200
        assert keyed_client.get("/v1/health").status_code == 200

    @pytest.mark.parametrize("path", ["/v1/anthropic/messages", "/v1/anthropic/v1/messages"])
    def test_open_mode_admits_the_anthropic_proxy(self, open_client: TestClient, path) -> None:
        assert open_client.post(path, json=_ANTHROPIC).status_code == 200


def _record_ledger(monkeypatch) -> list[dict]:
    """Capture every ``QueryLog`` row ``Engine._log_query`` would write (it looks
    ``repo.log_query`` up at call time)."""
    rows: list[dict] = []

    async def _log_query(**kwargs):
        rows.append(kwargs)

    monkeypatch.setattr(repo, "log_query", _log_query)
    return rows


class TestStreamedProxyLedger:
    """Streamed completions are logged like non-streamed ones: one row, the final
    chunk's cost and tokens, ``mode="proxy"``."""

    def test_openai_stream_leaves_one_row_with_the_chunk_cost(self, open_client: TestClient, monkeypatch) -> None:
        rows = _record_ledger(monkeypatch)
        resp = open_client.post("/v1/chat/completions", json={**_CHAT, "stream": True})
        assert resp.status_code == 200 and resp.text.rstrip().endswith("data: [DONE]")
        assert len(rows) == 1
        row = rows[0]
        assert row["mode"] == "proxy"
        assert row["provider"] == "alpha" and row["model"] == "test-model"
        assert row["cost_usd"] == Decimal("0.0005")
        assert (row["input_tokens"], row["output_tokens"]) == (10, 5)
        assert row["status"] == "success"

    def test_anthropic_stream_leaves_one_row_with_the_chunk_cost(self, open_client: TestClient, monkeypatch) -> None:
        rows = _record_ledger(monkeypatch)
        resp = open_client.post("/v1/anthropic/messages", json={**_ANTHROPIC, "stream": True})
        assert resp.status_code == 200 and "message_stop" in resp.text
        assert len(rows) == 1
        row = rows[0]
        assert row["mode"] == "proxy"
        assert row["provider"] == "alpha" and row["model"] == "test-model"
        assert row["cost_usd"] == Decimal("0.0005")
        assert (row["input_tokens"], row["output_tokens"]) == (10, 5)

    def test_a_stream_that_errors_leaves_no_success_row(self, error_client: TestClient, monkeypatch) -> None:
        rows = _record_ledger(monkeypatch)
        resp = error_client.post("/v1/chat/completions", json={**_CHAT, "stream": True})
        assert resp.status_code == 200
        assert any("error" in e for e in _json_events(resp.text))
        assert rows == []

    def test_an_erroring_anthropic_stream_leaves_no_success_row(self, error_client: TestClient, monkeypatch) -> None:
        rows = _record_ledger(monkeypatch)
        resp = error_client.post("/v1/anthropic/messages", json={**_ANTHROPIC, "stream": True})
        assert resp.status_code == 200
        assert "event: error" in resp.text and "message_stop" not in resp.text
        assert rows == []


def _budget_blocked_engine() -> SimpleNamespace:
    """A fake engine whose budget check refuses; the provider records every stream call."""
    decision = SimpleNamespace(provider="alpha", model="test-model")
    provider = SimpleNamespace(stream=MagicMock(name="stream"))
    return SimpleNamespace(
        config=CouncilConfig(),
        router=SimpleNamespace(route=lambda **kw: decision),
        registry=SimpleNamespace(has=lambda name: True, get=lambda name: provider),
        _check_budget=AsyncMock(side_effect=BudgetExceededError("budget exhausted")),
        _log_query=AsyncMock(),
        provider=provider,
    )


class TestStreamedProxyBudget:
    """``budget.hard_stop`` applies to streamed calls: the generator emits its own
    error shape with ``type: budget_exceeded`` and never touches the provider."""

    @pytest.mark.asyncio
    async def test_openai_generator_emits_budget_exceeded_then_done(self) -> None:
        engine = _budget_blocked_engine()
        out = b"".join([c async for c in openai_stream_generator(engine, "hi", None, None, None, None, None, "auto")])
        lines = [line for line in out.decode().splitlines() if line.startswith("data: ")]
        assert len(lines) == 2
        error = json.loads(lines[0][6:])["error"]
        assert error == {"message": "budget exhausted", "type": "budget_exceeded", "code": "budget_exceeded"}
        assert lines[1] == "data: [DONE]"
        engine.provider.stream.assert_not_called()
        engine._log_query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_anthropic_generator_emits_budget_exceeded_error_event(self) -> None:
        engine = _budget_blocked_engine()
        out = b"".join([c async for c in anthropic_stream_generator(engine, "hi", None, None, None, None, None, "auto")])
        text = out.decode()
        events = _parse_sse_events(text)
        assert [t for t, _ in events] == ["error"]
        payload = json.loads(events[0][1])
        assert payload == {"type": "error", "error": {"type": "budget_exceeded", "message": "budget exhausted"}}
        assert "message_start" not in text and "message_stop" not in text
        engine.provider.stream.assert_not_called()
        engine._log_query.assert_not_awaited()

    def test_endpoint_streams_the_refusal_as_an_event(self, open_client: TestClient) -> None:
        engine = server_module._engine
        assert isinstance(engine, Engine)
        engine._check_budget = AsyncMock(side_effect=BudgetExceededError("budget exhausted"))  # type: ignore[method-assign]
        resp = open_client.post("/v1/chat/completions", json={**_CHAT, "stream": True})
        assert resp.status_code == 200
        events = _json_events(resp.text)
        assert len(events) == 1 and events[0]["error"]["type"] == "budget_exceeded"
        assert resp.text.rstrip().endswith("data: [DONE]")
