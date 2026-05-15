"""Tests for the Wizard context collector + personality + chat."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nvh.integrations.wizard import context as context_module
from nvh.integrations.wizard.context import wizard_context
from nvh.integrations.wizard.personality import (
    WIZARD_PERSONA,
    _format_context_block,
    build_system_prompt,
)


def test_wizard_context_never_raises_when_all_helpers_fail() -> None:
    """The collector is the single source of truth for downstream Wizard
    surfaces — it must always return a dict, never propagate an exception."""

    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    with patch.object(context_module, "_safe_call", side_effect=lambda label, fn, *a, **kw: None):
        ctx = wizard_context()

    assert isinstance(ctx, dict)
    for key in ("gpu", "storage", "providers", "ollama_models", "recent_jobs", "receipts", "vault"):
        assert key in ctx


def test_wizard_context_shape_has_all_required_keys() -> None:
    """Stable shape — every Wizard surface depends on these top-level keys."""
    ctx = wizard_context()
    for key in ("gpu", "storage", "providers", "ollama_models", "recent_jobs", "receipts", "vault"):
        assert key in ctx, f"missing key: {key}"


def test_personality_persona_block_is_stable() -> None:
    """The persona constant should mention the product name + 'AI Wizard'
    role so future edits don't accidentally drop the brand from the prompt."""
    assert "AI Wizard" in WIZARD_PERSONA
    assert "nvHive" in WIZARD_PERSONA
    assert "rootless" in WIZARD_PERSONA or "cloud" in WIZARD_PERSONA.lower()


def test_format_context_block_compresses_to_json() -> None:
    """Live state block should be valid JSON that the model can parse."""
    import json

    snapshot = {
        "gpu": {"detected": True, "primary": {"name": "RTX 5090", "vram_gb": 32}},
        "storage": {"available": True, "home": "/mnt/persist/nvhive", "free_gb": 412.5, "ok": True},
        "providers": [{"name": "groq", "healthy": True}],
        "ollama_models": [{"name": "gemma3:4b"}],
        "recent_jobs": [],
        "receipts": {"count": 5, "unhealthy": 0},
        "vault": {"initialized": True, "memory_files": 12},
    }
    block = _format_context_block(snapshot)
    parsed = json.loads(block)
    assert parsed["gpu"]["name"] == "RTX 5090"
    assert parsed["storage"]["home"] == "/mnt/persist/nvhive"
    assert parsed["providers_enabled"] == ["groq"]


def test_format_context_block_omits_empty_fields() -> None:
    """Empty / falsy sub-fields are dropped so the prompt stays small."""
    snapshot = {
        "gpu": {"detected": False, "summary": "No GPU"},
        "storage": {"available": False},
        "providers": [],
        "ollama_models": [],
        "recent_jobs": [],
        "receipts": {"count": 0, "unhealthy": 0},
        "vault": {"initialized": False},
    }
    block = _format_context_block(snapshot)
    assert "storage" not in block  # Dropped — not available
    assert "providers_enabled" in block  # Always present, even if empty list
    assert "unhealthy_receipts" not in block  # 0 unhealthy → omitted
    assert "running_jobs" not in block
    assert "recent_failed_jobs" not in block


def test_build_system_prompt_includes_persona_and_live_block() -> None:
    snapshot = {
        "gpu": {"detected": True, "primary": {"name": "RTX 4090", "vram_gb": 24}},
        "storage": {"available": True, "home": "/persist/nvh", "ok": True},
        "providers": [],
        "ollama_models": [],
        "recent_jobs": [],
        "receipts": {},
        "vault": {},
    }
    prompt = build_system_prompt(snapshot)
    assert "AI Wizard" in prompt
    assert "RTX 4090" in prompt
    assert "Live workspace state" in prompt


def test_build_system_prompt_handles_empty_context() -> None:
    prompt = build_system_prompt({})
    assert "AI Wizard" in prompt
    assert "live state unavailable" in prompt


@pytest.mark.asyncio
async def test_wizard_chat_falls_back_to_deterministic_when_engine_missing() -> None:
    """If no engine is registered, chat must still answer — the deterministic
    setup_assistant_reply path is the offline-safe contract."""
    from nvh.integrations.wizard import chat as chat_mod

    with (
        patch("nvh.api.server.get_engine", return_value=None),
        patch("nvh.integrations.wizard.context.wizard_context", return_value={
            "gpu": {"detected": False},
            "storage": {"available": False},
            "providers": [],
            "ollama_models": [],
            "recent_jobs": [],
            "receipts": {},
            "vault": {},
        }),
        patch(
            "nvh.integrations.wizard.setup_agent.setup_assistant_reply",
            return_value={"answer": "Offline fallback fired", "actions": []},
        ),
    ):
        result = await chat_mod.wizard_chat("what can you do?")

    assert result["mode"] == "deterministic"
    assert "Offline fallback fired" in result["answer"]
    assert "context" in result


