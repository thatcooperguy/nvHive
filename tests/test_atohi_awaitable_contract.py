"""Real asyncio Awaitable/iterator contracts with inert lease authority."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from nvh.config.settings import AtohiConfig
from nvh.core.atohi import AdmissionRequest, AdmittedProvider, AtohiAdmission, ResourcePaused
from nvh.providers.base import StreamChunk


class Broker:
    def __init__(self):
        self.revoked = asyncio.Event()
        self.exits = 0

    async def wait_revoked(self):
        await self.revoked.wait()

    @asynccontextmanager
    async def admit(self, request):
        try:
            yield self
        finally:
            self.exits += 1


@pytest.mark.asyncio
async def test_run_accepts_future_returning_call_without_replaying():
    broker = Broker()
    policy = AtohiAdmission(AtohiConfig(enabled=True), broker=broker)
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    calls = []

    def call():
        calls.append("call")
        loop.call_soon(future.set_result, "result")
        return future

    assert await policy.run(AdmissionRequest("ollama", "complete"), call) == "result"
    assert calls == ["call"]
    assert broker.exits == 1


@pytest.mark.asyncio
async def test_revocation_cancels_returned_future_before_release():
    broker = Broker()
    policy = AtohiAdmission(AtohiConfig(enabled=True), broker=broker)
    future = asyncio.get_running_loop().create_future()
    called = asyncio.Event()

    def call():
        called.set()
        return future

    request = asyncio.create_task(policy.run(AdmissionRequest("ollama", "complete"), call))
    try:
        await asyncio.wait_for(called.wait(), 2)
        broker.revoked.set()
        with pytest.raises(ResourcePaused) as caught:
            await request
        assert caught.value.reason == "resource_revoked"
        assert future.cancelled()
        assert broker.exits == 1
    finally:
        future.cancel()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("caller_cancel", [False, True])
async def test_returned_task_cleanup_drains_before_lease_exit(caller_cancel):
    cleaned = asyncio.Event()

    class CleanupBroker(Broker):
        @asynccontextmanager
        async def admit(self, request):
            try:
                yield self
            finally:
                assert cleaned.is_set(), "lease exited before task cleanup"
                self.exits += 1

    broker = CleanupBroker()
    policy = AtohiAdmission(AtohiConfig(enabled=True), broker=broker)
    started = asyncio.Event()
    owned = []

    async def transport():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    def call():
        task = asyncio.create_task(transport())
        owned.append(task)
        return task

    request = asyncio.create_task(policy.run(AdmissionRequest("ollama", "complete"), call))
    try:
        await asyncio.wait_for(started.wait(), 2)
        if caller_cancel:
            request.cancel()
        else:
            broker.revoked.set()
        with pytest.raises(asyncio.CancelledError if caller_cancel else ResourcePaused) as caught:
            await request
        if not caller_cancel:
            assert caught.value.reason == "resource_revoked"
        assert cleaned.is_set() and broker.exits == 1
        assert len(owned) == 1 and owned[0].cancelled()
    finally:
        for task in owned:
            if not task.done():
                task.cancel()
        await asyncio.gather(*owned, request, return_exceptions=True)


@pytest.mark.asyncio
async def test_returned_future_error_remains_original_failure():
    broker = Broker()
    policy = AtohiAdmission(AtohiConfig(enabled=True), broker=broker)
    future = asyncio.get_running_loop().create_future()
    future.set_exception(ValueError("synthetic transport error"))
    try:
        with pytest.raises(ValueError, match="synthetic transport error"):
            await policy.run(AdmissionRequest("ollama", "complete"), lambda: future)
        assert broker.exits == 1
    finally:
        future.exception()  # consume the negative-control exception too


@pytest.mark.asyncio
async def test_plain_async_iterator_without_aclose_keeps_final_after_release():
    class Chunks:
        def __init__(self):
            self.index = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.index += 1
            if self.index == 1:
                return StreamChunk(delta="partial")
            if self.index == 2:
                return StreamChunk(is_final=True)
            raise StopAsyncIteration

    class Provider:
        def stream(self, *args, **kwargs):
            return Chunks()

    broker = Broker()
    policy = AtohiAdmission(AtohiConfig(enabled=True), broker=broker)
    provider = AdmittedProvider(Provider(), policy, "ollama")
    seen = []
    async for chunk in provider.stream():
        seen.append((chunk.is_final, broker.exits))
    assert seen[-1] == (True, 1)
    assert len(seen) == 2 and broker.exits == 1
