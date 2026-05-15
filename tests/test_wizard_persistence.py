"""Tests for Wizard conversation persistence (Tier 1, feature #1).

We mock the conversations repo at the function level. The actual storage
backend is exercised by other tests; here we just verify the wiring:

  - When a conversation_id is provided, both user + assistant turns are saved
  - When none is provided, no repo calls are made
  - When the repo raises, the chat still returns successfully
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _fake_engine_for_persistence() -> MagicMock:
    """Build an engine whose one completion returns a plain text reply."""
    from nvh.providers.base import CompletionResponse, FinishReason, Usage

    fake_response = CompletionResponse(
        content="Looks healthy.",
        model="ollama/gemma3:4b",
        provider="ollama",
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        cost_usd=Decimal("0"),
        latency_ms=100,
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
async def test_wizard_chat_persists_when_conversation_id_provided(monkeypatch) -> None:
    """conversation_id set → both turns written, with provider/model + meta tail."""
    from nvh.integrations.wizard import chat as chat_mod

    add_message = AsyncMock()
    monkeypatch.setattr(
        "nvh.storage.repository.add_message", add_message, raising=False,
    )

    engine = _fake_engine_for_persistence()
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch(
            "nvh.integrations.wizard.context.wizard_context",
            return_value=_EMPTY_SNAPSHOT,
        ),
        # Auto-fold off by default in test mode (enable_followup=False path
        # already guards it, but the default here is True). Disable explicitly
        # so the test's only repo calls come from persistence.
        monkeypatch.context() as mc,
    ):
        mc.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")
        result = await chat_mod.wizard_chat("hi there", conversation_id="conv-123")

    assert result["mode"] == "llm"
    assert add_message.await_count == 2
    user_call = add_message.await_args_list[0].kwargs
    asst_call = add_message.await_args_list[1].kwargs
    assert user_call["conversation_id"] == "conv-123"
    assert user_call["role"] == "user"
    assert user_call["content"] == "hi there"
    assert asst_call["role"] == "assistant"
    assert "Looks healthy" in asst_call["content"]
    # The assistant content carries a metadata tail for the tool trace.
    assert "wizard-meta" in asst_call["content"]


@pytest.mark.asyncio
async def test_wizard_chat_skips_persistence_when_no_conversation_id(monkeypatch) -> None:
    """conversation_id=None must produce zero repo calls."""
    from nvh.integrations.wizard import chat as chat_mod

    add_message = AsyncMock()
    monkeypatch.setattr(
        "nvh.storage.repository.add_message", add_message, raising=False,
    )
    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")

    engine = _fake_engine_for_persistence()
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch(
            "nvh.integrations.wizard.context.wizard_context",
            return_value=_EMPTY_SNAPSHOT,
        ),
    ):
        result = await chat_mod.wizard_chat("hello")

    assert result["mode"] == "llm"
    add_message.assert_not_called()


@pytest.mark.asyncio
async def test_wizard_chat_swallows_persistence_errors(monkeypatch) -> None:
    """Repo raises → chat still returns the LLM answer successfully."""
    from nvh.integrations.wizard import chat as chat_mod

    failing = AsyncMock(side_effect=RuntimeError("db down"))
    monkeypatch.setattr(
        "nvh.storage.repository.add_message", failing, raising=False,
    )
    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")

    engine = _fake_engine_for_persistence()
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch(
            "nvh.integrations.wizard.context.wizard_context",
            return_value=_EMPTY_SNAPSHOT,
        ),
    ):
        result = await chat_mod.wizard_chat("hi", conversation_id="conv-broken")

    assert result["mode"] == "llm"
    assert "Looks healthy" in result["answer"]


@pytest.mark.asyncio
async def test_wizard_chat_persists_deterministic_fallback(monkeypatch) -> None:
    """Deterministic fallback path must also persist when conversation_id set."""
    from nvh.integrations.wizard import chat as chat_mod

    add_message = AsyncMock()
    monkeypatch.setattr(
        "nvh.storage.repository.add_message", add_message, raising=False,
    )
    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")

    with (
        patch("nvh.api.server.get_engine", return_value=None),
        patch(
            "nvh.integrations.wizard.context.wizard_context",
            return_value=_EMPTY_SNAPSHOT,
        ),
        patch(
            "nvh.integrations.wizard.setup_agent.setup_assistant_reply",
            return_value={"answer": "Offline answer", "actions": []},
        ),
    ):
        result = await chat_mod.wizard_chat("hi", conversation_id="conv-offline")

    assert result["mode"] == "deterministic"
    # 2 add_message calls — user + assistant.
    assert add_message.await_count == 2
