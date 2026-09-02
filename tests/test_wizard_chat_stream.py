"""Tests for the streaming Wizard chat path.

We mock provider.stream to yield deterministic chunks and verify:

  - Plain answers stream as token events end-to-end
  - TOOL_CALL: lines are filtered from the live stream but still parsed
    out of the accumulated text and emitted as tool_call / tool_result events
  - Confirm-class tool calls land in a confirm_required event without
    auto-running
  - Engine-missing fallback yields a single error event with a fallback string
  - The done event carries the right final answer + iteration count

Provider streams are mocked at the function level (``provider.stream``)
so we don't depend on any real engine or LLM.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_chunks(texts: list[str]) -> AsyncIterator[MagicMock]:
    """Return an async iterator that yields chunks with .delta == each text."""
    async def _gen():
        for t in texts:
            yield MagicMock(delta=t)
    return _gen()


def _fake_engine(stream_responses: list[list[str]]) -> MagicMock:
    """Build an engine whose provider.stream returns chunked text per call.

    Each entry in ``stream_responses`` is a list of token deltas for one
    iteration of the follow-up loop.
    """
    call_count = {"i": 0}

    def stream_fn(**kwargs):
        i = call_count["i"]
        call_count["i"] += 1
        if i >= len(stream_responses):
            return _make_chunks([""])
        return _make_chunks(stream_responses[i])

    fake_provider = MagicMock()
    fake_provider.stream = MagicMock(side_effect=stream_fn)

    fake_decision = MagicMock()
    fake_decision.provider = "ollama"
    fake_decision.model = "ollama/gemma3:4b"

    fake_engine = MagicMock()
    fake_engine.initialize = AsyncMock()
    fake_engine._check_budget = AsyncMock()
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


@pytest.fixture(autouse=True)
def _hermetic_home(monkeypatch, tmp_path) -> None:
    """The concierge's profile catalog and the plugin dir resolve to tmp_path,
    never the developer's real $NVH_HOME; no vault recall."""
    monkeypatch.setenv("NVH_HOME", str(tmp_path))
    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")


@pytest.mark.asyncio
async def test_stream_plain_answer_yields_token_events_and_done() -> None:
    """A response with no tool calls should stream as token events and end
    in a done event carrying the full answer."""
    from nvh.integrations.wizard import chat as chat_mod

    engine = _fake_engine([["Hello ", "world", "!"]])

    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch(
            "nvh.integrations.wizard.context.wizard_context",
            return_value=_EMPTY_SNAPSHOT,
        ),
    ):
        events = [e async for e in chat_mod.wizard_chat_stream("hi")]

    types = [e["type"] for e in events]
    assert types[0] == "iteration"
    assert "token" in types
    assert types[-1] == "done"
    # Concatenated token text matches the deltas
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert "Hello" in text and "world" in text
    done = next(e for e in events if e["type"] == "done")
    assert done["iterations"] == 1
    assert done["tool_results"] == []
    assert done["tool_calls"] == [] and done["deferred_tool_calls"] == []


@pytest.mark.asyncio
async def test_stream_filters_tool_call_lines_from_user_stream() -> None:
    """TOOL_CALL: lines must NOT appear in the visible token stream, but
    must still be picked up and surface as tool_call/tool_result events."""
    from nvh.integrations.wizard import chat as chat_mod

    # Iteration 1: emits a tool call. Iteration 2: emits the final answer.
    engine = _fake_engine([
        [
            "Let me re-check.\n",
            'TOOL_CALL: {"name": "refresh_models", "arguments": {}}\n',
        ],
        ["Found 3 models."],
    ])

    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch(
            "nvh.integrations.wizard.context.wizard_context",
            return_value=_EMPTY_SNAPSHOT,
        ),
        patch(
            "nvh.integrations.wizard.chat._run_auto_tool",
            new=AsyncMock(return_value={"ok": True, "result": {"count": 3}, "safety_class": "auto"}),
        ),
    ):
        events = [e async for e in chat_mod.wizard_chat_stream("how many?")]

    visible_text = "".join(e["text"] for e in events if e["type"] == "token")
    # The TOOL_CALL line should be invisible to the user.
    assert "TOOL_CALL" not in visible_text
    # The user's stream still shows the natural-language prefix + final answer.
    assert "Let me re-check" in visible_text
    assert "Found 3 models" in visible_text
    # Tool events fire in order, with the matching name.
    tool_calls = [e for e in events if e["type"] == "tool_call"]
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["name"] == "refresh_models"
    assert len(tool_results) == 1
    assert tool_results[0]["result"]["ok"] is True
    done = next(e for e in events if e["type"] == "done")
    assert done["iterations"] == 2


@pytest.mark.asyncio
async def test_stream_emits_confirm_required_for_confirm_class_tools() -> None:
    """A confirm-class TOOL_CALL must surface as confirm_required and NOT
    auto-execute on the server."""
    from nvh.integrations.wizard import chat as chat_mod

    engine = _fake_engine([
        ['I can save that key.\n',
         'TOOL_CALL: {"name": "save_provider_key", "arguments": {"provider": "groq", "api_key": "gsk_x"}}\n'],
    ])

    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch(
            "nvh.integrations.wizard.context.wizard_context",
            return_value=_EMPTY_SNAPSHOT,
        ),
        patch(
            "nvh.integrations.wizard.chat._run_auto_tool",
            new=AsyncMock(return_value={
                "ok": False, "deferred_to_user": True, "safety_class": "confirm",
                "error": "confirm-class — surfaced to user instead of auto-running",
            }),
        ),
    ):
        events = [e async for e in chat_mod.wizard_chat_stream("save my key")]

    confirm_events = [e for e in events if e["type"] == "confirm_required"]
    assert len(confirm_events) == 1
    assert confirm_events[0]["tool_calls"][0]["name"] == "save_provider_key"
    # No tool_result events — we did NOT auto-execute.
    assert not [e for e in events if e["type"] == "tool_result"]


@pytest.mark.asyncio
async def test_stream_falls_back_when_engine_missing() -> None:
    """No engine → single error event with a string fallback answer."""
    from nvh.integrations.wizard import chat as chat_mod

    with (
        patch("nvh.api.server.get_engine", return_value=None),
        patch(
            "nvh.integrations.wizard.context.wizard_context",
            return_value=_EMPTY_SNAPSHOT,
        ),
        patch(
            "nvh.integrations.wizard.setup_agent.setup_assistant_reply",
            return_value={"answer": "Offline fallback fired", "actions": []},
        ),
    ):
        events = [e async for e in chat_mod.wizard_chat_stream("anything")]

    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "Offline fallback fired" in events[0]["fallback"]
