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
from decimal import Decimal
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
def _no_vault_autofold(monkeypatch) -> None:
    """No vault recall. The throwaway NVH_HOME comes from tests/conftest.py's
    autouse ``_hermetic_home`` (a module fixture of that name would shadow it
    and skip its Path.home / knob patching)."""
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


# ---------------------------------------------------------------------------
# One tool channel per turn — decided once, the system prompt built for it
# ---------------------------------------------------------------------------

_REFRESH = 'TOOL_CALL: {"name": "refresh_models", "arguments": {}}'


def _complete_engine(contents: list[str]) -> MagicMock:
    """The blocking twin of ``_fake_engine``: ``provider.complete`` answers each content in turn."""
    from nvh.providers.base import CompletionResponse, FinishReason, Usage

    engine = _fake_engine([])
    engine._log_query = AsyncMock()
    provider = engine.registry.get.return_value
    provider.complete = AsyncMock(side_effect=[
        CompletionResponse(
            content=c, model="ollama/gemma3:4b", provider="ollama",
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            cost_usd=Decimal("0"), latency_ms=1, finish_reason=FinishReason.STOP,
        )
        for c in contents
    ])
    return engine


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("native", [False, True])
async def test_tool_channel_is_decided_once_per_turn_and_the_prompt_matches(stream: bool, native: bool) -> None:
    """The provider is asked once whether the model takes ``tools=`` — not once
    per iteration — and the one system prompt of the turn is built for that
    answer: the short native variant (no ``TOOL_CALL:`` teaching, schemas as
    ``tools=``) or the text protocol with the full catalogue. Both channels
    are still read, so the text marker in iteration 1 drives iteration 2
    either way."""
    from nvh.integrations.wizard import chat as chat_mod

    engine = (
        _fake_engine([["Checking.\n", _REFRESH + "\n"], ["Done."]])
        if stream else _complete_engine(["Checking.\n" + _REFRESH, "Done."])
    )
    provider = engine.registry.get.return_value
    provider.supports_tools = AsyncMock(return_value=native)

    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY_SNAPSHOT),
        patch(
            "nvh.integrations.wizard.chat._run_auto_tool",
            new=AsyncMock(return_value={"ok": True, "result": {"count": 3}, "safety_class": "auto"}),
        ),
    ):
        if stream:
            events = [e async for e in chat_mod.wizard_chat_stream("refresh my models")]
            done = events[-1]
            assert done["type"] == "done" and done["iterations"] == 2
            assert "Done." in done["answer"]
        else:
            result = await chat_mod.wizard_chat("refresh my models")
            assert result["mode"] == "llm" and result["iterations"] == 2
            assert result["answer"] == "Done."

    calls = (provider.stream if stream else provider.complete).call_args_list
    assert len(calls) == 2
    # Decided once for the turn, for the routed model.
    provider.supports_tools.assert_awaited_once_with("ollama/gemma3:4b")
    prompts = {c.kwargs["system_prompt"] for c in calls}
    assert len(prompts) == 1
    prompt = prompts.pop()
    assert "--- Available actions" in prompt and "needs a terminal:" in prompt and "TOOL_RESULT" in prompt
    if native:
        assert "TOOL_CALL" not in prompt
        assert "auto: " in prompt and "refresh_models" in prompt
        for c in calls:
            assert c.kwargs["tool_choice"] == "auto"
            assert "refresh_models" in {t["function"]["name"] for t in c.kwargs["tools"]}
    else:
        assert "TOOL_CALL: {" in prompt and "• refresh_models [auto]" in prompt
        for c in calls:
            assert "tools" not in c.kwargs and "tool_choice" not in c.kwargs


@pytest.mark.asyncio
async def test_prepare_turn_leaves_the_prompt_to_the_channel_decision(tmp_path) -> None:
    """``_prepare_turn`` fixes the prompt inputs; the prompt itself is built
    once the provider is known, so the two never disagree."""
    from nvh.integrations.wizard import chat as chat_mod

    with patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY_SNAPSHOT):
        turn = await chat_mod._prepare_turn(
            "hi", history=None, home_dir=tmp_path, enable_followup=False,
            profile="wizard", label="test", engine=None,
        )
    assert turn.system_prompt == "" and turn.native is False
    assert turn.tool_schemas and turn.native_tools

    class Yes:
        async def supports_tools(self, model):
            return True

    assert await chat_mod._choose_tool_channel(turn, Yes(), "m") == {"tools": turn.native_tools, "tool_choice": "auto"}
    assert turn.native is True and "TOOL_CALL" not in turn.system_prompt and "TOOL_RESULT" in turn.system_prompt
    assert await chat_mod._choose_tool_channel(turn, MagicMock(), "m") == {}
    assert turn.native is False and "TOOL_CALL: {" in turn.system_prompt


# ── cost_known on the done event (0.46) ─────────────────────────────────────