@pytest.mark.asyncio
async def test_wizard_chat_uses_llm_path_when_engine_available() -> None:
    """When the engine is initialized, the LLM path runs and we surface the
    provider/model the router picked."""
    from decimal import Decimal

    from nvh.integrations.wizard import chat as chat_mod
    from nvh.providers.base import CompletionResponse, FinishReason, Usage

    fake_response = CompletionResponse(
        content="Welcome back — your workspace looks healthy.",
        model="ollama/gemma3:4b",
        provider="ollama",
        usage=Usage(input_tokens=10, output_tokens=20, total_tokens=30),
        cost_usd=Decimal("0"),
        latency_ms=120,
        finish_reason=FinishReason.STOP,
    )

    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock(return_value=fake_response)

    fake_decision = MagicMock()
    fake_decision.provider = "ollama"
    fake_decision.model = "ollama/gemma3:4b"

    fake_engine = MagicMock()
    fake_engine.initialize = AsyncMock()
    fake_engine._check_budget = AsyncMock()
    fake_engine._log_query = AsyncMock()
    fake_engine.router.route = MagicMock(return_value=fake_decision)
    fake_engine.registry.get = MagicMock(return_value=fake_provider)
    fake_engine.config.defaults.temperature = 0.7
    fake_engine.config.defaults.max_tokens = 256

    with (
        patch("nvh.api.server.get_engine", return_value=fake_engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value={
            "gpu": {"detected": True, "primary": {"name": "RTX 5090"}},
            "storage": {"available": True, "home": "/mnt/persist"},
            "providers": [],
            "ollama_models": [],
            "recent_jobs": [],
            "receipts": {},
            "vault": {},
        }),
    ):
        result = await chat_mod.wizard_chat("hi")

    assert result["mode"] == "llm"
    assert result["used_provider"] == "ollama"
    assert result["used_model"] == "ollama/gemma3:4b"
    assert "Welcome back" in result["answer"]


# ────────────────────────────────────────────────────────────────────────────
# Wizard-5: conversational tool-result follow-up loop
# ────────────────────────────────────────────────────────────────────────────


def _make_fake_engine(complete_returns: list) -> MagicMock:
    """Build an engine whose provider.complete returns a sequence of responses.

    Each call to ``complete`` pops the next response off ``complete_returns``,
    which lets us simulate a multi-iteration LLM loop where iteration 1 emits
    a TOOL_CALL and iteration 2 reacts to the tool result.
    """
    from decimal import Decimal

    from nvh.providers.base import CompletionResponse, FinishReason, Usage

    responses = [
        CompletionResponse(
            content=content,
            model="ollama/gemma3:4b",
            provider="ollama",
            usage=Usage(input_tokens=10, output_tokens=20, total_tokens=30),
            cost_usd=Decimal("0"),
            latency_ms=100,
            finish_reason=FinishReason.STOP,
        )
        for content in complete_returns
    ]

    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock(side_effect=responses)

    fake_decision = MagicMock()
    fake_decision.provider = "ollama"
    fake_decision.model = "ollama/gemma3:4b"

    fake_engine = MagicMock()
    fake_engine.initialize = AsyncMock()
    fake_engine._check_budget = AsyncMock()
    fake_engine._log_query = AsyncMock()
    fake_engine.router.route = MagicMock(return_value=fake_decision)
    fake_engine.registry.get = MagicMock(return_value=fake_provider)
    fake_engine.config.defaults.temperature = 0.7
    fake_engine.config.defaults.max_tokens = 256
    return fake_engine


_EMPTY_SNAPSHOT = {
    "gpu": {"detected": False},
    "storage": {"available": False},
    "providers": [],
    "ollama_models": [],
    "recent_jobs": [],
    "receipts": {},
    "vault": {},
}


@pytest.mark.asyncio
async def test_wizard_chat_followup_runs_auto_tool_and_reacts() -> None:
    """An auto-class TOOL_CALL on iter 1 should execute server-side, the
    result should be fed back, and iter 2's plain reply becomes the answer."""
    from nvh.integrations.wizard import chat as chat_mod

    fake_engine = _make_fake_engine([
        'Let me re-check.\nTOOL_CALL: {"name": "refresh_models", "arguments": {}}',
        "Found 3 models — gemma3:4b, qwen2.5:7b, llama3.2:3b.",
    ])

    with (
        patch("nvh.api.server.get_engine", return_value=fake_engine),
        patch(
            "nvh.integrations.wizard.context.wizard_context",
            return_value=_EMPTY_SNAPSHOT,
        ),
        patch(
            "nvh.integrations.wizard.chat._run_auto_tool",
            new=AsyncMock(return_value={"ok": True, "result": {"count": 3}, "safety_class": "auto"}),
        ),
    ):
        result = await chat_mod.wizard_chat("how many models are installed?")

    assert result["mode"] == "llm"
    assert result["iterations"] == 2
    assert "Found 3 models" in result["answer"]
    # TOOL_CALL marker stripped from final user-facing answer
    assert "TOOL_CALL" not in result["answer"]
    # Auto-class tool result captured for the UI
    assert len(result["tool_results"]) == 1
    assert result["tool_results"][0]["name"] == "refresh_models"
    assert result["tool_results"][0]["result"]["ok"] is True
    # No confirm-class calls pending
    assert result["tool_calls"] == []


