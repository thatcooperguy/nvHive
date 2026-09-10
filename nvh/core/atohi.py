"""Native call admission and cancellation, with no production Atohi broker.

The broker protocol is a trusted in-process extension point, not the Atohi
HTTP API. No configuration flag, response prose or caller-supplied boolean
constitutes a lease. A future broker must bind the actual workload, verify
ownership and retain allocations until node-side physical release is proven.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeVar

from nvh.config.settings import AtohiConfig

PauseReason = Literal["broker_unavailable", "admission_denied", "resource_revoked", "ownership_unknown"]
T = TypeVar("T")


class ResourcePaused(BaseException):
    """Terminal call interruption, deliberately outside provider retry errors.

    Callers must handle this explicitly as paused/cancelled. Never translate it
    into ProviderError, an HTTP quota error, or an ordinary council member loss.
    """

    def __init__(self, reason: PauseReason = "resource_revoked") -> None:
        if reason not in {"broker_unavailable", "admission_denied", "resource_revoked", "ownership_unknown"}:
            raise ValueError("Unknown resource pause reason")
        self.reason = reason
        super().__init__(reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": "Work paused by resource admission.", "code": "resource_paused",
            "reason": self.reason, "status": "paused", "automatic_retry": False,
        }


@dataclass(frozen=True)
class AdmissionRequest:
    """Content-free local call identity; not an Atohi workload specification."""

    provider: str
    operation: Literal["complete", "stream", "health_check", "embeddings"]


class ResourceLease(Protocol):
    async def wait_revoked(self) -> None:
        """Return on revocation, or raise on loss of ownership knowledge."""
        ...


class AdmissionBroker(Protocol):
    def admit(self, request: AdmissionRequest) -> AbstractAsyncContextManager[ResourceLease]:
        """Acquire authoritative admission; exit must not assert GPU release."""
        ...


async def _watch_lease(lease: ResourceLease) -> None:
    # Put malformed/failed broker implementations inside the ownership monitor;
    # they must never escape as ordinary provider failures and trigger fallback.
    await lease.wait_revoked()


async def _cancel_and_drain(tasks: Iterable[asyncio.Task[Any]]) -> None:
    tasks = tuple(tasks)
    for task in tasks:
        # A task may already be awaiting transport cleanup in its CancelledError
        # handler. Repeating cancel() would interrupt that cleanup instead of
        # waiting for the first cancellation to finish.
        if not task.done() and not task.cancelling():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _raise_pause(tasks: Iterable[asyncio.Task[Any]]) -> None:
    for task in tasks:
        if task.done() and not task.cancelled():
            error = task.exception()
            if isinstance(error, ResourcePaused):
                raise error


async def wait_resource_tasks(
    tasks: Iterable[asyncio.Task[Any]], *, timeout: float | None = None,
) -> tuple[set[asyncio.Task[Any]], set[asyncio.Task[Any]]]:
    """Wait for fan-out, abort siblings promptly when any resource is paused.

    Ordinary provider failures retain existing quorum behavior. Cancellation
    drains cooperating client tasks; it is not proof that remote GPU work ended.
    """
    all_tasks = set(tasks)
    pending = set(all_tasks)
    deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
    try:
        while pending:
            remaining = None if deadline is None else max(0, deadline - asyncio.get_running_loop().time())
            done, pending = await asyncio.wait(
                pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED,
            )
            _raise_pause(done)
            if not done:
                break
    finally:
        await _cancel_and_drain(all_tasks)
    # A provider may report revocation while handling the timeout cancellation.
    _raise_pause(all_tasks)
    return all_tasks - pending, pending


class AtohiAdmission:
    """Disabled unless configured; enabled without a broker always fails closed.

    Broker injection is for reviewed native code and hermetic tests only. The
    shipped application supplies no broker, endpoint, token, or mock lease.
    """

    def __init__(self, config: AtohiConfig, *, broker: AdmissionBroker | None = None) -> None:
        self.enabled = config.enabled
        self.managed_providers = frozenset(config.managed_providers)
        self._broker = broker

    def manages(self, name: str, provider_type: str = "") -> bool:
        return self.enabled and (
            name in self.managed_providers or (provider_type or name) in {"ollama", "triton"}
        )

    def require_broker(self) -> None:
        if self.enabled and self._broker is None:
            raise ResourcePaused("broker_unavailable")

    @asynccontextmanager
    async def _lease(self, request: AdmissionRequest) -> AsyncIterator[ResourceLease]:
        self.require_broker()
        assert self._broker is not None
        try:
            context = self._broker.admit(request)
            lease = await context.__aenter__()
        except Exception:
            raise ResourcePaused("broker_unavailable") from None
        try:
            yield lease
        finally:
            try:
                # Never allow a broker context to suppress cancellation/failure.
                await context.__aexit__(*sys.exc_info())
            except Exception:
                raise ResourcePaused("ownership_unknown") from None

    @staticmethod
    def _revocation(watch: asyncio.Task[None]) -> None:
        if watch.done():
            if watch.cancelled() or watch.exception() is not None:
                raise ResourcePaused("ownership_unknown")
            raise ResourcePaused("resource_revoked")

    async def run(self, request: AdmissionRequest, call: Callable[[], Awaitable[T]]) -> T:
        if not self.enabled:
            return await call()
        watch: asyncio.Task[None] | None = None
        work: asyncio.Task[T] | None = None
        try:
            async with self._lease(request) as lease:
                watch = asyncio.create_task(_watch_lease(lease))
                try:
                    # Do not invoke a provider when admission is already revoked.
                    await asyncio.sleep(0)
                    self._revocation(watch)

                    async def invoke_call() -> T:
                        # A trusted call may return any Awaitable, including an
                        # existing Future/Task. The lease owns this wrapper task
                        # and drains propagated cancellation before release.
                        return await call()

                    work = asyncio.create_task(invoke_call())
                    await asyncio.wait({watch, work}, return_when=asyncio.FIRST_COMPLETED)
                    self._revocation(watch)  # revocation wins a simultaneous completion
                    return work.result()
                finally:
                    await _cancel_and_drain(t for t in (work,) if t is not None)
        finally:
            try:
                if watch is not None:
                    # Result delivery must wait for work cleanup and broker exit,
                    # with ownership still observed throughout that whole scope.
                    await asyncio.sleep(0)
                    self._revocation(watch)
            finally:
                await _cancel_and_drain(t for t in (watch,) if t is not None)

    async def stream(
        self, request: AdmissionRequest, call: Callable[[], AsyncIterator[T]],
    ) -> AsyncGenerator[T, None]:
        if not self.enabled:
            async for chunk in call():
                yield chunk
            return
        watch: asyncio.Task[None] | None = None
        work: asyncio.Task[None] | None = None
        monitor_task: asyncio.Task[None] | None = None
        take: asyncio.Task[T] | None = None
        closing_upstream = False
        try:
            async with self._lease(request) as lease:
                queue: asyncio.Queue[T] = asyncio.Queue(maxsize=1)

                async def pump() -> None:
                    iterator = call()
                    try:
                        async for chunk in iterator:
                            await queue.put(chunk)
                    finally:
                        close = getattr(iterator, "aclose", None)
                        if close is not None:
                            await close()

                watch = asyncio.create_task(_watch_lease(lease))

                async def monitor() -> None:
                    try:
                        await watch
                    finally:
                        # During cleanup the existing cancellation is draining
                        # transport finally/aclose; don't interrupt it a second
                        # time. The watch still records revocation for the caller.
                        if not closing_upstream and work is not None and not work.done():
                            work.cancel()

                try:
                    await asyncio.sleep(0)
                    self._revocation(watch)
                    work = asyncio.create_task(pump())
                    monitor_task = asyncio.create_task(monitor())
                    while True:
                        self._revocation(watch)
                        if work.done():
                            work.result()
                            if queue.empty():
                                return
                            yield queue.get_nowait()
                            continue
                        take = asyncio.create_task(queue.get())
                        await asyncio.wait({take, work, watch}, return_when=asyncio.FIRST_COMPLETED)
                        self._revocation(watch)
                        if not take.done():
                            if work.done():
                                work.result()
                                await _cancel_and_drain((take,))
                                return
                            continue
                        yield take.result()
                        take = None
                finally:
                    closing_upstream = True
                    closing = sys.exc_info()[0] in (None, GeneratorExit)
                    # Keep ownership observation alive through all upstream
                    # cleanup and the broker's __aexit__, not just its last chunk.
                    await _cancel_and_drain(t for t in (take, work) if t is not None)
                    if work is not None:
                        _raise_pause((work,))
                        if closing and not work.cancelled() and work.exception() is not None:
                            raise ResourcePaused("ownership_unknown") from None
        finally:
            try:
                if watch is not None:
                    # A cleanup routine may just have signaled the lease; let
                    # that monitor observe it before we stop our own watch.
                    await asyncio.sleep(0)
                    self._revocation(watch)
            finally:
                await _cancel_and_drain(
                    t for t in (watch, monitor_task) if t is not None
                )


class AdmittedProvider:
    """Wrap an existing registered provider without loading its transport early."""

    def __init__(self, provider: Any, admission: AtohiAdmission, name: str) -> None:
        self.provider = provider
        self.admission = admission
        self.name = name

    async def complete(self, *args: Any, **kwargs: Any) -> Any:
        return await self.admission.run(
            AdmissionRequest(self.name, "complete"), lambda: self.provider.complete(*args, **kwargs),
        )

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        iterator = self.admission.stream(
            AdmissionRequest(self.name, "stream"), lambda: self.provider.stream(*args, **kwargs),
        )
        final_chunk = None
        try:
            async for chunk in iterator:
                if chunk.is_final:
                    # Callers commonly break on is_final. Close the admission
                    # scope before exposing that success boundary to them.
                    final_chunk = chunk
                    break
                yield chunk
        finally:
            await iterator.aclose()
        if final_chunk is not None:
            yield final_chunk

    async def health_check(self) -> Any:
        # Several adapters perform a small inference as their health check.
        return await self.admission.run(
            AdmissionRequest(self.name, "health_check"), self.provider.health_check,
        )

    async def list_models(self) -> Any:
        return await self.provider.list_models()

    def estimate_tokens(self, text: str) -> int:
        return self.provider.estimate_tokens(text)
