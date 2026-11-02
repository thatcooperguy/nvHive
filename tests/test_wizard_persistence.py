"""Tests for Wizard conversation persistence (Tier 1, feature #1).

We mock the conversations repo at the function level. The actual storage
backend is exercised by other tests; here we just verify the wiring:

  - When a conversation_id is provided, both user + assistant turns are saved
  - When none is provided, no repo calls are made
  - When the repo raises, the chat still returns successfully
  - The assistant row carries the turn's meter in its own columns and a
    versioned ``wizard-meta`` tail whose keys are exactly the ones
    ``WIZARD_META_KEYS`` documents (docs/CONFIGURATION.md's ``wizard-meta``
    block is generated from that tuple)
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _fake_engine_for_persistence(cost_usd: Decimal = Decimal("0")) -> MagicMock:
    """Build an engine whose one completion returns a plain text reply."""
    from nvh.providers.base import CompletionResponse, FinishReason, Usage

    fake_response = CompletionResponse(
        content="Looks healthy.",
        model="ollama/gemma3:4b",
        provider="ollama",
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        cost_usd=cost_usd,
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


# ── the wizard-meta tail is versioned and described by WIZARD_META_KEYS ─────


def _meta_tail(content: str) -> dict[str, Any]:
    """The parsed ``wizard-meta`` tail of a persisted assistant row."""
    m = re.search(r"\n\n<!-- wizard-meta: (\{.*\}) -->$", content, re.DOTALL)
    assert m, content
    return json.loads(m.group(1))


# The three key sets the tail can carry: on every tail, on an LLM-answered
# turn, on a deterministic one. A streamed turn adds ``streamed``.
_ALWAYS_KEYS = {
    "source", "v", "used_profile", "profile_reason",
    "iterations", "tool_calls", "tool_results", "deferred_tool_calls",
}
_LLM_KEYS = _ALWAYS_KEYS | {"cost_usd", "cost_known", "latency_ms"}
_DETERMINISTIC_KEYS = _ALWAYS_KEYS | {"mode", "fallback_reason"}
_PRESENCE_PREFIXES = ("LLM-answered turns only", "Deterministic turns only", "Streamed turns only")


def test_wizard_meta_keys_describe_the_tail_once_each() -> None:
    """``WIZARD_META_KEYS`` is what the CONFIGURATION.md generator renders:
    unique keys, a one-line doc each, and the presence condition of every
    conditional key spelled the way the tuple's comment promises."""
    from nvh.integrations.wizard import chat as chat_mod

    keys = [key for key, _ in chat_mod.WIZARD_META_KEYS]
    assert len(keys) == len(set(keys))
    assert set(keys) == _LLM_KEYS | _DETERMINISTIC_KEYS | {"streamed"}
    assert keys[:2] == ["source", "v"]
    docs = dict(chat_mod.WIZARD_META_KEYS)
    for key, doc in docs.items():
        assert isinstance(doc, str) and doc.strip() and "\n" not in doc, key
    for key in _LLM_KEYS - _ALWAYS_KEYS:
        assert docs[key].startswith("LLM-answered turns only"), key
    for key in _DETERMINISTIC_KEYS - _ALWAYS_KEYS:
        assert docs[key].startswith("Deterministic turns only"), key
    assert docs["streamed"].startswith("Streamed turns only")
    for key in _ALWAYS_KEYS:
        assert not docs[key].startswith(_PRESENCE_PREFIXES), key
    assert chat_mod.WIZARD_META_VERSION == 1
    assert f"`{chat_mod.WIZARD_META_VERSION}`" in docs["v"]


@pytest.mark.asyncio
async def test_assistant_row_carries_tokens_cost_and_latency(monkeypatch) -> None:
    """The assistant row's own columns are filled from the turn's meter — the
    tail is no longer the only carrier of cost — and the user row keeps the
    column defaults."""
    from nvh.integrations.wizard import chat as chat_mod

    add_message = AsyncMock()
    monkeypatch.setattr("nvh.storage.repository.add_message", add_message, raising=False)
    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")

    engine = _fake_engine_for_persistence(cost_usd=Decimal("0.0015"))
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY_SNAPSHOT),
    ):
        result = await chat_mod.wizard_chat("hi there", conversation_id="conv-cols")

    assert result["mode"] == "llm"
    user_call, asst_call = (c.kwargs for c in add_message.await_args_list)
    assert asst_call["role"] == "assistant"
    assert asst_call["input_tokens"] == 10 and asst_call["output_tokens"] == 5
    assert isinstance(asst_call["cost_usd"], Decimal) and asst_call["cost_usd"] == Decimal("0.0015")
    assert asst_call["latency_ms"] == 100
    assert asst_call["provider"] == "ollama" and asst_call["model"] == "ollama/gemma3:4b"
    for column in ("input_tokens", "output_tokens", "cost_usd", "latency_ms"):
        assert column not in user_call, column
    # The tail agrees with the columns.
    meta = _meta_tail(asst_call["content"])
    assert meta["cost_usd"] == pytest.approx(0.0015) and meta["latency_ms"] == 100


