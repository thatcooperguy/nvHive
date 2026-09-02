"""Tests for the per-agent cost ceiling guard."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _engine_with_costs(costs: list[str]):
    """Engine whose successive completions report each cost in `costs`.

    Each completion also emits a TOOL_CALL so the follow-up loop would
    normally iterate — we want to verify that the ceiling stops iteration,
    not that the model decides to stop.
    """
    from nvh.providers.base import CompletionResponse, FinishReason, Usage

    def make(cost: str):
        return CompletionResponse(
            content='Working.\nTOOL_CALL: {"name": "refresh_models", "arguments": {}}',
            model="cloud/expensive",
            provider="cloud",
            usage=Usage(input_tokens=100, output_tokens=100, total_tokens=200),
            cost_usd=Decimal(cost),
            latency_ms=200,
            finish_reason=FinishReason.STOP,
        )

    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock(side_effect=[make(c) for c in costs])
    fake_decision = MagicMock()
    fake_decision.provider = "cloud"
    fake_decision.model = "cloud/expensive"
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


@pytest.fixture(autouse=True)
def _hermetic_home(monkeypatch, tmp_path) -> None:
    """The profile store and plugin dir resolve to tmp_path, never the
    developer's real $NVH_HOME."""
    monkeypatch.setenv("NVH_HOME", str(tmp_path))


@pytest.mark.asyncio
async def test_default_wizard_profile_has_no_ceiling(monkeypatch) -> None:
    """No ceiling = no abort even on a multi-iteration expensive run."""
    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")
    engine = _engine_with_costs(["0.20", "0.20", "0.20"])
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY),
        patch(
            "nvh.integrations.wizard.chat._run_auto_tool",
            new=AsyncMock(return_value={"ok": True, "result": {}, "safety_class": "auto"}),
        ),
    ):
        result = await chat_mod.wizard_chat("loop forever")

    # No ceiling => loop runs up to WIZARD_FOLLOWUP_MAX_ITER (3).
    assert result["iterations"] == chat_mod.WIZARD_FOLLOWUP_MAX_ITER
    assert result["cost_ceiling_hit"] is False
    assert result["cost_ceiling_usd"] is None


@pytest.mark.asyncio
async def test_profile_ceiling_aborts_follow_up_loop(monkeypatch, tmp_path) -> None:
    """A profile with a low ceiling cuts the loop short the moment the
    accumulated cost crosses it. We use the built-in researcher's $0.05
    ceiling against a completion that already costs $0.10."""
    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")
    engine = _engine_with_costs(["0.10", "0.10", "0.10"])
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY),
        patch(
            "nvh.integrations.wizard.chat._run_auto_tool",
            new=AsyncMock(return_value={"ok": True, "result": {}, "safety_class": "auto"}),
        ),
    ):
        result = await chat_mod.wizard_chat(
            "deep research question",
            profile="researcher",
            home_dir=tmp_path,
        )

    # First completion costs $0.10, exceeds the $0.05 ceiling → loop stops
    # after iteration 1 even though the model emitted a tool call.
    assert result["iterations"] == 1
    assert result["cost_ceiling_hit"] is True
    assert result["cost_ceiling_usd"] == 0.05
    assert result["cost_usd"] >= 0.05
    # No auto-class tool ran: the loop aborted before that step, and the
    # only recorded result is the whitelist refusal (`researcher` may not
    # call `refresh_models`), never an execution — and it is neither offered
    # to the UI as a confirm card nor reported as merely deferred.
    assert [(r["name"], r["result"]["not_allowed"]) for r in result["tool_results"]] == [
        ("refresh_models", True),
    ]
    assert result["tool_calls"] == []
    assert result["deferred_tool_calls"] == []


@pytest.mark.asyncio
async def test_profile_ceiling_with_tiny_cost_lets_loop_complete(monkeypatch, tmp_path) -> None:
    """If a profile has a ceiling but the per-iteration cost stays well
    below it, the loop runs to its natural max."""
    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")
    engine = _engine_with_costs(["0.001", "0.001", "0.001"])
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY),
        patch(
            "nvh.integrations.wizard.chat._run_auto_tool",
            new=AsyncMock(return_value={"ok": True, "result": {}, "safety_class": "auto"}),
        ),
    ):
        result = await chat_mod.wizard_chat(
            "small task",
            profile="researcher",
            home_dir=tmp_path,
        )

    assert result["cost_ceiling_hit"] is False
    assert result["iterations"] == chat_mod.WIZARD_FOLLOWUP_MAX_ITER


@pytest.mark.asyncio
async def test_user_profile_can_set_its_own_ceiling(monkeypatch, tmp_path) -> None:
    """A custom user profile with max_cost_usd_per_turn=0.02 enforces that
    just like a built-in's ceiling does."""
    from nvh.integrations.wizard import chat as chat_mod
    from nvh.integrations.wizard.profiles import (
        AgentProfile,
        save_user_profile,
    )

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")
    save_user_profile(
        AgentProfile(
            name="cheap",
            title="Cheap",
            description="",
            system_prompt="",
            max_cost_usd_per_turn=0.02,
        ),
        home_dir=tmp_path,
    )

    engine = _engine_with_costs(["0.03", "0.03"])
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY),
        patch(
            "nvh.integrations.wizard.chat._run_auto_tool",
            new=AsyncMock(return_value={"ok": True, "result": {}, "safety_class": "auto"}),
        ),
    ):
        result = await chat_mod.wizard_chat(
            "anything",
            profile="cheap",
            home_dir=tmp_path,
        )

    assert result["cost_ceiling_hit"] is True
    assert result["cost_ceiling_usd"] == 0.02
    assert result["iterations"] == 1
    # The auto-class call the ceiling stopped is reported as deferred with the
    # reason — not handed to the UI as a confirm card it would auto-run.
    assert result["tool_calls"] == []
    assert result["deferred_tool_calls"] == [
        {"name": "refresh_models", "arguments": {}, "reason": chat_mod.DEFER_COST_CEILING},
    ]
    assert result["tool_results"] == []


@pytest.mark.asyncio
async def test_zero_or_negative_ceiling_treated_as_no_limit(monkeypatch, tmp_path) -> None:
    """Edge case: ceiling 0 or negative shouldn't accidentally fire on every
    turn — that would brick the agent."""
    from nvh.integrations.wizard import chat as chat_mod
    from nvh.integrations.wizard.profiles import (
        AgentProfile,
        save_user_profile,
    )

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")
    save_user_profile(
        AgentProfile(
            name="zero",
            title="Zero",
            description="",
            system_prompt="",
            max_cost_usd_per_turn=0.0,
        ),
        home_dir=tmp_path,
    )

    engine = _engine_with_costs(["0.05", "0.05", "0.05"])
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY),
        patch(
            "nvh.integrations.wizard.chat._run_auto_tool",
            new=AsyncMock(return_value={"ok": True, "result": {}, "safety_class": "auto"}),
        ),
    ):
        result = await chat_mod.wizard_chat(
            "anything",
            profile="zero",
            home_dir=tmp_path,
        )

    assert result["cost_ceiling_hit"] is False
    assert result["iterations"] == chat_mod.WIZARD_FOLLOWUP_MAX_ITER
