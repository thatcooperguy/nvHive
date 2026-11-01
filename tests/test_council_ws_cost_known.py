"""The council's WebSocket events carry ``cost_known`` beside ``cost`` / ``total_cost`` (0.46).

``member_complete``, ``synthesis_complete`` and ``council_complete`` from
:meth:`nvh.core.council.CouncilOrchestrator.run_council_streaming` say whether
the $0 (or the price) they carry is the catalog's verdict on the model that
answered -- the same lookup the REST ``/v1/council`` envelope uses
(:func:`nvh.core.agent_loop.model_cost_known`) -- so the web council card can
tell a free member from a $0 nobody priced. A council that priced nothing is
not a known $0.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from nvh.config.settings import CouncilConfig, CouncilModeConfig, ProviderConfig
from nvh.core.council import CouncilOrchestrator
from nvh.providers.base import FinishReason, ModelInfo, StreamChunk, Usage
from nvh.providers.registry import ProviderRegistry


def _config(*names: str) -> CouncilConfig:
    return CouncilConfig(
        providers={n: ProviderConfig(default_model=f"m-{n}") for n in names},
        council=CouncilModeConfig(
            default_weights={n: 1.0 for n in names}, synthesis_provider=names[0], quorum=1, timeout=10,
        ),
    )


def _stream(model: str, cost: str, *, tokens: int = 5):
    async def _chunks(*args, **kwargs):
        yield StreamChunk(delta="hello", is_final=False, model=model)
        yield StreamChunk(
            delta="", is_final=True, model=model, cost_usd=Decimal(cost), finish_reason=FinishReason.STOP,
            usage=Usage(input_tokens=tokens, output_tokens=0, total_tokens=tokens),
        )

    return _chunks


def _failing_stream(*args, **kwargs):
    async def _chunks():
        raise RuntimeError("503 upstream")
        yield  # pragma: no cover - makes this an async generator

    return _chunks()


async def _events(registry: ProviderRegistry, *names: str) -> list[dict]:
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    orch = CouncilOrchestrator(_config(*names), registry)
    await orch.run_council_streaming(query="q", on_event=on_event, synthesize=False, timeout=10)
    return events


def _registry(*names: str) -> ProviderRegistry:
    reg = ProviderRegistry()
    for n in names:
        reg.register(n, AsyncMock())
    return reg


@pytest.mark.asyncio
async def test_member_and_total_events_carry_the_catalogs_verdict() -> None:
    """One member's model is priced at $0 by its row, the other's row says nobody priced it: the members say so
    each, and the total -- a floor once any member is unpriced -- is not known."""
    reg = _registry("provA", "provB")
    reg._model_catalog["m-provA"] = ModelInfo(model_id="m-provA", provider="provA", cost_known=True)
    reg._model_catalog["m-provB"] = ModelInfo(model_id="m-provB", provider="provB", cost_known=False)
    reg.get("provA").stream = _stream("m-provA", "0")
    reg.get("provB").stream = _stream("m-provB", "0")

    events = await _events(reg, "provA", "provB")
    members = {e["member"]: e for e in events if e["type"] == "member_complete"}
    assert members["provA"]["cost"] == "0" and members["provA"]["cost_known"] is True
    assert members["provB"]["cost"] == "0" and members["provB"]["cost_known"] is False
    done = events[-1]
    assert done["type"] == "council_complete" and done["total_cost"] == "0" and done["cost_known"] is False


@pytest.mark.asyncio
async def test_total_is_known_when_every_member_was_priced_and_a_bill_is_known_without_a_row() -> None:
    reg = _registry("provA", "provB")
    reg._model_catalog["m-provA"] = ModelInfo(model_id="m-provA", provider="provA", cost_known=True)
    reg.get("provA").stream = _stream("m-provA", "0")
    reg.get("provB").stream = _stream("m-provB", "0.004")  # no row, but a non-zero bill was priced by something

    events = await _events(reg, "provA", "provB")
    members = {e["member"]: e for e in events if e["type"] == "member_complete"}
    assert members["provA"]["cost_known"] is True and members["provB"]["cost_known"] is True
    done = events[-1]
    assert done["type"] == "council_complete" and done["cost_known"] is True


@pytest.mark.asyncio
async def test_a_council_that_priced_nothing_is_not_a_known_zero() -> None:
    reg = _registry("provA", "provB")
    reg.get("provA").stream = _failing_stream
    reg.get("provB").stream = _failing_stream

    events = await _events(reg, "provA", "provB")
    assert [e["type"] for e in events if e["type"] == "member_complete"] == []
    assert len([e for e in events if e["type"] == "member_failed"]) == 2
    done = events[-1]
    assert done["type"] == "council_complete" and done["total_cost"] == "0" and done["cost_known"] is False