@pytest.mark.asyncio
async def test_meta_tail_is_versioned_and_described_by_wizard_meta_keys(monkeypatch) -> None:
    """Every persisted tail carries ``v: 1`` and exactly the keys
    ``WIZARD_META_KEYS`` documents for its kind of turn — LLM-answered or
    deterministic — so the generated CONFIGURATION.md block is the truth."""
    from nvh.integrations.wizard import chat as chat_mod

    add_message = AsyncMock()
    monkeypatch.setattr("nvh.storage.repository.add_message", add_message, raising=False)
    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")

    with (
        patch("nvh.api.server.get_engine", return_value=_fake_engine_for_persistence()),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY_SNAPSHOT),
    ):
        await chat_mod.wizard_chat("hi", conversation_id="conv-llm")
    llm_meta = _meta_tail(add_message.await_args_list[1].kwargs["content"])

    add_message.reset_mock()
    with (
        patch("nvh.api.server.get_engine", return_value=None),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY_SNAPSHOT),
        patch(
            "nvh.integrations.wizard.setup_agent.setup_assistant_reply",
            return_value={"answer": "Offline answer", "actions": []},
        ),
    ):
        await chat_mod.wizard_chat("hi", conversation_id="conv-det")
    det_meta = _meta_tail(add_message.await_args_list[1].kwargs["content"])

    documented = {key for key, _ in chat_mod.WIZARD_META_KEYS}
    for meta in (llm_meta, det_meta):
        assert meta["v"] == 1 == chat_mod.WIZARD_META_VERSION
        assert meta["source"] == "wizard"
        assert set(meta) <= documented
        assert list(meta)[:2] == ["source", "v"]
    assert set(llm_meta) == _LLM_KEYS
    assert set(det_meta) == _DETERMINISTIC_KEYS
    assert det_meta["mode"] == "deterministic"
    assert det_meta["fallback_reason"] == "engine not initialized"


@pytest.mark.asyncio
async def test_persist_wizard_turn_versions_the_tail_and_converts_the_cost(monkeypatch) -> None:
    """The writer owns the format: a metadata dict without ``v`` is written
    with it (the caller's dict untouched), the float meter becomes the
    ``Decimal`` the column takes, and a non-finite cost becomes 0 rather
    than a row that fails to write."""
    from nvh.integrations.wizard import chat as chat_mod

    add_message = AsyncMock()
    monkeypatch.setattr("nvh.storage.repository.add_message", add_message, raising=False)

    metadata = {"source": "wizard", "iterations": 1}
    await chat_mod._persist_wizard_turn(
        "conv-x", user_question="q", assistant_text="a", provider="p", model="m",
        metadata=metadata, input_tokens=3, output_tokens=2, cost_usd=0.004, latency_ms=7,
    )
    assert add_message.await_count == 2
    asst = add_message.await_args_list[1].kwargs
    assert asst["content"].startswith("a\n\n<!-- wizard-meta: ")
    assert _meta_tail(asst["content"]) == {"v": 1, "source": "wizard", "iterations": 1}
    assert metadata == {"source": "wizard", "iterations": 1}
    assert asst["cost_usd"] == Decimal("0.004") and isinstance(asst["cost_usd"], Decimal)
    assert asst["input_tokens"] == 3 and asst["output_tokens"] == 2 and asst["latency_ms"] == 7

    assert chat_mod._decimal_usd(0.0015) == Decimal("0.0015")
    assert chat_mod._decimal_usd(Decimal("1.5")) == Decimal("1.5")
    assert chat_mod._decimal_usd(2) == Decimal("2")
    assert chat_mod._decimal_usd(float("nan")) == Decimal("0")
    assert chat_mod._decimal_usd(float("inf")) == Decimal("0")
    assert chat_mod._decimal_usd(True) == Decimal("0")
    assert chat_mod._decimal_usd(None) == Decimal("0")
    assert chat_mod._decimal_usd(MagicMock()) == Decimal("0")


@pytest.mark.asyncio
async def test_meta_builders_strip_approval_tokens_from_tool_calls(tmp_path) -> None:
    """A surfaced privileged card carries a single-use approval token; neither
    meta builder lets it reach the conversation store, while the card's
    plan and arguments stay. The live envelope keeps the token: the card the
    user clicks needs it."""
    from nvh.integrations.wizard import chat as chat_mod

    with patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY_SNAPSHOT):
        turn = await chat_mod._prepare_turn(
            "hi", history=None, home_dir=tmp_path, enable_followup=False,
            profile="wizard", label="test", engine=None,
        )
    card = {
        "name": "enable_ssh", "arguments": {"setting": "enable_ssh"}, "privileged": True,
        "plan": ["sudo -n systemctl enable --now ssh"],
        "approval_token": "tok-secret", "approval_expires_at": "2026-11-01T00:15:00+00:00",
    }
    stripped = {
        "name": "enable_ssh", "arguments": {"setting": "enable_ssh"}, "privileged": True,
        "plan": ["sudo -n systemctl enable --now ssh"],
    }

    meta = turn.meta(
        iterations=1, tool_calls=[dict(card)], tool_results=[], deferred_tool_calls=[],
        cost_usd=0.0, cost_known=True, latency_ms=1,
    )
    assert meta["tool_calls"] == [stripped] and meta["v"] == 1
    assert set(meta) == _LLM_KEYS

    state = chat_mod._LoopState(pending_confirm=[dict(card)], iterations=2)
    result = chat_mod._deterministic_result(turn, "fallback", "provider went away", state=state)
    det = turn.meta_for(result, streamed=True)
    assert det["tool_calls"] == [stripped] and det["v"] == 1
    assert set(det) == _DETERMINISTIC_KEYS | {"streamed"} and det["streamed"] is True
    assert result["tool_calls"][0]["approval_token"] == "tok-secret"
