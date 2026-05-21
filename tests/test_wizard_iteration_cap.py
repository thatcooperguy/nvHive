"""Tests for the user-controllable max_iterations cap on the Wizard loop."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _engine_emitting_tool_call() -> MagicMock:
    """Engine whose every completion emits a TOOL_CALL so the loop would
    otherwise iterate to the global ceiling."""
    from nvh.providers.base import CompletionResponse, FinishReason, Usage

    def make():
        return CompletionResponse(
            content='Looking.\nTOOL_CALL: {"name": "refresh_models", "arguments": {}}',
            model="ollama/x",
            provider="ollama",
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            cost_usd=Decimal("0"),
            latency_ms=10,
            finish_reason=FinishReason.STOP,
        )

    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock(side_effect=[make(), make(), make(), make()])
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
async def test_default_max_iterations_uses_global_cap(monkeypatch) -> None:
    """No override = uses WIZARD_FOLLOWUP_MAX_ITER (currently 3)."""
    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")
    engine = _engine_emitting_tool_call()
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY),
        patch(
            "nvh.integrations.wizard.chat._run_auto_tool",
            new=AsyncMock(return_value={"ok": True, "result": {}, "safety_class": "auto"}),
        ),
    ):
        result = await chat_mod.wizard_chat("loop")

    assert result["iterations"] == chat_mod.WIZARD_FOLLOWUP_MAX_ITER


@pytest.mark.asyncio
async def test_max_iterations_one_is_a_single_completion(monkeypatch) -> None:
    """The 'just answer me' mode: max_iterations=1 means no follow-up loop
    regardless of tool calls in the response."""
    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")
    engine = _engine_emitting_tool_call()
    auto_run = AsyncMock(return_value={"ok": True, "result": {}, "safety_class": "auto"})
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY),
        patch("nvh.integrations.wizard.chat._run_auto_tool", new=auto_run),
    ):
        result = await chat_mod.wizard_chat("loop", max_iterations=1)

    assert result["iterations"] == 1
    # The first iteration's tool calls are surfaced as confirm/pending but
    # NOT executed because the loop terminated before the tool-execution step.
    auto_run.assert_not_called()


@pytest.mark.asyncio
async def test_max_iterations_higher_than_global_cap_is_clamped(monkeypatch) -> None:
    """Calling with max_iterations=99 must not let the loop run forever —
    the global ceiling still bounds it."""
    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")
    engine = _engine_emitting_tool_call()
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY),
        patch(
            "nvh.integrations.wizard.chat._run_auto_tool",
            new=AsyncMock(return_value={"ok": True, "result": {}, "safety_class": "auto"}),
        ),
    ):
        result = await chat_mod.wizard_chat("loop", max_iterations=99)

    assert result["iterations"] == chat_mod.WIZARD_FOLLOWUP_MAX_ITER


@pytest.mark.asyncio
async def test_max_iterations_two_runs_loop_once_then_stops(monkeypatch) -> None:
    """max_iterations=2 = one tool-using iteration + one reaction. Useful for
    'let it think but not too hard'."""
    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")
    engine = _engine_emitting_tool_call()
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY),
        patch(
            "nvh.integrations.wizard.chat._run_auto_tool",
            new=AsyncMock(return_value={"ok": True, "result": {}, "safety_class": "auto"}),
        ),
    ):
        result = await chat_mod.wizard_chat("loop", max_iterations=2)

    assert result["iterations"] == 2