def _chunk_engine(chunks: list[MagicMock]) -> MagicMock:
    """An engine whose provider.stream yields exactly ``chunks`` once."""
    async def _gen():
        for c in chunks:
            yield c

    engine = _fake_engine([])
    engine.registry.get.return_value.stream = MagicMock(return_value=_gen())
    return engine


@pytest.mark.asyncio
async def test_stream_done_carries_the_catalogs_cost_known_for_the_routed_model() -> None:
    """``done.cost_known`` is the catalog row's verdict on the model the turn
    used; with no chunk naming a model that is the router's pick. False
    means the $0 beside it is a placeholder the UI shows as "cost unknown"."""
    from nvh.integrations.wizard import chat as chat_mod
    from nvh.providers.base import ModelInfo

    for known in (True, False):
        engine = _fake_engine([["Hello ", "world"]])
        engine.registry.get_model_info = MagicMock(
            return_value=ModelInfo(model_id="ollama/gemma3:4b", provider="ollama", cost_known=known),
        )
        with (
            patch("nvh.api.server.get_engine", return_value=engine),
            patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY_SNAPSHOT),
        ):
            events = [e async for e in chat_mod.wizard_chat_stream("hi")]
        done = events[-1]
        assert done["type"] == "done"
        assert done["cost_usd"] == 0.0
        assert done["cost_known"] is known
        engine.registry.get_model_info.assert_called_with("ollama/gemma3:4b")


@pytest.mark.asyncio
async def test_stream_done_looks_up_the_model_the_final_chunk_named() -> None:
    """A provider that names the model it actually served on its chunks is
    looked up by that spelling, not the router's; a non-str ``model`` (test
    doubles) is ignored."""
    from decimal import Decimal

    from nvh.integrations.wizard import chat as chat_mod
    from nvh.providers.base import Usage

    final = MagicMock(delta="", model="ollama/served:1b", cost_usd=Decimal("0"),
                      usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5))
    engine = _chunk_engine([MagicMock(delta="Hi"), final])
    engine.registry.get_model_info = MagicMock(return_value=None)  # no row
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY_SNAPSHOT),
    ):
        events = [e async for e in chat_mod.wizard_chat_stream("hi")]
    done = events[-1]
    assert done["type"] == "done"
    # The served spelling first; the router's pick is asked only because it had no row.
    engine.registry.get_model_info.assert_any_call("ollama/served:1b")
    assert [c.args[0] for c in engine.registry.get_model_info.call_args_list][:2] == ["ollama/served:1b", "ollama/gemma3:4b"]
    # No row, but ollama is a zero_cost provider: a known $0.
    assert done["cost_usd"] == 0.0 and done["cost_known"] is True
    assert done["input_tokens"] == 3 and done["output_tokens"] == 2


@pytest.mark.asyncio
async def test_stream_done_unmetered_zero_on_a_paid_model_is_not_known() -> None:
    """A priced (paid) row, $0 and no usage on any chunk: the provider did not
    meter the turn, so the $0 was billed upstream and is unknown -- the old
    ``costUsd > 0`` footer guard's promise, kept. The same row with tokens
    is a known $0."""
    from decimal import Decimal

    from nvh.integrations.wizard import chat as chat_mod
    from nvh.providers.base import ModelInfo, Usage

    paid = ModelInfo(
        model_id="paid/x", provider="openai", cost_known=True,
        input_cost_per_1m_tokens=Decimal("2.5"), output_cost_per_1m_tokens=Decimal("10"),
    )
    for usage, expected in ((None, False), (Usage(input_tokens=3, output_tokens=2, total_tokens=5), True)):
        final = MagicMock(delta="", model="paid/x", cost_usd=Decimal("0"), usage=usage)
        engine = _chunk_engine([MagicMock(delta="Hi"), final])
        engine.router.route.return_value.provider = "openai"
        engine.router.route.return_value.model = "paid/x"
        engine.registry.get_model_info = MagicMock(return_value=paid)
        with (
            patch("nvh.api.server.get_engine", return_value=engine),
            patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY_SNAPSHOT),
        ):
            events = [e async for e in chat_mod.wizard_chat_stream("hi")]
        done = events[-1]
        assert done["type"] == "done" and done["cost_usd"] == 0.0
        assert done["cost_known"] is expected, (usage, done)


@pytest.mark.asyncio
async def test_stream_done_non_zero_cost_is_known_even_without_a_row() -> None:
    from decimal import Decimal

    from nvh.integrations.wizard import chat as chat_mod

    final = MagicMock(delta="", model="", cost_usd=Decimal("0.004"), usage=None)
    engine = _chunk_engine([MagicMock(delta="Hi"), final])
    engine.router.route.return_value.provider = "openai"
    engine.router.route.return_value.model = "gpt-unlisted"
    engine.registry.get_model_info = MagicMock(return_value=None)
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY_SNAPSHOT),
    ):
        events = [e async for e in chat_mod.wizard_chat_stream("hi")]
    done = events[-1]
    assert done["type"] == "done"
    assert done["cost_usd"] == pytest.approx(0.004) and done["cost_known"] is True
