"""Actual registry/RAG dispatch with explicitly issued local test transports.

These fixtures prove the application boundary, not native model authority or
GPU cleanup. No job API, model endpoint or operating-system service is used.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nvh.config.settings import AtohiConfig, CouncilConfig, ProviderConfig
from nvh.core.atohi import AllocationIdentity, AtohiAdmission, ModelSession, ResourcePaused
from nvh.integrations.rag import embedder
from nvh.providers.base import CompletionResponse, StreamChunk, Usage
from nvh.providers.registry import ProviderRegistry


class OwnedTransport:
    def __init__(self, *, hang=False):
        self.calls = []
        self.hang = hang
        self.started, self.closed = asyncio.Event(), asyncio.Event()
        self.before_result = None

    async def complete(self, messages, model=None, **kwargs):
        self.calls.append(("complete", model, kwargs))
        self.started.set()
        try:
            if self.hang:
                await asyncio.Event().wait()
            if self.before_result:
                self.before_result()
            return CompletionResponse(content="owned", model=model, provider="spark", usage=Usage())
        finally:
            self.closed.set()

    async def stream(self, messages, model=None, **kwargs):
        self.calls.append(("stream", model, kwargs))
        self.started.set()
        try:
            yield StreamChunk(delta="owned partial", model=model)
            if self.hang:
                await asyncio.Event().wait()
            yield StreamChunk(delta=" final", model=model, is_final=True)
        finally:
            self.closed.set()

    async def health_check(self, *, model):
        self.calls.append(("health_check", model, {}))
        return "owned health"

    async def embeddings(self, texts, *, model, timeout):
        self.calls.append(("embeddings", model, {"timeout": timeout, "count": len(texts)}))
        return [[float(index), 1.0] for index, _ in enumerate(texts)]


class Issuer:
    def __init__(self, transport=None, *, ttl=30):
        self.transport = transport or OwnedTransport()
        self.ttl = ttl
        self.revoked = asyncio.Event()
        self.requests, self.leases = [], []
        self.exits = 0
        self.mutate = None
        self.exit_delay = 0

    @asynccontextmanager
    async def admit(self, request):
        self.requests.append(request)
        allocation = AllocationIdentity("job-test", f"allocation-{len(self.requests)}", 1)
        deadline = time.monotonic() + self.ttl
        lease = SimpleNamespace(allocation=allocation, expires_at_monotonic=deadline,
                                wait_revoked=self.revoked.wait)
        lease.model_session = ModelSession(allocation, request.provider, request.model,
                                          request.operation, deadline, self.transport)
        if self.mutate:
            self.mutate(lease)
        self.leases.append(lease)
        try:
            yield lease
        finally:
            if self.exit_delay:
                await asyncio.sleep(self.exit_delay)
            self.exits += 1


def registry(issuer, *, enabled=True, model="registered/model-v1"):
    config = CouncilConfig(atohi=AtohiConfig(enabled=enabled),
        providers={"spark": ProviderConfig(type="ollama", default_model=model)})
    raw = Mock()
    raw.complete = Mock(side_effect=AssertionError("shared completion transport touched"))
    raw.stream = Mock(side_effect=AssertionError("shared streaming transport touched"))
    raw.health_check = Mock(side_effect=AssertionError("shared health transport touched"))
    reg = ProviderRegistry(atohi_broker=issuer)
    reg.register("spark", raw)
    return reg.scoped(config), raw


@pytest.mark.parametrize("operation", ["complete", "stream", "health_check"])
async def test_real_registry_uses_only_owned_transport(operation):
    issuer = Issuer()
    reg, raw = registry(issuer)
    provider = reg.get("spark")
    if operation == "stream":
        chunks = [chunk async for chunk in provider.stream([])]
        assert chunks[-1].is_final and issuer.transport.closed.is_set()
    elif operation == "complete":
        assert (await provider.complete([])).content == "owned"
    else:
        assert await provider.health_check() == "owned health"
    assert issuer.transport.calls[0][:2] == (operation, "registered/model-v1")
    assert issuer.requests[0].model == "registered/model-v1" and issuer.exits == 1
    raw.complete.assert_not_called()
    raw.stream.assert_not_called()
    raw.health_check.assert_not_called()


@pytest.mark.parametrize("change", ["missing", "job_dict", "allocation", "generation", "provider",
    "model", "operation", "expiry_mismatch", "transport", "expired"])
async def test_invalid_capability_refused_before_any_transport(change):
    issuer = Issuer(ttl=-1 if change == "expired" else 30)

    def mutate(lease):
        session = lease.model_session
        if change == "missing":
            del lease.model_session
        elif change == "job_dict":
            lease.model_session = {"state": "running", "allocation_id": lease.allocation.allocation_id}
        elif change == "allocation":
            lease.model_session = replace(session, allocation=AllocationIdentity("job-other", "allocation-other", 1))
        elif change == "generation":
            lease.model_session = replace(session, allocation=replace(session.allocation, generation=2))
        elif change in {"provider", "model", "operation"}:
            value = {"provider": "other", "model": "other-model", "operation": "embeddings"}[change]
            lease.model_session = replace(session, **{change: value})
        elif change == "expiry_mismatch":
            lease.expires_at_monotonic += 1
        elif change == "transport":
            lease.model_session = replace(session, transport=object())

    issuer.mutate = mutate
    reg, raw = registry(issuer)
    with pytest.raises(ResourcePaused):
        await reg.get("spark").complete([])
    assert not issuer.transport.calls and issuer.exits == 1
    raw.complete.assert_not_called()


@pytest.mark.parametrize("operation", ["complete", "stream"])
async def test_expiry_cancels_blocked_owned_work_even_without_broker_signal(operation):
    transport = OwnedTransport(hang=True)
    issuer = Issuer(transport, ttl=0.03)
    reg, raw = registry(issuer)
    with pytest.raises(ResourcePaused, match="resource_revoked"):
        if operation == "stream":
            async for _ in reg.get("spark").stream([]):
                pass
        else:
            await reg.get("spark").complete([])
    assert transport.started.is_set() and transport.closed.is_set() and issuer.exits == 1
    assert not issuer.revoked.is_set()
    raw.complete.assert_not_called()


async def test_swapping_entire_session_and_allocation_during_call_refuses_result():
    issuer = Issuer()
    reg, _ = registry(issuer)

    def swap():
        lease = issuer.leases[-1]
        lease.allocation = AllocationIdentity("job-other", "allocation-other", 2)
        lease.model_session = replace(lease.model_session, allocation=lease.allocation)

    issuer.transport.before_result = swap
    with pytest.raises(ResourcePaused, match="ownership_unknown"):
        await reg.get("spark").complete([])


async def test_expiry_through_broker_cleanup_prevents_final_success():
    issuer = Issuer(ttl=0.02)
    issuer.exit_delay = 0.04
    reg, _ = registry(issuer)
    with pytest.raises(ResourcePaused, match="resource_revoked"):
        await reg.get("spark").complete([])
    assert issuer.exits == 1


async def test_revocation_during_stream_cleanup_withholds_final_chunk():
    entered, finish = asyncio.Event(), asyncio.Event()

    class Closing(OwnedTransport):
        async def stream(self, *args, **kwargs):
            try:
                yield StreamChunk(delta="final", is_final=True)
                await asyncio.Event().wait()
            finally:
                entered.set()
                await finish.wait()
                self.closed.set()

    issuer = Issuer(Closing())
    reg, _ = registry(issuer)
    iterator = reg.get("spark").stream([])
    task = asyncio.create_task(anext(iterator))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        issuer.revoked.set()
        finish.set()
        with pytest.raises(ResourcePaused):
            await asyncio.wait_for(task, 1)
        assert issuer.transport.closed.is_set()
    finally:
        finish.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await iterator.aclose()


async def test_explicit_model_arguments_are_bound_and_default_is_immutable():
    issuer = Issuer()
    reg, _ = registry(issuer)
    await reg.get("spark").complete([], "explicit-model", temperature=0.2)
    assert issuer.requests[-1].model == "explicit-model"
    assert issuer.transport.calls[-1] == ("complete", "explicit-model", {"temperature": 0.2})
    config = CouncilConfig(atohi=AtohiConfig(enabled=True),
        providers={"spark": ProviderConfig(type="ollama", default_model="different")})
    with pytest.raises(ValueError, match="admission policy"):
        reg.configure_admission(config)
    other = reg.scoped(config)
    await other.get("spark").complete([])
    await reg.get("spark").complete([])
    assert [row.model for row in issuer.requests[-2:]] == ["different", "registered/model-v1"]


@pytest.mark.parametrize("model", ["", "auto", "ollama/__auto__", "has space", None])
async def test_missing_or_automatic_model_refuses_before_admission(model):
    issuer = Issuer()
    reg, raw = registry(issuer, model="")
    with pytest.raises(ResourcePaused, match="admission_denied"):
        await reg.get("spark").complete([], model=model)
    assert not issuer.requests
    raw.complete.assert_not_called()


async def test_rag_uses_owned_batch_without_shared_http_or_pull(monkeypatch):
    issuer = Issuer()
    policy = AtohiAdmission(AtohiConfig(enabled=True), broker=issuer)
    monkeypatch.setattr(embedder, "embed_model_name", lambda: "registered-embedding")
    shared = Mock(side_effect=AssertionError("shared embedding URL touched"))
    pull = Mock(side_effect=AssertionError("shared model pull touched"))
    monkeypatch.setattr(embedder.httpx, "AsyncClient", shared)
    monkeypatch.setattr(embedder, "_pull_ollama_model", pull)
    assert await embedder.embed_texts(["one", "two"], admission=policy) == [[0.0, 1.0], [1.0, 1.0]]
    assert issuer.requests[0].model == "registered-embedding"
    assert issuer.transport.calls == [("embeddings", "registered-embedding", {"timeout": 30.0, "count": 2})]
    shared.assert_not_called()
    pull.assert_not_called()


async def test_disabled_registry_keeps_normal_provider_and_does_not_request_capability():
    issuer = Issuer()
    reg, raw = registry(issuer, enabled=False)
    assert reg.get("spark") is raw and not issuer.requests


async def test_concurrent_engines_cannot_exchange_owned_transports():
    first, second = Issuer(), Issuer()
    reg1, raw1 = registry(first, model="model-one")
    reg2, raw2 = registry(second, model="model-two")
    answers = await asyncio.gather(reg1.get("spark").complete([]), reg2.get("spark").complete([]))
    assert [answer.model for answer in answers] == ["model-one", "model-two"]
    assert first.transport.calls[0][1] == "model-one" and second.transport.calls[0][1] == "model-two"
    raw1.complete.assert_not_called()
    raw2.complete.assert_not_called()


@pytest.mark.parametrize("cloud", [False, True])
async def test_vision_uses_issued_transport_and_exact_image_content(monkeypatch, cloud):
    import sys

    from nvh.core import vision_tools
    from nvh.providers.base import Message

    captured = []

    class Vision(OwnedTransport):
        async def complete(self, messages, model=None, **kwargs):
            captured.append((messages, model, kwargs))
            return await super().complete(messages, model=model, **kwargs)

    for name in ("OPENAI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-unused")
    shared = Mock(side_effect=AssertionError("shared vision transport touched"))
    monkeypatch.setattr("httpx.AsyncClient", shared)
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=shared))
    issuer = Issuer(Vision())
    policy = AtohiAdmission(AtohiConfig(enabled=True, managed_providers=["openai"]), broker=issuer)
    if cloud:
        result = await vision_tools._analyze_with_cloud("aW1hZ2U=", "image/jpeg", "look", admission=policy)
    else:
        result = await vision_tools._analyze_with_ollama("aW1hZ2U=", "look", "vision-exact", admission=policy)
    assert result == "owned" and len(captured) == 1
    messages, model, _ = captured[0]
    assert isinstance(messages[0], Message)
    assert messages[0].content == [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,aW1hZ2U=" if cloud else "aW1hZ2U="}},
    ]
    assert model == issuer.requests[0].model == ("gpt-5.6-terra" if cloud else "vision-exact")
    assert issuer.requests[0].provider == ("openai" if cloud else "ollama")
    shared.assert_not_called()


@pytest.mark.parametrize("failure", ["missing", "expired", "transport_error"])
async def test_managed_vision_failure_never_falls_back_to_cloud(monkeypatch, failure):
    import sys

    from nvh.core import vision_tools

    shared = Mock(side_effect=AssertionError("unmanaged fallback touched"))
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=shared))
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-unused")
    monkeypatch.setenv("GOOGLE_API_KEY", "synthetic-unused")
    issuer = Issuer(ttl=-1 if failure == "expired" else 30)
    if failure == "missing":
        issuer.mutate = lambda lease: delattr(lease, "model_session")
    elif failure == "transport_error":
        async def fail(*args, **kwargs):
            raise RuntimeError("connection lost")
        issuer.transport.complete = fail
    policy = AtohiAdmission(AtohiConfig(enabled=True, managed_providers=["openai"]), broker=issuer)
    with pytest.raises(ResourcePaused):
        await vision_tools._analyze_with_cloud("data", "image/png", "look", admission=policy)
    assert len(issuer.requests) == 1
    shared.assert_not_called()


@pytest.mark.parametrize("operation", ["complete", "stream"])
async def test_bound_provider_error_is_terminal_pause(operation):
    class Broken(OwnedTransport):
        async def complete(self, *args, **kwargs):
            raise RuntimeError("connection failed")

        async def stream(self, *args, **kwargs):
            yield StreamChunk(delta="partial")
            raise RuntimeError("connection failed")

    issuer = Issuer(Broken())
    reg, raw = registry(issuer)
    with pytest.raises(ResourcePaused, match="ownership_unknown"):
        if operation == "complete":
            await reg.get("spark").complete([])
        else:
            _ = [chunk async for chunk in reg.get("spark").stream([])]
    assert len(issuer.requests) == issuer.exits == 1
    raw.complete.assert_not_called()
    raw.stream.assert_not_called()


async def test_queued_partial_is_not_delivered_after_deadline_before_timer_runs(monkeypatch):
    from nvh.core import atohi

    queued = asyncio.Event()

    class Buffered(OwnedTransport):
        async def stream(self, *args, **kwargs):
            yield StreamChunk(delta="first")
            yield StreamChunk(delta="queued second")
            queued.set()
            await asyncio.Event().wait()

    issuer = Issuer(Buffered())
    reg, _ = registry(issuer)
    stream = reg.get("spark").stream([])
    try:
        assert (await anext(stream)).delta == "first"
        await asyncio.wait_for(queued.wait(), 1)
        # Advance only the boundary's clock; its sleep task is still pending.
        deadline = issuer.leases[0].expires_at_monotonic
        monkeypatch.setattr(atohi, "time", SimpleNamespace(monotonic=lambda: deadline + 1))
        with pytest.raises(ResourcePaused, match="resource_revoked"):
            await anext(stream)
    finally:
        try:
            await stream.aclose()
        except ResourcePaused:
            pass


@pytest.mark.parametrize("owned", [True, False])
async def test_stream_close_failure_is_terminal_only_for_owned_model_transport(owned):
    from nvh.core.atohi import AdmissionRequest

    class FailingClose:
        def __init__(self):
            self.sent = False
            self.closes = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return StreamChunk(delta="partial")

        async def aclose(self):
            self.closes += 1
            raise OSError("owned connection failed during close")

    upstream = FailingClose()
    issuer = Issuer(SimpleNamespace(stream=lambda *args, **kwargs: upstream))
    reg, raw = registry(issuer)
    if owned:
        stream = reg.get("spark").stream([])
    else:
        # Generic trusted lifecycle plumbing retains its existing exceptions.
        stream = reg.admission.stream(AdmissionRequest("spark", "stream", "registered/model-v1"), lambda: upstream)
    with pytest.raises(ResourcePaused if owned else OSError):
        _ = [chunk async for chunk in stream]
    assert upstream.closes == 1 and issuer.exits == 1
    raw.stream.assert_not_called()