@pytest.mark.asyncio
async def test_wizard_chat_followup_defers_confirm_class_to_ui() -> None:
    """A confirm-class TOOL_CALL must NOT auto-execute; it must be surfaced
    to the UI via ``tool_calls`` and the loop should stop after one iteration."""
    from nvh.integrations.wizard import chat as chat_mod

    fake_engine = _make_fake_engine([
        'I can save that key.\nTOOL_CALL: {"name": "save_provider_key", "arguments": {"provider": "groq", "api_key": "gsk_x"}}',
    ])

    with (
        patch("nvh.api.server.get_engine", return_value=fake_engine),
        patch(
            "nvh.integrations.wizard.context.wizard_context",
            return_value=_EMPTY_SNAPSHOT,
        ),
        patch(
            "nvh.integrations.wizard.chat._run_auto_tool",
            new=AsyncMock(return_value={
                "ok": False,
                "deferred_to_user": True,
                "safety_class": "confirm",
                "error": "confirm-class — surfaced to user instead of auto-running",
            }),
        ),
    ):
        result = await chat_mod.wizard_chat("save my groq key")

    assert result["mode"] == "llm"
    # Only one LLM round-trip since nothing auto-ran.
    assert result["iterations"] == 1
    # The confirm-class call is surfaced for the UI to render a button.
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0]["name"] == "save_provider_key"
    assert result["tool_calls"][0]["arguments"]["provider"] == "groq"
    # And it didn't auto-run.
    assert result["tool_results"] == []


@pytest.mark.asyncio
async def test_wizard_chat_followup_caps_at_max_iter() -> None:
    """If the model keeps emitting TOOL_CALL on every iteration, the loop
    must terminate at ``WIZARD_FOLLOWUP_MAX_ITER`` rather than spinning."""
    from nvh.integrations.wizard import chat as chat_mod

    # Always emit a tool call so the loop never naturally exits.
    looping = 'Working on it.\nTOOL_CALL: {"name": "refresh_models", "arguments": {}}'
    fake_engine = _make_fake_engine([looping, looping, looping])

    with (
        patch("nvh.api.server.get_engine", return_value=fake_engine),
        patch(
            "nvh.integrations.wizard.context.wizard_context",
            return_value=_EMPTY_SNAPSHOT,
        ),
        patch(
            "nvh.integrations.wizard.chat._run_auto_tool",
            new=AsyncMock(return_value={"ok": True, "result": {}, "safety_class": "auto"}),
        ),
    ):
        result = await chat_mod.wizard_chat("loop forever")

    assert result["iterations"] == chat_mod.WIZARD_FOLLOWUP_MAX_ITER
    # The cap prevents runaway tool execution, but the auto-class results
    # we did collect should still be returned.
    assert len(result["tool_results"]) == chat_mod.WIZARD_FOLLOWUP_MAX_ITER


@pytest.mark.asyncio
async def test_wizard_chat_enable_followup_false_preserves_oneshot_behavior() -> None:
    """``enable_followup=False`` must short-circuit the loop after one call,
    matching the pre-Wizard-5 single-completion contract that older tests
    and callers rely on."""
    from nvh.integrations.wizard import chat as chat_mod

    fake_engine = _make_fake_engine([
        'TOOL_CALL: {"name": "refresh_models", "arguments": {}}',
    ])

    run_auto = AsyncMock()

    with (
        patch("nvh.api.server.get_engine", return_value=fake_engine),
        patch(
            "nvh.integrations.wizard.context.wizard_context",
            return_value=_EMPTY_SNAPSHOT,
        ),
        patch("nvh.integrations.wizard.chat._run_auto_tool", new=run_auto),
    ):
        result = await chat_mod.wizard_chat("hi", enable_followup=False)

    assert result["iterations"] == 1
    # Nothing executed server-side when follow-up is off — the call is
    # surfaced to the caller exactly like before.
    run_auto.assert_not_called()
    assert result["tool_results"] == []
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0]["name"] == "refresh_models"
