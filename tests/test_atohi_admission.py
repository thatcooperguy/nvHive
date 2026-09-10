"""Native admission tests: every provider and HTTP call is a local double."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from nvh.config.settings import (
    AtohiConfig,
    CouncilConfig,
    CouncilModeConfig,
    DefaultsConfig,
    ProviderConfig,
)
from nvh.core.atohi import (
    AdmissionRequest,
    AdmittedProvider,
    AtohiAdmission,
    ResourcePaused,
    wait_resource_tasks,
)
from nvh.core.council import CouncilOrchestrator
from nvh.core.engine import Engine
from nvh.core.router import RoutingDecision
from nvh.integrations.rag import embedder
from nvh.providers.base import (
    CompletionResponse,
    Message,
    ProviderError,
    StreamChunk,
    TaskType,
    Usage,
)
from nvh.providers.registry import ProviderRegistry


class BrokerDouble:
    """Test lifecycle, never evidence of real admission or GPU release."""

    def __init__(self):
        self.revoked = asyncio.Event()
        self.requests = []
        self.exits = 0
        self.watch_error = False
        self.exit_error = False

    async def wait_revoked(self):
        await self.revoked.wait()
        if self.watch_error:
            raise ConnectionError("untrusted broker detail")

    @asynccontextmanager
    async def admit(self, request):
        self.requests.append(request)
        try:
            yield self
        finally:
            self.exits += 1
            if self.exit_error:
                raise ConnectionError("untrusted release detail")


class ProviderDouble:
    def __init__(self, name="ollama", *, hang=False):
        self.name = name
        self.hang = hang
        self.calls = 0
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def complete(self, *args, **kwargs):
        self.calls += 1
        self.started.set()
        try:
            if self.hang:
                await asyncio.Event().wait()
            return CompletionResponse(content="synthetic", model="test", provider=self.name, usage=Usage())
        finally:
            self.closed.set()

    async def stream(self, *args, **kwargs):
        self.calls += 1
        self.started.set()
        try:
            yield StreamChunk(delta="partial", provider=self.name)
            if self.hang:
                await asyncio.Event().wait()
            yield StreamChunk(delta=" done", is_final=True, provider=self.name)
        finally:
            self.closed.set()

    async def health_check(self):
        return await self.complete()

    async def list_models(self):
        return []

    def estimate_tokens(self, text):
        return len(text)


def config(*, enabled=True, names=("ollama", "cloud"), aliases=None):
    return CouncilConfig(
        atohi=AtohiConfig(enabled=enabled),
        defaults=DefaultsConfig(provider=names[0], orchestration_mode="off"),
        providers={n: ProviderConfig(default_model="test", type=(aliases or {}).get(n, "")) for n in names},
        council=CouncilModeConfig(
            default_weights={n: 1 for n in names}, synthesis_provider=names[-1],
            fallback_order=list(names), quorum=1, timeout=5,
        ),
    )


def registry(cfg, broker=None, *, hang=True):
    reg = ProviderRegistry(atohi_broker=broker)
    providers = {name: ProviderDouble(name, hang=hang) for name in cfg.providers}
    for name, provider in providers.items():
        reg.register(name, provider)
    reg.configure_admission(cfg)
    return reg, providers


def test_default_is_disabled_and_registry_keeps_original_adapter():
    cfg = config(enabled=False)
    reg, providers = registry(cfg)
    assert CouncilConfig().atohi.enabled is False
    assert reg.get("ollama") is providers["ollama"]


@pytest.mark.parametrize("value", ["true", 1, None])
def test_activation_requires_actual_boolean(value):
    with pytest.raises(ValidationError):
        AtohiConfig(enabled=value)


@pytest.mark.parametrize("values", [{"granted": True}, {"broker_module": "fake"}, {"managed_providers": ["x", "x"]}, {"managed_providers": ["../x"]}])
def test_unrecognized_security_options_and_invalid_provider_names_rejected(values):
    with pytest.raises(ValidationError):
        AtohiConfig(**values)


def test_profile_cannot_silently_drop_or_override_admission_policy():
    with pytest.raises(ValidationError, match="top-level"):
        CouncilConfig(profiles={"test": {"atohi": {"enabled": True}}})


@pytest.mark.parametrize("local_type", ["ollama", "triton"])
def test_alias_and_explicit_self_hosted_provider_selection(local_type):
    cfg = config(names=("spark", "nim", "cloud"), aliases={"spark": local_type, "nim": "openai_compatible"})
    cfg.atohi.managed_providers = ["nim"]
    reg, providers = registry(cfg)
    assert isinstance(reg.get("spark"), AdmittedProvider)
    assert isinstance(reg.get("nim"), AdmittedProvider)
    assert reg.get("cloud") is providers["cloud"]
    later = ProviderDouble("triton")
    reg.register("triton", later)
    assert isinstance(reg.get("triton"), AdmittedProvider)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["complete", "stream", "health_check"])
async def test_missing_broker_prevents_native_transport(operation):
    reg, providers = registry(config())
    with pytest.raises(ResourcePaused, match="broker_unavailable"):
        if operation == "stream":
            await anext(reg.get("ollama").stream([]))
        else:
            await getattr(reg.get("ollama"), operation)(*([[]] if operation == "complete" else []))
    assert providers["ollama"].calls == 0


@pytest.mark.asyncio
async def test_missing_broker_fails_before_engine_initialization_or_cloud_call(monkeypatch):
    cfg = config()
    reg, providers = registry(cfg)
    engine = Engine(cfg, reg)
    init_db = AsyncMock()
    monkeypatch.setattr("nvh.core.engine.repo.init_db", init_db)
    with pytest.raises(ResourcePaused):
        await engine.initialize()
    init_db.assert_not_awaited()
    assert all(p.calls == 0 for p in providers.values())


@pytest.mark.asyncio
async def test_embedding_config_blocks_before_http_or_model_pull(monkeypatch):
    monkeypatch.setattr(embedder, "load_config", lambda: config())
    transport = MagicMock(side_effect=AssertionError("HTTP must not open"))
    pull = AsyncMock()
    monkeypatch.setattr(embedder.httpx, "AsyncClient", transport)
    monkeypatch.setattr(embedder, "_pull_ollama_model", pull)
    with pytest.raises(ResourcePaused, match="broker_unavailable"):
        await embedder.embed_texts(["private text"])
    transport.assert_not_called()
    pull.assert_not_awaited()


@pytest.mark.asyncio
async def test_embedding_revocation_cancels_batch_once_without_auto_pull(monkeypatch):
    broker = BrokerDouble()
    admission = AtohiAdmission(AtohiConfig(enabled=True), broker=broker)
    entered, closed = asyncio.Event(), asyncio.Event()

    async def post(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    client = AsyncMock()
    client.post.side_effect = post
    context = AsyncMock()
    context.__aenter__.return_value = client
    monkeypatch.setattr(embedder.httpx, "AsyncClient", lambda **kwargs: context)
    pull = AsyncMock()
    monkeypatch.setattr(embedder, "_pull_ollama_model", pull)
    task = asyncio.create_task(embedder.embed_texts(["one", "two"], admission=admission))
    await asyncio.wait_for(entered.wait(), 1)
    broker.revoked.set()
    with pytest.raises(ResourcePaused):
        await asyncio.wait_for(task, 1)
    assert closed.is_set()
    assert client.post.await_count == 1
    pull.assert_not_awaited()
    assert broker.requests == [AdmissionRequest("ollama", "embeddings")]


@pytest.mark.asyncio
async def test_run_revocation_and_unknown_ownership_never_return_success():
    for watch_error in (False, True):
        broker = BrokerDouble()
        broker.watch_error = watch_error
        provider = ProviderDouble(hang=True)
        admission = AtohiAdmission(AtohiConfig(enabled=True), broker=broker)
        task = asyncio.create_task(admission.run(AdmissionRequest("ollama", "complete"), provider.complete))
        await asyncio.wait_for(provider.started.wait(), 1)
        broker.revoked.set()
        with pytest.raises(ResourcePaused) as error:
            await asyncio.wait_for(task, 1)
        assert error.value.reason == ("ownership_unknown" if watch_error else "resource_revoked")
        assert provider.calls == broker.exits == 1
        assert provider.closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_already_revoked_does_not_start_provider(stream):
    broker = BrokerDouble()
    broker.revoked.set()
    provider = ProviderDouble()
    admission = AtohiAdmission(AtohiConfig(enabled=True), broker=broker)
    with pytest.raises(ResourcePaused):
        if stream:
            await anext(admission.stream(AdmissionRequest("ollama", "stream"), provider.stream))
        else:
            await admission.run(AdmissionRequest("ollama", "complete"), provider.complete)
    assert provider.calls == 0
    assert broker.exits == 1


@pytest.mark.asyncio
async def test_stream_revocation_cancels_upstream_even_when_consumer_stops_pulling():
    broker = BrokerDouble()
    provider = ProviderDouble(hang=True)
    admission = AtohiAdmission(AtohiConfig(enabled=True), broker=broker)
    stream = admission.stream(AdmissionRequest("ollama", "stream"), provider.stream)
    assert (await anext(stream)).delta == "partial"
    broker.revoked.set()
    await asyncio.wait_for(provider.closed.wait(), 1)
    with pytest.raises(ResourcePaused):
        await anext(stream)
    assert provider.calls == broker.exits == 1


@pytest.mark.asyncio
async def test_successful_stream_preserves_all_chunks_and_closes_scope():
    broker = BrokerDouble()
    provider = ProviderDouble()
    admission = AtohiAdmission(AtohiConfig(enabled=True), broker=broker)
    chunks = [c.delta async for c in admission.stream(AdmissionRequest("ollama", "stream"), provider.stream)]
    assert chunks == ["partial", " done"]
    assert broker.exits == 1


@pytest.mark.asyncio
async def test_broker_exit_failure_becomes_ownership_unknown():
    broker = BrokerDouble()
    broker.exit_error = True
    admission = AtohiAdmission(AtohiConfig(enabled=True), broker=broker)
    with pytest.raises(ResourcePaused, match="ownership_unknown"):
        await admission.run(AdmissionRequest("ollama", "complete"), ProviderDouble().complete)


@pytest.mark.asyncio
async def test_external_cancellation_drains_work_and_broker_scope():
    broker = BrokerDouble()
    provider = ProviderDouble(hang=True)
    admission = AtohiAdmission(AtohiConfig(enabled=True), broker=broker)
    task = asyncio.create_task(admission.run(AdmissionRequest("ollama", "complete"), provider.complete))
    await asyncio.wait_for(provider.started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.closed.is_set()
    assert broker.exits == 1


@pytest.mark.asyncio
async def test_ordinary_provider_errors_remain_provider_errors():
    broker = BrokerDouble()
    admission = AtohiAdmission(AtohiConfig(enabled=True), broker=broker)
    with pytest.raises(ProviderError):
        await admission.run(AdmissionRequest("ollama", "complete"), AsyncMock(side_effect=ProviderError("ordinary")))


@pytest.mark.asyncio
async def test_chat_revocation_never_reaches_cloud_fallback():
    cfg = config()
    broker = BrokerDouble()
    reg, providers = registry(cfg, broker)
    engine = Engine(cfg, reg)
    decision = RoutingDecision(provider="ollama", model="test", task_type=TaskType.CONVERSATION, confidence=1, scores={}, reason="test")
    task = asyncio.create_task(engine._execute_with_fallback(
        [Message(role="user", content="private")], decision, 0, 16, None, False,
    ))
    await asyncio.wait_for(providers["ollama"].started.wait(), 1)
    broker.revoked.set()
    with pytest.raises(ResourcePaused):
        await asyncio.wait_for(task, 1)
    assert providers["ollama"].calls == 1
    assert providers["cloud"].calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_council_missing_broker_blocks_all_members_before_fanout(stream):
    cfg = config()
    reg, providers = registry(cfg)
    council = CouncilOrchestrator(cfg, reg)
    with pytest.raises(ResourcePaused):
        if stream:
            await council.run_council_streaming("private", on_event=AsyncMock())
        else:
            await council.run_council("private")
    assert all(p.calls == 0 for p in providers.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["council", "council_stream", "compare"])
async def test_fanout_revocation_cancels_siblings_without_synthesis_or_success(mode, monkeypatch):
    cfg = config()
    broker = BrokerDouble()
    reg, providers = registry(cfg, broker)
    engine = Engine(cfg, reg)
    engine._initialized = True
    monkeypatch.setattr(engine, "_check_budget", AsyncMock())
    log = AsyncMock()
    monkeypatch.setattr(engine, "_log_query", log)
    synthesis = AsyncMock()
    engine.council._synthesize = synthesis
    agreement = AsyncMock()
    engine.council._analyze_agreement = agreement
    events = []

    async def event(value):
        events.append(value["type"])

    if mode == "council":
        call = engine.council.run_council("private")
    elif mode == "council_stream":
        call = engine.council.run_council_streaming("private", on_event=event)
    else:
        call = engine.compare("private")
    task = asyncio.create_task(call)
    await asyncio.wait_for(providers["ollama"].started.wait(), 1)
    await asyncio.wait_for(providers["cloud"].started.wait(), 1)
    broker.revoked.set()
    with pytest.raises(ResourcePaused):
        await asyncio.wait_for(task, 1)
    assert all(p.closed.is_set() and p.calls == 1 for p in providers.values())
    synthesis.assert_not_awaited()
    agreement.assert_not_awaited()
    log.assert_not_awaited()
    assert "council_complete" not in events
    assert "synthesis_start" not in events


@pytest.mark.asyncio
async def test_timeout_cleanup_cannot_swallow_late_resource_pause():
    async def late_pause():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise ResourcePaused()

    task = asyncio.create_task(late_pause())
    with pytest.raises(ResourcePaused):
        await wait_resource_tasks([task], timeout=0.01)


@pytest.mark.asyncio
async def test_streaming_chat_revocation_has_no_fallback(monkeypatch):
    cfg = config()
    broker = BrokerDouble()
    reg, providers = registry(cfg, broker)
    engine = Engine(cfg, reg)
    engine._initialized = True
    monkeypatch.setattr(engine, "_check_budget", AsyncMock())
    decision = RoutingDecision(provider="ollama", model="test", task_type=TaskType.CONVERSATION, confidence=1, scores={}, reason="test")
    monkeypatch.setattr(engine.router, "route", lambda **kwargs: decision)
    task = asyncio.create_task(engine.query_stream("private"))
    await asyncio.wait_for(providers["ollama"].started.wait(), 1)
    broker.revoked.set()
    with pytest.raises(ResourcePaused):
        await asyncio.wait_for(task, 1)
    assert providers["ollama"].calls == 1
    assert providers["cloud"].calls == 0


@pytest.mark.asyncio
async def test_stream_consumer_close_cleans_work_and_scope():
    broker = BrokerDouble()
    provider = ProviderDouble(hang=True)
    admission = AtohiAdmission(AtohiConfig(enabled=True), broker=broker)
    stream = admission.stream(AdmissionRequest("ollama", "stream"), provider.stream)
    await anext(stream)
    await stream.aclose()
    assert provider.closed.is_set()
    assert broker.exits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint,method,model", [
    ("query", "query", "QueryRequest"),
    ("conversation_query", "query", "ConversationQueryRequest"),
    ("council_query", "run_council", "CouncilRequest"),
    ("compare", "compare", "CompareRequest"),
])
async def test_http_pause_is_explicit_and_non_retryable(endpoint, method, model, monkeypatch):
    from fastapi import HTTPException

    from nvh.api import server

    engine = MagicMock()
    setattr(engine, method, AsyncMock(side_effect=ResourcePaused()))
    monkeypatch.setattr(server, "get_engine", lambda: engine)
    with pytest.raises(HTTPException) as error:
        await getattr(server, endpoint)(getattr(server, model)(prompt="private"))
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "resource_paused"
    assert error.value.detail["automatic_retry"] is False


@pytest.mark.asyncio
async def test_embedding_http_boundary_preserves_pause(monkeypatch):
    from fastapi import HTTPException

    from nvh.api import server

    engine = Engine(config(), ProviderRegistry())
    monkeypatch.setattr(server, "get_engine", lambda: engine)
    monkeypatch.setattr("nvh.integrations.rag.ask", AsyncMock(side_effect=ResourcePaused("ownership_unknown")))
    with pytest.raises(HTTPException) as error:
        await server.rag_ask_endpoint(server.RagAskRequest(question="private"))
    assert error.value.status_code == 409
    assert error.value.detail["reason"] == "ownership_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("during_stream", [False, True])
async def test_sse_pause_is_terminal_without_done_event(during_stream, monkeypatch):
    from nvh.api import server

    engine = MagicMock()
    engine.config = config()
    engine.initialize = AsyncMock(side_effect=None if during_stream else ResourcePaused("broker_unavailable"))
    engine._check_budget = AsyncMock()
    engine.router.route.return_value = MagicMock(provider="ollama", model="test")

    async def stream(*args, **kwargs):
        yield StreamChunk(delta="partial")
        raise ResourcePaused()

    engine.registry.get.return_value.stream = stream
    events = [event async for event in server._sse_query_stream(engine, server.QueryRequest(prompt="private", stream=True))]
    assert b"resource_paused" in events[-1]
    assert all(b"event: done" not in event for event in events)


def test_cli_pause_exits_without_retry():
    import typer

    from nvh.cli.main import _run

    async def paused():
        raise ResourcePaused()

    with pytest.raises(typer.Exit) as error:
        _run(paused())
    assert error.value.exit_code == 1


@pytest.mark.asyncio
async def test_final_chunk_waits_for_broker_scope_and_cannot_hide_exit_failure():
    for exit_error in (False, True):
        broker = BrokerDouble()
        broker.exit_error = exit_error
        admission = AtohiAdmission(AtohiConfig(enabled=True), broker=broker)
        provider = AdmittedProvider(ProviderDouble(), admission, "ollama")
        stream = provider.stream([])
        assert (await anext(stream)).is_final is False
        if exit_error:
            with pytest.raises(ResourcePaused, match="ownership_unknown"):
                await anext(stream)
        else:
            assert (await anext(stream)).is_final is True
        assert broker.exits == 1
        await stream.aclose()


@pytest.mark.asyncio
async def test_wrapped_stream_early_close_drains_provider_and_scope():
    broker = BrokerDouble()
    original = ProviderDouble(hang=True)
    provider = AdmittedProvider(original, AtohiAdmission(AtohiConfig(enabled=True), broker=broker), "ollama")
    stream = provider.stream([])
    await anext(stream)
    await stream.aclose()
    assert original.closed.is_set()
    assert broker.exits == 1


@pytest.mark.asyncio
async def test_invalid_broker_lease_cannot_start_provider_or_trigger_fallback():
    class InvalidBroker:
        @asynccontextmanager
        async def admit(self, request):
            yield None

    original = ProviderDouble()
    admission = AtohiAdmission(AtohiConfig(enabled=True), broker=InvalidBroker())
    with pytest.raises(ResourcePaused, match="ownership_unknown"):
        await admission.run(AdmissionRequest("ollama", "complete"), original.complete)
    assert original.calls == 0


@pytest.mark.asyncio
async def test_broker_admission_error_discards_prose_without_starting():
    class FailingBroker:
        def admit(self, request):
            raise ConnectionError("429 capacity_exhausted with private provider detail")

    original = ProviderDouble()
    admission = AtohiAdmission(AtohiConfig(enabled=True), broker=FailingBroker())
    with pytest.raises(ResourcePaused) as error:
        await admission.run(AdmissionRequest("ollama", "complete"), original.complete)
    assert error.value.reason == "broker_unavailable"
    assert "private" not in str(error.value.as_dict())
    assert original.calls == 0


@pytest.mark.asyncio
async def test_resource_revocation_wins_simultaneous_provider_completion():
    broker = BrokerDouble()
    admission = AtohiAdmission(AtohiConfig(enabled=True), broker=broker)

    async def call():
        broker.revoked.set()
        return "must not finalize"

    with pytest.raises(ResourcePaused):
        await admission.run(AdmissionRequest("ollama", "complete"), call)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ResourcePaused(), RuntimeError("cleanup uncertain")])
async def test_final_chunk_cannot_hide_pause_or_unknown_cleanup(failure):
    class CleanupFailure:
        async def stream(self, *args, **kwargs):
            try:
                yield StreamChunk(delta="must remain partial", is_final=True)
                await asyncio.Event().wait()
            finally:
                raise failure

    broker = BrokerDouble()
    admission = AtohiAdmission(AtohiConfig(enabled=True), broker=broker)
    provider = AdmittedProvider(CleanupFailure(), admission, "ollama")
    with pytest.raises(ResourcePaused):
        await anext(provider.stream([]))
    assert broker.exits == 1


@pytest.mark.asyncio
async def test_duplicate_compare_target_cannot_create_an_untracked_call(monkeypatch):
    cfg = config(names=("ollama",))
    broker = BrokerDouble()
    reg, providers = registry(cfg, broker)
    engine = Engine(cfg, reg)
    engine._initialized = True
    monkeypatch.setattr(engine, "_check_budget", AsyncMock())
    task = asyncio.create_task(engine.compare("private", providers=["ollama", "ollama"]))
    await asyncio.wait_for(providers["ollama"].started.wait(), 1)
    broker.revoked.set()
    with pytest.raises(ResourcePaused):
        await asyncio.wait_for(task, 1)
    assert providers["ollama"].calls == 1
    assert len(broker.requests) == broker.exits == 1
