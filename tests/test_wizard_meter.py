"""Tests for Wizard cost/latency surfacing + fallback signal (Tier 3)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _engine_with_response(cost: str, latency: int, fallback_from: str | None = None):
    from nvh.providers.base import CompletionResponse, FinishReason, Usage

    resp = CompletionResponse(
        content="ok",
        model="ollama/x",
        provider="ollama",
        usage=Usage(input_tokens=42, output_tokens=21, total_tokens=63),
        cost_usd=Decimal(cost),
        latency_ms=latency,
        finish_reason=FinishReason.STOP,
        fallback_from=fallback_from,
    )
    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock(return_value=resp)
    fake_decision = MagicMock()
    fake_decision.provider = "ollama"
    fake_decision.model = "ollama/x"
    fake_engine = MagicMock()
    fake_engine.initialize = AsyncMock()
    fake_engine._check_budget = AsyncMock()
    fake_engine._log_query = AsyncMock()
    fake_engine.router.route = MagicMock(return_value=fake_decision)
    fake_engine.registry.get = MagicMock(return_value=fake_provider)
    fake_engine.config.defaults.temperature = 0.7
    fake_engine.config.defaults.max_tokens = 256
    return fake_engine


_EMPTY = {
    "gpu": {"detected": False},
    "storage": {"available": False},
    "providers": [],
    "ollama_models": [],
    "recent_jobs": [],
    "receipts": {},
    "vault": {},
}


@pytest.mark.asyncio
async def test_chat_envelope_includes_cost_and_latency(monkeypatch) -> None:
    """cost_usd / latency_ms / token counts ride in the response envelope."""
    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")
    engine = _engine_with_response("0.012345", 850)
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY),
    ):
        result = await chat_mod.wizard_chat("hello world")

    assert result["mode"] == "llm"
    assert pytest.approx(result["cost_usd"], abs=1e-6) == 0.012345
    assert result["latency_ms"] == 850
    assert result["input_tokens"] == 42
    assert result["output_tokens"] == 21
    assert result["fallback_from"] is None


@pytest.mark.asyncio
async def test_chat_envelope_surfaces_fallback_from(monkeypatch) -> None:
    """When the provider response reports a fallback_from, the envelope carries it."""
    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")
    engine = _engine_with_response("0", 200, fallback_from="openai")
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY),
    ):
        result = await chat_mod.wizard_chat("anything")

    assert result["fallback_from"] == "openai"


@pytest.mark.asyncio
async def test_chat_envelope_zero_when_provider_omits_meter(monkeypatch) -> None:
    """Providers like Ollama report 0 cost; envelope should still surface
    consistent fields so the UI can hide them gracefully."""
    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")
    engine = _engine_with_response("0", 0)
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY),
    ):
        result = await chat_mod.wizard_chat("anything")

    assert result["cost_usd"] == 0.0
    assert result["latency_ms"] == 0
