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
