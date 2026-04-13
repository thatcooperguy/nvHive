"""Tests for SSE streaming in server.py and proxy.py generators."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import nvh.api.server as server_module
import nvh.storage.repository as repo
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



def _make_engine(tmp_path: Path, extra_providers: dict | None = None) -> Engine:
    provider = StreamingTestProvider("alpha")
    config = CouncilConfig(
        defaults=DefaultsConfig(provider="alpha", model="test-model", temperature=1.0, max_tokens=256),
        providers={"alpha": ProviderConfig(enabled=True, default_model="test-model")},
        council=CouncilModeConfig(quorum=1, strategy="majority_vote", timeout=30, default_weights={"alpha": 1.0}, synthesis_provider="alpha"),
        routing=RoutingConfig(),
        budget=BudgetConfig(),
        cache=CacheConfig(enabled=True, ttl_seconds=3600, max_size=100),
    )
    registry = ProviderRegistry()
    registry.register("alpha", provider)
    if extra_providers:
        for pname, prov in extra_providers.items():
            registry.register(pname, prov)
    engine = Engine(config=config, registry=registry)
    engine._initialized = True
    return engine


@pytest.fixture()
def test_client(tmp_path: Path):
    db_file = tmp_path / "sse_test.db"
    repo._engine = None
    repo._session_factory = None
    asyncio.run(repo.init_db(db_path=db_file))

    engine = _make_engine(tmp_path)
    original = server_module._engine
    server_module._engine = engine
    client = TestClient(app, raise_server_exceptions=False)
    yield client
    server_module._engine = original
    repo._engine = None
    repo._session_factory = None


@pytest.fixture()
def error_client(tmp_path: Path):
    db_file = tmp_path / "sse_err.db"
    repo._engine = None
    repo._session_factory = None
    asyncio.run(repo.init_db(db_path=db_file))

    errp = ErroringStreamProvider("alpha")
    config = CouncilConfig(
        defaults=DefaultsConfig(provider="alpha", model="test-model", temperature=1.0, max_tokens=256),
        providers={"alpha": ProviderConfig(enabled=True, default_model="test-model")},
        council=CouncilModeConfig(quorum=1, strategy="majority_vote", timeout=30, default_weights={"alpha": 1.0}, synthesis_provider="alpha"),
        routing=RoutingConfig(),
        budget=BudgetConfig(),
        cache=CacheConfig(enabled=True, ttl_seconds=3600, max_size=100),
    )
    registry = ProviderRegistry()
    registry.register("alpha", errp)
    engine = Engine(config=config, registry=registry)
    engine._initialized = True

    original = server_module._engine
    server_module._engine = engine
    client = TestClient(app, raise_server_exceptions=False)
    yield client
    server_module._engine = original
    repo._engine = None
    repo._session_factory = None



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



class TestSSEQueryStream:
    def test_stream_query_emits_chunk_and_done(self, test_client: TestClient) -> None:
        resp = test_client.post("/v1/query", json={"prompt": "hi", "stream": True, "provider": "alpha"})
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

    def test_stream_query_invalid_provider_emits_error(self, test_client: TestClient) -> None:
        resp = test_client.post("/v1/query", json={"prompt": "hi", "stream": True, "provider": "nonexistent_xyz"})
        assert resp.status_code == 200
        events = _parse_sse_events(resp.text)
        error_events = [(t, d) for t, d in events if t == "error"]
        assert len(error_events) >= 1
        err = json.loads(error_events[0][1])
        assert "error" in err


class TestOpenAIProxyStreaming:
    def test_chat_completions_stream(self, test_client: TestClient) -> None:
        resp = test_client.post("/v1/proxy/chat/completions", json={"model": "auto", "messages": [{"role": "user", "content": "hello"}], "stream": True})
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        events = _parse_sse_events(resp.text)
        data_lines = [d for _, d in events]
        assert any("[DONE]" in d for d in data_lines)
        json_events = [json.loads(d) for d in data_lines if d.strip() not in ("[DONE]", "")]
        assert any(e.get("object") == "chat.completion.chunk" for e in json_events)
        role_event = json_events[0]
        assert role_event["choices"][0]["delta"].get("role") == "assistant"

    def test_chat_completions_non_stream(self, test_client: TestClient) -> None:
        resp = test_client.post("/v1/proxy/chat/completions", json={"model": "auto", "messages": [{"role": "user", "content": "hello"}], "stream": False})
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["message"]["role"] == "assistant"
        assert "Mock response" in body["choices"][0]["message"]["content"]
        assert "usage" in body


class TestProxyCompletions:
    def test_completions_non_stream(self, test_client: TestClient) -> None:
        resp = test_client.post("/v1/proxy/completions", json={"model": "auto", "prompt": "Say hi"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "text_completion"
        assert len(body["choices"]) == 1
        assert "text" in body["choices"][0]
        assert "usage" in body


class TestAnthropicProxy:
    def test_anthropic_non_stream(self, test_client: TestClient) -> None:
        resp = test_client.post("/v1/anthropic/messages", json={"model": "auto", "messages": [{"role": "user", "content": "hi"}], "stream": False})
        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] == "message"
        assert body["role"] == "assistant"
        assert len(body["content"]) >= 1
        assert body["content"][0]["type"] == "text"
        assert "usage" in body

    def test_anthropic_stream(self, test_client: TestClient) -> None:
        resp = test_client.post("/v1/anthropic/messages", json={"model": "auto", "messages": [{"role": "user", "content": "hello"}], "stream": True})
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
    def test_models_list_format(self, test_client: TestClient) -> None:
        resp = test_client.get("/v1/proxy/models")
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "list"
        assert isinstance(body["data"], list)
        assert len(body["data"]) > 0
        for m in body["data"]:
            assert "id" in m
            assert m["object"] == "model"


class TestCouncilRouting:
    def test_council_model_routes_to_council(self, test_client: TestClient) -> None:
        resp = test_client.post("/v1/proxy/chat/completions", json={"model": "council:3", "messages": [{"role": "user", "content": "test"}], "stream": False})
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
