"""Tests for the PackInstaller protocol scaffolding."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from nvh.integrations.installs._base import (
    InstallEvent,
    PackInstaller,
    PackInstallerRegistry,
    make_event,
)


def test_make_event_default_shape() -> None:
    event = make_event("start", message="hello")
    assert event == {"event": "start", "status": "info", "message": "hello"}


def test_make_event_with_extras() -> None:
    event = make_event("download", status="ok", message="done", url="https://x", pct=42)
    assert event["event"] == "download"
    assert event["status"] == "ok"
    assert event["url"] == "https://x"
    assert event["pct"] == 42


def test_registry_register_and_get() -> None:
    class FakeInstaller(PackInstaller):
        install_kind = "fake"

        async def install(self, pack: Any, force_update: bool) -> AsyncIterator[InstallEvent]:
            yield make_event("start")

    reg = PackInstallerRegistry()
    inst = FakeInstaller()
    reg.register(inst)
    assert reg.get("fake") is inst
    assert reg.known_kinds() == ["fake"]
    assert reg.get("missing") is None


def test_registry_rejects_empty_install_kind() -> None:
    class Empty(PackInstaller):
        install_kind = ""

        async def install(self, pack: Any, force_update: bool) -> AsyncIterator[InstallEvent]:
            yield make_event("start")

    reg = PackInstallerRegistry()
    with pytest.raises(ValueError, match="empty install_kind"):
        reg.register(Empty())


def test_registry_overwrite_warns() -> None:
    # Attach a stub handler directly to the module logger so the assertion
    # doesn't depend on caplog/global-logger config that other tests can
    # mutate (running this test in isolation passes; in the full suite it
    # was order-sensitive without this).
    import logging

    class A(PackInstaller):
        install_kind = "dup"

        async def install(self, pack: Any, force_update: bool) -> AsyncIterator[InstallEvent]:
            yield make_event("start")

    class B(PackInstaller):
        install_kind = "dup"

        async def install(self, pack: Any, force_update: bool) -> AsyncIterator[InstallEvent]:
            yield make_event("start")

    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    module_logger = logging.getLogger("nvh.integrations.installs._base")
    handler = _Capture(level=logging.WARNING)
    module_logger.addHandler(handler)
    prior_level = module_logger.level
    module_logger.setLevel(logging.WARNING)
    try:
        reg = PackInstallerRegistry()
        reg.register(A())
        reg.register(B())
    finally:
        module_logger.removeHandler(handler)
        module_logger.setLevel(prior_level)

    assert isinstance(reg.get("dup"), B)
    assert any("Overwriting installer" in r.getMessage() for r in captured)


@pytest.mark.asyncio
async def test_default_detect_is_false() -> None:
    class FakeInstaller(PackInstaller):
        install_kind = "fake"

        async def install(self, pack: Any, force_update: bool) -> AsyncIterator[InstallEvent]:
            yield make_event("start")

    inst = FakeInstaller()
    assert (await inst.detect(pack=object())) is False


@pytest.mark.asyncio
async def test_subclass_install_yields_events() -> None:
    class CountingInstaller(PackInstaller):
        install_kind = "counter"

        async def install(self, pack: Any, force_update: bool) -> AsyncIterator[InstallEvent]:
            yield make_event("start", message="begin")
            yield make_event("progress", pct=50)
            yield make_event("complete", status="ok", message="done")

    events = []
    inst = CountingInstaller()
    async for event in inst.install(pack=None, force_update=False):
        events.append(event)
    assert [e["event"] for e in events] == ["start", "progress", "complete"]
    assert events[-1]["status"] == "ok"
