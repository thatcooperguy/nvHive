"""Compatible SSE protocols stop on admission loss and close their transport."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Request

from nvh.api import proxy, server
from nvh.config.settings import CouncilConfig
from nvh.core.atohi import ResourcePaused
from nvh.core.engine import Engine
from nvh.providers.base import StreamChunk
from tests.test_atohi_admission import config, registry


def stream_for(protocol, engine):
    common = dict(engine=engine, prompt="synthetic", system_prompt=None, temperature=None, max_tokens=None)
    if protocol == "council":
        return proxy.council_stream_generator(**common, council_size=2, requested_model="council")
    if protocol == "throwdown":
        return proxy.throwdown_stream_generator(**common)
    generate = proxy.anthropic_stream_generator if protocol == "anthropic" else proxy.openai_stream_generator
    return generate(**common, provider_override="ollama", model_override="test", requested_model="test")


def assert_pause_only(data):
    assert b"[DONE]" not in data and b'"finish_reason": "stop"' not in data
    assert b"message_stop" not in data and b"content_block_stop" not in data
    assert b'"stop_reason": "end_turn"' not in data
    events = [json.loads(line[6:]) for line in data.decode().splitlines() if line.startswith("data: ")]
    errors = [e["error"] for e in events if "error" in e]
    assert len(errors) == 1
    assert errors[0]["code"] == "resource_paused" and errors[0]["status"] == "paused"
    assert errors[0]["automatic_retry"] is False


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize("stage", ["route", "stream", "cleanup"])
async def test_proxy_maps_pause_before_iteration_during_tokens_and_during_cleanup(protocol, stage):
    class Iterator:
        def __init__(self):
            self.pulls = self.closes = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.pulls += 1
            if self.pulls == 1:
                return StreamChunk(delta="partial", is_final=stage == "cleanup")
            if stage == "stream":
                raise ResourcePaused("resource_revoked")
            raise StopAsyncIteration

        async def aclose(self):
            self.closes += 1
            if stage == "cleanup":
                raise ResourcePaused("ownership_unknown")

    source = Iterator()
    def route(**kw):
        if stage == "route":
            raise ResourcePaused("broker_unavailable")
        return SimpleNamespace(provider="ollama", model="test")

    engine = SimpleNamespace(config=CouncilConfig(), router=SimpleNamespace(route=route), registry=SimpleNamespace(
        has=lambda name: True, get=lambda name: SimpleNamespace(stream=lambda **kw: source),
    ))
    data = b"".join([chunk async for chunk in stream_for(protocol, engine)])
    assert_pause_only(data)
    assert source.closes == (0 if stage == "route" else 1)
    assert (b"partial" in data) == (stage != "route")


@pytest.mark.parametrize("protocol", ["council", "throwdown"])
async def test_collective_proxy_pause_has_no_synthetic_finish(protocol):
    engine = SimpleNamespace(run_council=AsyncMock(side_effect=ResourcePaused("resource_revoked")))
    data = b"".join([chunk async for chunk in stream_for(protocol, engine)])
    assert_pause_only(data)
    engine.run_council.assert_awaited_once()


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
async def test_consumer_close_drains_provider_without_emitting_an_error(protocol):
    closed = []
    async def source(**kw):
        try:
            yield StreamChunk(delta="partial")
            yield StreamChunk(delta="final", is_final=True)
        finally:
            closed.append(True)

    engine = SimpleNamespace(config=CouncilConfig(), router=SimpleNamespace(route=lambda **kw: SimpleNamespace(provider="ollama", model="test")),
        registry=SimpleNamespace(has=lambda name: True, get=lambda name: SimpleNamespace(stream=source)))
    stream = stream_for(protocol, engine)
    for _ in range(4):
        item = await anext(stream)
        if b"partial" in item:
            break
    else:
        pytest.fail("No actual provider token was observed")
    await stream.aclose()
    assert closed == [True]


@pytest.mark.parametrize("surface", ["openai", "completions", "anthropic"])
async def test_nonstream_proxy_initialization_pause_is_http409(surface, monkeypatch):
    cfg = config(names=("ollama",))
    providers, originals = registry(cfg, hang=False)
    engine = Engine(cfg, providers)
    monkeypatch.setattr(engine, "_check_budget", AsyncMock())
    initialize_db = AsyncMock()
    monkeypatch.setattr("nvh.core.engine.repo.init_db", initialize_db)
    monkeypatch.setattr(server, "get_engine", lambda: engine)
    raw = Request({"type": "http", "headers": []})
    with pytest.raises(HTTPException) as error:
        if surface == "openai":
            await server.proxy_chat_completions(server._ProxyChatRequest(
                model="safe", messages=[{"role": "user", "content": "synthetic"}],
            ), raw)
        elif surface == "completions":
            await server.proxy_completions(server._ProxyCompletionsRequest(model="safe", prompt="synthetic"))
        else:
            await server.anthropic_messages(server.AnthropicMessageRequest(
                model="safe", messages=[{"role": "user", "content": "synthetic"}], max_tokens=32,
            ), raw)
    assert error.value.status_code == 409 and error.value.detail["code"] == "resource_paused"
    assert error.value.detail["automatic_retry"] is False
    assert originals["ollama"].calls == 0
    initialize_db.assert_not_awaited()


@pytest.mark.parametrize("surface", ["openai", "anthropic"])
async def test_public_stream_endpoint_uses_terminal_error_for_managed_provider(surface, monkeypatch):
    cfg = config(names=("ollama",))
    providers, originals = registry(cfg, hang=False)
    engine = Engine(cfg, providers)
    monkeypatch.setattr(server, "get_engine", lambda: engine)
    monkeypatch.setattr(engine.router, "route", lambda **kw: SimpleNamespace(provider="ollama", model="test"))
    raw = Request({"type": "http", "headers": []})
    if surface == "openai":
        response = await server.proxy_chat_completions(server._ProxyChatRequest(
            model="safe", messages=[{"role": "user", "content": "synthetic"}], stream=True,
        ), raw)
    else:
        response = await server.anthropic_messages(server.AnthropicMessageRequest(
            model="safe", messages=[{"role": "user", "content": "synthetic"}], max_tokens=32, stream=True,
        ), raw)
    data = b"".join([chunk async for chunk in response.body_iterator])
    assert_pause_only(data)
    assert originals["ollama"].calls == 0
