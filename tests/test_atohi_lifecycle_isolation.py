"""Engine-policy isolation and revocation through real async-generator cleanup."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nvh.core.atohi import AtohiAdmission, ResourcePaused
from nvh.core.council import CouncilOrchestrator
from nvh.core.engine import Engine
from nvh.providers import registry as registry_module
from nvh.providers.base import StreamChunk
from tests.test_atohi_admission import (
    BrokerDouble,
    ProviderDouble,
    config,
    fixture_model_lease,
    model_wrapper,
    registry,
)


@pytest.fixture(autouse=True)
def private_context(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))


@pytest.mark.parametrize("global_registry", [False, True])
async def test_another_engine_cannot_disable_existing_engine_admission(
    global_registry, monkeypatch,
):
    enabled = config(names=("ollama",))
    shared, originals = registry(enabled, hang=False)
    monkeypatch.setattr(registry_module, "_registry", shared)
    provided = {} if global_registry else {"registry": shared}
    first = Engine(enabled, **provided)
    second = Engine(config(enabled=False, names=("ollama",)), **provided)
    initialize = AsyncMock()
    monkeypatch.setattr("nvh.core.engine.repo.init_db", initialize)
    with pytest.raises(ResourcePaused, match="broker_unavailable"):
        await first.initialize()
    with pytest.raises(ResourcePaused):
        await first.registry.get("ollama").complete([])
    initialize.assert_not_awaited()
    assert originals["ollama"].calls == 0
    # Regular standalone use of the same registered adapter remains usable.
    assert (await second.registry.get("ollama").complete([])).content == "synthetic"
    assert originals["ollama"].calls == 1 and first.config.atohi.enabled


async def test_direct_councils_do_not_reconfigure_each_others_policy():
    enabled = config(names=("ollama",))
    shared, originals = registry(enabled, hang=False)
    first = CouncilOrchestrator(enabled, shared)
    second = CouncilOrchestrator(config(enabled=False, names=("ollama",)), shared)
    with pytest.raises(ResourcePaused):
        await first.run_council("synthetic")
    assert originals["ollama"].calls == 0
    assert (await second.registry.get("ollama").complete([])).content == "synthetic"


async def test_enabled_engine_does_not_wrap_an_existing_disabled_engine():
    disabled = config(enabled=False, names=("ollama",))
    shared, originals = registry(disabled, hang=False)
    normal = Engine(disabled, shared)
    managed = Engine(config(names=("ollama",)), shared)
    assert normal.registry.get("ollama") is originals["ollama"]
    assert (await normal.registry.get("ollama").complete([])).content == "synthetic"
    with pytest.raises(ResourcePaused):
        await managed.registry.get("ollama").complete([])
    assert originals["ollama"].calls == 1


async def test_bound_registry_rejects_policy_mutation_and_preserves_shared_adapter():
    enabled = config(names=("ollama",))
    broker = BrokerDouble()
    shared, originals = registry(enabled, broker, hang=False)
    first = Engine(enabled, shared)
    assert first.registry.admission is first.council.registry.admission
    with pytest.raises(ValueError, match="admission policy"):
        first.registry.configure_admission(config(enabled=False, names=("ollama",)))
    assert (await first.registry.get("ollama").complete([])).content == "synthetic"
    assert len(broker.requests) == broker.exits == originals["ollama"].calls == 1
    # Reusing that bound registry for another engine creates a separate policy.
    second = Engine(config(enabled=False, names=("ollama",)), first.registry)
    assert second.registry.admission is not first.registry.admission
    await second.registry.get("ollama").complete([])
    assert len(broker.requests) == 1 and originals["ollama"].calls == 2


def test_unconfigured_auxiliary_binding_cannot_fall_back_to_disabled_policy():
    with pytest.raises(ValueError, match="not configured"):
        _ = registry_module.ProviderRegistry().admission


async def test_engine_council_sees_late_registered_providers_and_catalog(tmp_path):
    cfg = config(names=("spark",), aliases={"spark": "ollama"})
    broker = BrokerDouble()
    shared, _ = registry(cfg, broker, hang=False)
    engine = Engine(cfg, shared)
    late = ProviderDouble("spark")
    engine.registry.register("spark", late)
    await engine.council.registry.get("spark").complete([])
    assert late.calls == 1 and broker.exits == 1
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text("models:\n  synthetic-model:\n    provider: spark\n", encoding="utf-8")
    engine.registry.load_capabilities(catalog)
    assert engine.council.registry.get_model_info("synthetic-model") is not None
    # Callers may retain and update the original registry after constructing an
    # Engine. Existing views must see the new raw adapter with their own policy.
    replacement = ProviderDouble("spark")
    shared.register("spark", replacement)
    shared.configure_admission(config(enabled=False, names=("spark",)))
    assert shared.get("spark") is replacement
    await engine.council.registry.get("spark").complete([])
    assert replacement.calls == 1 and broker.exits == 2


@pytest.mark.parametrize("watch_error", [False, True])
async def test_revocation_during_upstream_cleanup_prevents_final_success(watch_error):
    broker = BrokerDouble()
    broker.watch_error = watch_error
    cleanup_started, finish_cleanup = asyncio.Event(), asyncio.Event()
    closed = asyncio.Event()

    class ClosingProvider:
        async def stream(self, *args, **kwargs):
            try:
                yield StreamChunk(delta="synthetic final", is_final=True)
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await finish_cleanup.wait()
                closed.set()

    wrapper = model_wrapper(
        ClosingProvider(), AtohiAdmission(config().atohi, broker=broker), "ollama",
    )
    iterator = wrapper.stream([])
    pull = asyncio.create_task(anext(iterator))
    try:
        await asyncio.wait_for(cleanup_started.wait(), 1)
        broker.revoked.set()
        await asyncio.sleep(0)
        finish_cleanup.set()
        with pytest.raises(ResourcePaused) as error:
            await asyncio.wait_for(pull, 1)
        assert error.value.reason == ("ownership_unknown" if watch_error else "resource_revoked")
        assert closed.is_set() and broker.exits == 1
    finally:
        finish_cleanup.set()
        if not pull.done():
            pull.cancel()
            await asyncio.gather(pull, return_exceptions=True)
        await iterator.aclose()


async def test_revocation_during_broker_exit_also_prevents_final_success():
    from contextlib import asynccontextmanager

    class ExitRevokingBroker(BrokerDouble):
        @asynccontextmanager
        async def admit(self, request):
            self.requests.append(request)
            try:
                yield fixture_model_lease(self, request)
            finally:
                self.revoked.set()
                await asyncio.sleep(0)
                self.exits += 1

    broker = ExitRevokingBroker()
    wrapper = model_wrapper(
        ProviderDouble(), AtohiAdmission(config().atohi, broker=broker), "ollama",
    )
    stream = wrapper.stream([])
    assert not (await anext(stream)).is_final
    try:
        with pytest.raises(ResourcePaused):
            await anext(stream)
        assert broker.exits == 1
    finally:
        await stream.aclose()


@pytest.mark.parametrize("operation", ["complete", "health_check"])
async def test_nonstream_broker_exit_revocation_prevents_success(operation):
    from contextlib import asynccontextmanager

    class ExitRevokingBroker(BrokerDouble):
        @asynccontextmanager
        async def admit(self, request):
            self.requests.append(request)
            try:
                yield fixture_model_lease(self, request)
            finally:
                self.revoked.set()
                await asyncio.sleep(0)
                self.exits += 1

    broker = ExitRevokingBroker()
    provider = ProviderDouble()
    wrapper = model_wrapper(provider, AtohiAdmission(config().atohi, broker=broker), "ollama")
    with pytest.raises(ResourcePaused, match="resource_revoked"):
        if operation == "complete":
            await wrapper.complete([])
        else:
            await wrapper.health_check()
    assert provider.calls == 1 and provider.closed.is_set() and broker.exits == 1
    assert broker.requests[0].operation == operation


async def test_revocation_first_drains_upstream_cleanup_without_second_cancellation():
    broker = BrokerDouble()
    cleanup_started, finish_cleanup = asyncio.Event(), asyncio.Event()
    closed = asyncio.Event()

    class SlowClosingProvider:
        async def stream(self, *args, **kwargs):
            try:
                yield StreamChunk(delta="partial", is_final=False)
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await finish_cleanup.wait()
                closed.set()

    wrapper = model_wrapper(
        SlowClosingProvider(), AtohiAdmission(config().atohi, broker=broker), "ollama",
    )
    stream = wrapper.stream([])
    assert (await anext(stream)).delta == "partial"
    pull = asyncio.create_task(anext(stream))
    try:
        broker.revoked.set()
        await asyncio.wait_for(cleanup_started.wait(), 1)
        # Let the caller observe revocation and enter its own drain while the
        # transport is still cleaning up from the monitor's cancellation.
        for _ in range(5):
            await asyncio.sleep(0)
        assert not pull.done() and broker.exits == 0
        finish_cleanup.set()
        with pytest.raises(ResourcePaused, match="resource_revoked"):
            await asyncio.wait_for(pull, 1)
        assert closed.is_set() and broker.exits == 1
    finally:
        finish_cleanup.set()
        if not pull.done():
            pull.cancel()
        await asyncio.gather(pull, return_exceptions=True)
        await stream.aclose()


async def test_unrevoked_slow_cleanup_keeps_watch_live_and_preserves_final_tool_chunk():
    broker = BrokerDouble()
    cleanup_started, finish_cleanup = asyncio.Event(), asyncio.Event()
    captured = {}
    tool_calls = [{"id": "synthetic", "type": "function", "function": {"name": "echo", "arguments": "{}"}}]

    class ToolProvider:
        async def stream(self, *args, **kwargs):
            captured.update(kwargs)
            try:
                yield StreamChunk(delta="", is_final=True, tool_calls=tool_calls)
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await finish_cleanup.wait()

    wrapper = model_wrapper(
        ToolProvider(), AtohiAdmission(config().atohi, broker=broker), "ollama",
    )
    stream = wrapper.stream([], tools=[{"type": "function"}], tool_choice="required")
    pull = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(cleanup_started.wait(), 1)
        assert not pull.done()
        finish_cleanup.set()
        final = await asyncio.wait_for(pull, 1)
        assert final.is_final and final.tool_calls == tool_calls
        assert captured == {"tools": [{"type": "function"}], "tool_choice": "required", "model": "test"}
        assert broker.exits == 1
    finally:
        finish_cleanup.set()
        if not pull.done():
            pull.cancel()
            await asyncio.gather(pull, return_exceptions=True)
        await stream.aclose()
