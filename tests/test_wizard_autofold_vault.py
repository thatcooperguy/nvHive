"""Tests for the auto-fold top-vault-chunk feature (Tier 1, feature #3)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _fake_engine_capturing_prompt() -> tuple[MagicMock, list[str]]:
    """Build an engine that captures the system_prompt arg of each complete()."""
    from nvh.providers.base import CompletionResponse, FinishReason, Usage

    captured_prompts: list[str] = []

    async def fake_complete(**kwargs):
        captured_prompts.append(kwargs.get("system_prompt", ""))
        return CompletionResponse(
            content="ok",
            model="ollama/x",
            provider="ollama",
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            cost_usd=Decimal("0"),
            latency_ms=10,
            finish_reason=FinishReason.STOP,
        )

    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock(side_effect=fake_complete)
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
    return fake_engine, captured_prompts


_EMPTY_SNAPSHOT = {
    "gpu": {"detected": False},
    "storage": {"available": False},
    "providers": [],
    "ollama_models": [],
    "recent_jobs": [],
    "receipts": {},
    "vault": {},
}

_HIGH_SCORE_VAULT_HIT = {
    "ok": True,
    "auto_indexed": False,
    "collection": "vault",
    "chunks": [
        {
            "source": "/persist/vault/notes.md",
            "chunk_index": 0,
            "text": "Setup checklist: install Ollama, pull gemma3:4b, configure NVH_HOME mount.",
            "score": 0.92,
        },
    ],
}

_LOW_SCORE_VAULT_HIT = {
    "ok": True,
    "auto_indexed": False,
    "collection": "vault",
    "chunks": [
        {"source": "/persist/vault/notes.md", "chunk_index": 0, "text": "irrelevant", "score": 0.3},
    ],
}


@pytest.mark.asyncio
async def test_autofold_appends_vault_chunk_when_score_above_threshold(monkeypatch) -> None:
    """High-score chunk lands in the system prompt under the Relevant note block."""
    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "1")
    engine, prompts = _fake_engine_capturing_prompt()
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch(
            "nvh.integrations.wizard.context.wizard_context",
            return_value=_EMPTY_SNAPSHOT,
        ),
        patch(
            "nvh.integrations.rag.ask_vault",
            new=AsyncMock(return_value=_HIGH_SCORE_VAULT_HIT),
        ),
    ):
        await chat_mod.wizard_chat("how do I set up my workspace from scratch?")

    assert prompts, "engine.complete was not called"
    final = prompts[-1]
    assert "Relevant note from your vault" in final
    assert "notes.md" in final
    assert "Setup checklist" in final


@pytest.mark.asyncio
async def test_autofold_skipped_when_score_below_threshold(monkeypatch) -> None:
    """Low-score hit must NOT pollute the prompt."""
    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "1")
    engine, prompts = _fake_engine_capturing_prompt()
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch(
            "nvh.integrations.wizard.context.wizard_context",
            return_value=_EMPTY_SNAPSHOT,
        ),
        patch(
            "nvh.integrations.rag.ask_vault",
            new=AsyncMock(return_value=_LOW_SCORE_VAULT_HIT),
        ),
    ):
        await chat_mod.wizard_chat("how do I set up my workspace from scratch?")

    assert "Relevant note from your vault" not in prompts[-1]


@pytest.mark.asyncio
async def test_autofold_skipped_when_env_disabled(monkeypatch) -> None:
    """NVH_WIZARD_AUTOFOLD_VAULT=0 disables the feature even on a strong hit."""
    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")
    engine, prompts = _fake_engine_capturing_prompt()
    asked = AsyncMock(return_value=_HIGH_SCORE_VAULT_HIT)
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch(
            "nvh.integrations.wizard.context.wizard_context",
            return_value=_EMPTY_SNAPSHOT,
        ),
        patch("nvh.integrations.rag.ask_vault", new=asked),
    ):
        await chat_mod.wizard_chat("a long enough question to bypass the trivial-length skip")

    assert "Relevant note from your vault" not in prompts[-1]
    asked.assert_not_called()


@pytest.mark.asyncio
async def test_autofold_skipped_when_followup_disabled(monkeypatch) -> None:
    """enable_followup=False (test mode) must short-circuit auto-fold."""
    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "1")
    engine, prompts = _fake_engine_capturing_prompt()
    asked = AsyncMock(return_value=_HIGH_SCORE_VAULT_HIT)
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch(
            "nvh.integrations.wizard.context.wizard_context",
            return_value=_EMPTY_SNAPSHOT,
        ),
        patch("nvh.integrations.rag.ask_vault", new=asked),
    ):
        await chat_mod.wizard_chat(
            "what should I do next on this project?", enable_followup=False,
        )

    asked.assert_not_called()


@pytest.mark.asyncio
async def test_autofold_failure_does_not_break_chat(monkeypatch) -> None:
    """ask_vault raising must NOT propagate — chat returns normally."""
    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "1")
    engine, _ = _fake_engine_capturing_prompt()
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch(
            "nvh.integrations.wizard.context.wizard_context",
            return_value=_EMPTY_SNAPSHOT,
        ),
        patch(
            "nvh.integrations.rag.ask_vault",
            new=AsyncMock(side_effect=RuntimeError("rag store offline")),
        ),
    ):
        result = await chat_mod.wizard_chat("long question about my setup")

    assert result["mode"] == "llm"


@pytest.mark.asyncio
async def test_autofold_skipped_for_trivial_questions(monkeypatch) -> None:
    """Questions shorter than 10 chars never trigger a vault lookup."""
    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "1")
    engine, _ = _fake_engine_capturing_prompt()
    asked = AsyncMock(return_value=_HIGH_SCORE_VAULT_HIT)
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch(
            "nvh.integrations.wizard.context.wizard_context",
            return_value=_EMPTY_SNAPSHOT,
        ),
        patch("nvh.integrations.rag.ask_vault", new=asked),
    ):
        await chat_mod.wizard_chat("hi")

    asked.assert_not_called()
