"""Tests for the streaming Wizard chat path.

We mock provider.stream to yield deterministic chunks and verify:

  - Plain answers stream as token events end-to-end
  - TOOL_CALL: lines are filtered from the live stream but still parsed
    out of the accumulated text and emitted as tool_call / tool_result events
  - Confirm-class tool calls land in a confirm_required event without
    auto-running
  - Engine-missing fallback yields a single error event with a fallback string
  - The done event carries the right final answer + iteration count
  - The turn reaches the cost ledger (``engine._log_query``,
    ``mode="wizard-chat"``) once, with the meter summed over its iterations,
    and the persisted assistant row carries that meter in its columns

Provider streams are mocked at the function level (``provider.stream``)
so we don't depend on any real engine or LLM.
"""

from __future__ import annotations

import json
import sys
import types
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
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


# ── the stream path writes the ledger row and the row columns (Wave A4) ─────


def _metered_engine(iterations: list[tuple[list[str], int, int, str, str]]) -> MagicMock:
    """Like ``_fake_engine`` but each iteration ends with a final chunk that
    carries ``usage`` / ``cost_usd`` / ``model`` the way a real provider's
    does: ``(deltas, input_tokens, output_tokens, cost_usd, model)``."""
    from nvh.providers.base import Usage

    call_count = {"i": 0}

    def stream_fn(**kwargs):
        texts, in_tok, out_tok, cost, model = iterations[min(call_count["i"], len(iterations) - 1)]
        call_count["i"] += 1

        async def _gen():
            for t in texts:
                yield MagicMock(delta=t)
            yield MagicMock(
                delta="", model=model, cost_usd=Decimal(cost),
                usage=Usage(input_tokens=in_tok, output_tokens=out_tok, total_tokens=in_tok + out_tok),
            )
        return _gen()

    engine = _fake_engine([])
    engine.registry.get.return_value.stream = MagicMock(side_effect=stream_fn)
    engine._log_query = AsyncMock()
    return engine


@pytest.mark.asyncio
async def test_stream_logs_one_ledger_row_with_the_summed_meter() -> None:
    """A streamed turn reaches the cost ledger the way a blocking one does:
    one ``_log_query`` with ``mode="wizard-chat"``, written before ``done``,
    whose response sums the tokens and cost of every iteration and names the
    model the chunks served and the provider the router picked."""
    from nvh.integrations.wizard import chat as chat_mod

    engine = _metered_engine([
        (["Checking.\n", _REFRESH + "\n"], 10, 5, "0.001", "ollama/served:1b"),
        (["Done."], 20, 7, "0.002", "ollama/served:1b"),
    ])
    order: list[str] = []

    async def _log(response, mode):
        order.append("log")

    engine._log_query = AsyncMock(side_effect=_log)
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY_SNAPSHOT),
        patch(
            "nvh.integrations.wizard.chat._run_auto_tool",
            new=AsyncMock(return_value={"ok": True, "result": {"count": 3}, "safety_class": "auto"}),
        ),
    ):
        events = []
        async for event in chat_mod.wizard_chat_stream("refresh my models"):
            events.append(event)
            if event["type"] == "done":
                order.append("done")
    done = events[-1]
    assert done["type"] == "done" and done["iterations"] == 2
    assert done["input_tokens"] == 30 and done["output_tokens"] == 12
    assert done["cost_usd"] == pytest.approx(0.003)
    assert order == ["log", "done"]

    engine._log_query.assert_awaited_once()
    assert engine._log_query.await_args.kwargs == {"mode": "wizard-chat"}
    response = engine._log_query.await_args.args[0]
    assert response.provider == "ollama" and response.model == "ollama/served:1b"
    assert response.usage.input_tokens == 30 and response.usage.output_tokens == 12
    assert response.usage.total_tokens == 42
    assert isinstance(response.cost_usd, Decimal)
    assert float(response.cost_usd) == pytest.approx(0.003)
    assert response.cost_usd == Decimal(str(done["cost_usd"]))
    assert response.latency_ms == done["latency_ms"]


@pytest.mark.asyncio
async def test_stream_done_survives_a_ledger_failure() -> None:
    """The ledger is best-effort on both paths: a ``_log_query`` that raises
    leaves the done event intact, with the meter it would have logged."""
    from nvh.integrations.wizard import chat as chat_mod

    engine = _metered_engine([(["Hello"], 3, 2, "0.004", "ollama/served:1b")])
    engine._log_query = AsyncMock(side_effect=RuntimeError("ledger down"))
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY_SNAPSHOT),
    ):
        events = [e async for e in chat_mod.wizard_chat_stream("hi")]
    done = events[-1]
    assert done["type"] == "done" and "Hello" in done["answer"]
    assert done["cost_usd"] == pytest.approx(0.004)
    assert done["input_tokens"] == 3 and done["output_tokens"] == 2
    engine._log_query.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_persists_the_meter_in_the_assistant_row_and_a_versioned_tail(monkeypatch) -> None:
    """The streamed assistant row carries the summed meter in its own columns
    and a ``v: 1`` tail with exactly the keys ``WIZARD_META_KEYS`` documents
    for a streamed LLM turn."""
    import json
    import re

    from nvh.integrations.wizard import chat as chat_mod

    add_message = AsyncMock()
    monkeypatch.setattr("nvh.storage.repository.add_message", add_message, raising=False)
    engine = _metered_engine([(["Hello"], 3, 2, "0.004", "ollama/served:1b")])
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY_SNAPSHOT),
    ):
        events = [e async for e in chat_mod.wizard_chat_stream("hi", conversation_id="conv-stream")]
    done = events[-1]
    assert done["type"] == "done"
    assert add_message.await_count == 2
    user, asst = (c.kwargs for c in add_message.await_args_list)
    assert user["role"] == "user" and "input_tokens" not in user
    assert asst["role"] == "assistant"
    assert asst["input_tokens"] == 3 and asst["output_tokens"] == 2
    assert asst["cost_usd"] == Decimal("0.004") and asst["latency_ms"] == done["latency_ms"]
    m = re.search(r"\n\n<!-- wizard-meta: (\{.*\}) -->$", asst["content"], re.DOTALL)
    assert m, asst["content"]
    meta = json.loads(m.group(1))
    assert meta["v"] == chat_mod.WIZARD_META_VERSION == 1 and meta["streamed"] is True
    assert set(meta) == {
        "source", "v", "used_profile", "profile_reason", "iterations", "tool_calls", "tool_results",
        "deferred_tool_calls", "cost_usd", "cost_known", "latency_ms", "streamed",
    }
    assert set(meta) <= {key for key, _ in chat_mod.WIZARD_META_KEYS}
    assert meta["cost_usd"] == pytest.approx(0.004) and meta["latency_ms"] == done["latency_ms"]


@pytest.mark.asyncio
async def test_stream_error_path_logs_the_iterations_that_were_metered() -> None:
    """Iteration 1 is metered and asks for a tool; iteration 2 raises. The blocking
    path had already logged iteration 1 by then, so the stream logs it too — right
    before the ``error`` event — instead of dropping the spend from the ledger."""
    from nvh.integrations.wizard import chat as chat_mod

    engine = _metered_engine([(["Checking.\n", _REFRESH + "\n"], 10, 5, "0.02", "ollama/served:1b")])
    first_stream = engine.registry.get.return_value.stream.side_effect
    calls = {"n": 0}

    def stream_fn(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise TimeoutError("iteration 2 timed out")
        return first_stream(**kwargs)

    engine.registry.get.return_value.stream = MagicMock(side_effect=stream_fn)
    order: list[str] = []

    async def _log(response, mode):
        order.append("log")

    engine._log_query = AsyncMock(side_effect=_log)
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY_SNAPSHOT),
        patch(
            "nvh.integrations.wizard.chat._run_auto_tool",
            new=AsyncMock(return_value={"ok": True, "result": {"count": 3}, "safety_class": "auto"}),
        ),
    ):
        events = []
        async for event in chat_mod.wizard_chat_stream("refresh my models"):
            events.append(event)
            if event["type"] == "error":
                order.append("error")

    assert events[-1]["type"] == "error" and "timed out" in events[-1]["error"]
    assert calls["n"] == 2 and order == ["log", "error"]
    engine._log_query.assert_awaited_once()
    assert engine._log_query.await_args.kwargs == {"mode": "wizard-chat"}
    response = engine._log_query.await_args.args[0]
    assert response.provider == "ollama" and response.model == "ollama/served:1b"
    assert (response.usage.input_tokens, response.usage.output_tokens) == (10, 5)
    assert float(response.cost_usd) == pytest.approx(0.02)



# ── 0.46: ask_user, confirm cards with plans, unattended turns ───────────────

_ASK = (
    'TOOL_CALL: {"name": "ask_user", "arguments": '
    '{"question": "Which disk should hold models?", "choices": ["/mnt/nvme0", "/home"]}}\n'
)
_ASK_2 = 'TOOL_CALL: {"name": "ask_user", "arguments": {"question": "And which model size?"}}\n'


async def _events(engine: MagicMock, question: str = "set up models", **kw: Any) -> list[dict[str, Any]]:
    """Every event of one streamed turn against ``engine`` (engine + context patched)."""
    from nvh.integrations.wizard import chat as chat_mod

    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY_SNAPSHOT),
    ):
        return [e async for e in chat_mod.wizard_chat_stream(question, **kw)]


def _fake_imagegen(monkeypatch) -> dict[str, list[dict[str, Any]]]:
    """A stand-in ``nvh.integrations.imagegen``: a finite ceiling plans local, none plans hosted.

    Every name here is made up; nothing is a catalog fact.
    """
    calls: dict[str, list[dict[str, Any]]] = {"plan": [], "render": []}

    class ImageGenerationUnavailable(RuntimeError):  # noqa: N818 — the C1 contract's name
        pass

    def plan_image_backend(*, home_dir=None, cost_ceiling_usd=None, requested="auto"):
        calls["plan"].append({"cost_ceiling_usd": cost_ceiling_usd, "requested": requested})
        if cost_ceiling_usd is not None:
            return {
                "backend": "comfyui", "reason": "", "model": "fake-local", "cost_usd": 0.0, "cost_known": True,
                "host": "127.0.0.1", "port": 8188,
                "summary": "local ComfyUI on 127.0.0.1:8188 · $0 · uses GPU memory while it runs",
            }
        return {
            "backend": "nvidia", "reason": "", "model": "fake-hosted", "cost_usd": None, "cost_known": False,
            "host": None, "port": None, "summary": "NVIDIA-hosted (fake-hosted) · cost unknown",
        }

    async def render_image(prompt, **kwargs):
        calls["render"].append({"prompt": prompt, **kwargs})
        raise AssertionError("a surfaced card never renders in the turn")

    module = types.ModuleType("nvh.integrations.imagegen")
    module.ImageGenerationUnavailable = ImageGenerationUnavailable  # type: ignore[attr-defined]
    module.plan_image_backend = plan_image_backend  # type: ignore[attr-defined]
    module.render_image = render_image  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nvh.integrations.imagegen", module)
    return calls


@pytest.mark.asyncio
async def test_ask_user_surfaces_as_one_card_and_ends_the_turn() -> None:
    """A deferred ``ask_user`` is exactly one ``confirm_required`` card — no ``tool_result``, no plan,
    no token — and the turn stops after one iteration although a second response was scripted."""
    engine = _fake_engine([["I need to know where models go.\n", _ASK], ["never reached"]])
    events = await _events(engine)
    kinds = [e["type"] for e in events]
    assert kinds[-1] == "done" and kinds.count("confirm_required") == 1 and "tool_result" not in kinds
    assert [(e["name"], e["safety_class"]) for e in events if e["type"] == "tool_call"] == [("ask_user", "confirm")]
    done = events[-1]
    assert done["iterations"] == 1 and "never reached" not in done["answer"]
    assert done["tool_calls"] == [{
        "name": "ask_user",
        "arguments": {"question": "Which disk should hold models?", "choices": ["/mnt/nvme0", "/home"]},
    }]
    confirm = next(e for e in events if e["type"] == "confirm_required")
    assert confirm["tool_calls"] == done["tool_calls"]
    assert done["deferred_tool_calls"] == [] and done["tool_results"] == []
    assert engine.registry.get.return_value.stream.call_count == 1


@pytest.mark.asyncio
async def test_two_ask_user_calls_make_one_card_and_one_question_pending_deferral() -> None:
    engine = _fake_engine([["Two things.\n", _ASK, _ASK_2], ["never reached"]])
    events = await _events(engine)
    done = events[-1]
    assert done["type"] == "done" and done["iterations"] == 1
    assert [c["name"] for c in done["tool_calls"]] == ["ask_user"]
    assert done["tool_calls"][0]["arguments"]["question"] == "Which disk should hold models?"
    assert done["deferred_tool_calls"] == [{
        "name": "ask_user", "arguments": {"question": "And which model size?"}, "reason": "question_pending",
    }]
    confirm = next(e for e in events if e["type"] == "confirm_required")
    assert confirm["tool_calls"] == done["tool_calls"]


@pytest.mark.asyncio
async def test_ask_user_plus_an_auto_sibling_is_one_iteration() -> None:
    """The auto sibling runs and its result is recorded, but the question ends the turn: no second
    completion to act on an assumption before the answer arrives."""
    from nvh.integrations.wizard import chat as chat_mod

    real_run = chat_mod._run_auto_tool

    async def run(name, arguments, **kw):
        if name == "refresh_models":
            return {"ok": True, "result": {"count": 3}, "safety_class": "auto"}
        return await real_run(name, arguments, **kw)

    engine = _fake_engine([["Checking.\n", _REFRESH + "\n", _ASK], ["never reached"]])
    with patch("nvh.integrations.wizard.chat._run_auto_tool", new=AsyncMock(side_effect=run)):
        events = await _events(engine)
    done = events[-1]
    assert done["type"] == "done" and done["iterations"] == 1
    assert [e["name"] for e in events if e["type"] == "tool_result"] == ["refresh_models"]
    assert [(e["name"], e["safety_class"]) for e in events if e["type"] == "tool_call"] == [
        ("refresh_models", "auto"), ("ask_user", "confirm"),
    ]
    assert [c["name"] for c in done["tool_calls"]] == ["ask_user"]
    assert [r["name"] for r in done["tool_results"]] == ["refresh_models"]
    assert engine.registry.get.return_value.stream.call_count == 1


@pytest.mark.asyncio
async def test_tool_call_events_carry_the_registry_safety_class_or_unknown() -> None:
    engine = _fake_engine([
        ["Go.\n", _REFRESH + "\n", 'TOOL_CALL: {"name": "no_such_tool", "arguments": {}}\n'],
        ["Done."],
    ])
    with patch(
        "nvh.integrations.wizard.chat._run_auto_tool",
        new=AsyncMock(return_value={"ok": True, "result": {}, "safety_class": "auto"}),
    ):
        events = await _events(engine)
    assert [(e["name"], e["safety_class"]) for e in events if e["type"] == "tool_call"] == [
        ("refresh_models", "auto"), ("no_such_tool", "unknown"),
    ]


@pytest.mark.asyncio
async def test_history_tool_results_render_the_ask_user_answer_line(tmp_path) -> None:
    """The answer travels twice by design: the user message is authoritative, the ``TOOL_RESULT
    ask_user`` system line names the question that was answered (or skipped)."""
    from nvh.integrations.wizard import chat as chat_mod

    answered = 'TOOL_RESULT ask_user: {"ok": true, "summary": "/mnt/nvme0"}'
    history = [
        {"role": "user", "content": "set up models"},
        {
            "role": "assistant", "content": "Which disk should hold models?",
            "tool_results": [{"name": "ask_user", "ok": True, "summary": "/mnt/nvme0"}],
        },
        {"role": "user", "content": "/mnt/nvme0"},
    ]
    with patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY_SNAPSHOT):
        turn = await chat_mod._prepare_turn(
            "go ahead", history=history, home_dir=tmp_path, enable_followup=False,
            profile="wizard", label="test", engine=None,
        )
    rendered = [(m.role, m.content) for m in chat_mod._build_messages(turn)]
    assert ("system", answered) in rendered
    assert (
        rendered.index(("assistant", "Which disk should hold models?"))
        < rendered.index(("system", answered))
        < rendered.index(("user", "/mnt/nvme0"))
    )
    skipped = chat_mod._history_tool_results_message({
        "tool_results": [{"name": "ask_user", "ok": False, "summary": "skipped by the user; not answered"}],
    })
    assert skipped == 'TOOL_RESULT ask_user: {"ok": false, "summary": "skipped by the user; not answered"}'


def _planned_registry(monkeypatch, planned: list[dict[str, Any]]):
    """A registry with one confirm-class tool whose planner records what it was asked to plan."""
    from nvh.integrations.wizard.tools import WizardTool, WizardToolRegistry

    async def handler(args):
        return {"ok": True}

    async def planner(args):
        planned.append(dict(args))
        return {"ok": True, "commands": [], "summary": f"plan for {args.get('profile')}"}

    reg = WizardToolRegistry()
    reg.register(WizardTool(
        name="planned", description="Confirm tool with a dry run.", safety_class="confirm",
        parameters={"x": {"type": "integer"}}, handler=handler, planner=planner, summary_template="Plan {x}.",
    ))
    monkeypatch.setattr("nvh.integrations.wizard.tools.default_registry", lambda **kwargs: reg)
    return reg


@pytest.mark.asyncio
async def test_confirm_tool_with_a_planner_is_surfaced_with_its_plan_and_the_pinned_profile(monkeypatch) -> None:
    """The general Wizard turn pins ``profile: "wizard"`` over whatever the model wrote, plans from the
    pinned arguments and carries the plan on the card — no approval token anywhere."""
    planned: list[dict[str, Any]] = []
    _planned_registry(monkeypatch, planned)
    engine = _fake_engine([[
        "Planning.\n", 'TOOL_CALL: {"name": "planned", "arguments": {"x": 1, "profile": "evil"}}\n',
        'TOOL_CALL: {"name": "planned", "arguments": {"x": 1}}\n',
    ]])
    events = await _events(engine)
    done = events[-1]
    assert done["type"] == "done"
    assert done["tool_calls"] == [{
        "name": "planned",
        "arguments": {"x": 1, "profile": "wizard"},
        "plan": {"ok": True, "commands": [], "summary": "plan for wizard", "pinned_arguments": {"profile": "wizard"}},
    }]
    assert planned == [{"x": 1, "profile": "wizard"}]  # the re-emitted call de-duplicated against the pin
    assert "approval_token" not in json.dumps(done, default=str)


@pytest.mark.asyncio
async def test_whitelisted_profile_ceiling_pins_the_real_profile_and_plans_the_local_backend(
    monkeypatch, tmp_path,
) -> None:
    """A model that writes ``profile: null`` does not get the hosted path: the card carries the turn's
    profile and the plan (the pinned ceiling against an unknown hosted price) is the local backend."""
    from nvh.integrations.wizard.profiles import AgentProfile, save_user_profile

    monkeypatch.setenv("NVH_HOME", str(tmp_path))
    save_user_profile(AgentProfile(
        name="artist", title="Artist", description="", system_prompt="You draw.",
        tools_allowed=["generate_image"], max_cost_usd_per_turn=0.05,
    ), home_dir=tmp_path)
    calls = _fake_imagegen(monkeypatch)
    engine = _fake_engine([[
        "Rendering.\n",
        'TOOL_CALL: {"name": "generate_image", "arguments": {"prompt": "a hero image", "profile": null}}\n',
    ]])
    events = await _events(engine, "make me a hero image", profile="artist")
    done = events[-1]
    assert done["type"] == "done" and done["used_profile"] == "artist"
    [card] = done["tool_calls"]
    assert card["name"] == "generate_image"
    assert card["arguments"] == {"prompt": "a hero image", "profile": "artist"}
    assert card["plan"]["backend"] == "comfyui" and card["plan"]["cost_known"] is True
    assert card["plan"]["pinned_arguments"] == {"profile": "artist"}
    assert card["plan"]["summary"].startswith("local ComfyUI")
    assert calls["plan"] == [{"cost_ceiling_usd": 0.05, "requested": "auto"}] and calls["render"] == []
    assert "approval_token" not in json.dumps(done, default=str)


@pytest.mark.asyncio
async def test_unattended_turn_drops_ask_user_and_never_mints_a_token(monkeypatch) -> None:
    """The schedule runner's turn: the catalog is the derived read-only roster (no ``ask_user``, no
    ``refresh_models``), a confirm or privileged request is recorded without a plan or token and
    ``execute(confirmed=False)`` is never called, the question and the daemon restart are refused."""
    from nvh.integrations.wizard import personality
    from nvh.integrations.wizard.tools import WizardTool, WizardToolRegistry

    async def ok(args):
        return {"ran": True}

    async def planner(args):
        raise AssertionError("an unattended turn never plans")

    reg = WizardToolRegistry()
    for name in ("diagnose", "web_search", "refresh_models", "rag_ask_vault"):
        reg.register(WizardTool(name=name, description=name, safety_class="auto", parameters={}, handler=ok))
    reg.register(WizardTool(
        name="ask_user", description="Ask.", safety_class="confirm", parameters={"question": {}}, handler=ok,
    ))
    reg.register(WizardTool(name="save_provider_key", description="Save.", safety_class="confirm", parameters={}, handler=ok))
    reg.register(WizardTool(
        name="system_settings_apply", description="Apply.", safety_class="privileged",
        parameters={"setting": {}}, handler=ok, planner=planner,
    ))
    monkeypatch.setattr("nvh.integrations.wizard.tools.default_registry", lambda **kwargs: reg)

    executed: list[tuple[str, bool]] = []
    real_execute = reg.execute

    async def spy(name, arguments=None, *, confirmed=False, approval_token=None):
        executed.append((name, confirmed))
        return await real_execute(name, arguments, confirmed=confirmed, approval_token=approval_token)

    monkeypatch.setattr(reg, "execute", spy)
    shown: list[list[str]] = []
    real_build = personality.build_system_prompt

    def spy_build(context, tools=None, **kw):
        shown.append(sorted(t["name"] for t in (tools or [])))
        return real_build(context, tools=tools, **kw)

    monkeypatch.setattr("nvh.integrations.wizard.personality.build_system_prompt", spy_build)

    engine = _fake_engine([
        [
            "Doing it all.\n",
            'TOOL_CALL: {"name": "system_settings_apply", "arguments": {"setting": "enable_ssh"}}\n',
            'TOOL_CALL: {"name": "save_provider_key", "arguments": {"provider": "groq", "api_key": "gsk_x"}}\n',
            _ASK,
            _REFRESH + "\n",
            'TOOL_CALL: {"name": "diagnose", "arguments": {}}\n',
        ],
        ["Done."],
    ])
    events = await _events(engine, "nightly check", unattended=True)
    done = events[-1]
    assert done["type"] == "done" and done["iterations"] == 2
    assert shown == [["diagnose", "rag_ask_vault", "web_search"]]
    assert done["tool_calls"] == [
        {"name": "system_settings_apply", "arguments": {"setting": "enable_ssh"}, "privileged": True, "unattended": True},
        {
            "name": "save_provider_key", "arguments": {"provider": "groq", "api_key": "gsk_x"},
            "privileged": False, "unattended": True,
        },
    ]
    refused = [r["name"] for r in done["tool_results"] if r["result"].get("not_allowed")]
    assert refused == ["ask_user", "refresh_models"]
    assert [r["name"] for r in done["tool_results"] if r["result"].get("ok")] == ["diagnose"]
    assert executed == [("diagnose", True)]


def _question_and_action_registry(monkeypatch):
    """``ask_user`` (confirm) beside a privileged tool with a planner, both spied."""
    from nvh.integrations.wizard.tools import WizardTool, WizardToolRegistry

    seen: dict[str, list[Any]] = {"planned": [], "executed": []}

    async def ok(args):
        return {"ran": True}

    async def planner(args):
        seen["planned"].append(dict(args))
        return {"ok": True, "commands": ["sudo cleanup D:"], "sudo": True}

    reg = WizardToolRegistry()
    reg.register(WizardTool(
        name="ask_user", description="Ask.", safety_class="confirm", parameters={"question": {}}, handler=ok,
    ))
    reg.register(WizardTool(
        name="system_settings_apply", description="Apply.", safety_class="privileged",
        parameters={"target": {}}, handler=ok, planner=planner,
    ))
    reg.register(WizardTool(name="diagnose", description="d", safety_class="auto", parameters={}, handler=ok))
    monkeypatch.setattr("nvh.integrations.wizard.tools.default_registry", lambda **kwargs: reg)
    real_execute = reg.execute

    async def spy(name, arguments=None, *, confirmed=False, approval_token=None):
        seen["executed"].append((name, confirmed))
        return await real_execute(name, arguments, confirmed=confirmed, approval_token=approval_token)

    monkeypatch.setattr(reg, "execute", spy)
    return reg, seen


@pytest.mark.asyncio
@pytest.mark.parametrize("question_first", [True, False])
async def test_a_privileged_sibling_of_a_question_is_held_not_minted(monkeypatch, question_first: bool) -> None:
    """One iteration emits ``ask_user("C: or D:?")`` and ``system_settings_apply(target="D:")``: the
    question is the only card. The apply — an action the model made conditional on an unanswered
    question — lands in ``deferred_tool_calls`` as ``question_pending``: no plan, no red card, no
    approval token minted (``execute(confirmed=False)`` never runs), whatever the order."""
    _reg, seen = _question_and_action_registry(monkeypatch)
    ask = 'TOOL_CALL: {"name": "ask_user", "arguments": {"question": "Which disk should I free: C: or D:?"}}\n'
    apply = 'TOOL_CALL: {"name": "system_settings_apply", "arguments": {"target": "D:"}}\n'
    lines = [ask, apply] if question_first else [apply, ask]
    engine = _fake_engine([["Let me check.\n", *lines], ["never reached"]])
    events = await _events(engine, "free some disk space", profile="wizard")  # the general Wizard, no whitelist
    done = events[-1]
    assert done["type"] == "done" and done["iterations"] == 1
    assert done["tool_calls"] == [{
        "name": "ask_user", "arguments": {"question": "Which disk should I free: C: or D:?"},
    }]
    assert done["deferred_tool_calls"] == [{
        "name": "system_settings_apply", "arguments": {"target": "D:"}, "reason": "question_pending",
    }]
    confirm = next(e for e in events if e["type"] == "confirm_required")
    assert confirm["tool_calls"] == done["tool_calls"]
    assert seen["planned"] == [] and seen["executed"] == []
    assert "approval_token" not in json.dumps(events, default=str)


@pytest.mark.asyncio
async def test_the_defer_path_holds_the_action_beside_a_question_too(monkeypatch) -> None:
    """With follow-up off the calls take the defer path (``_defer_calls_to_ui``) and the same rule
    applies: the question is the card, the apply beside it is held as ``question_pending``."""
    _reg, seen = _question_and_action_registry(monkeypatch)
    from nvh.integrations.wizard import chat as chat_mod

    engine = _fake_engine([[
        "Checking.\n",
        'TOOL_CALL: {"name": "ask_user", "arguments": {"question": "Which one?"}}\n',
        'TOOL_CALL: {"name": "system_settings_apply", "arguments": {"target": "D:"}}\n',
    ]])
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY_SNAPSHOT),
    ):
        events = [e async for e in chat_mod.wizard_chat_stream("free space", profile="wizard", enable_followup=False)]
    done = events[-1]
    assert [c["name"] for c in done["tool_calls"]] == ["ask_user"]
    assert done["deferred_tool_calls"] == [{
        "name": "system_settings_apply", "arguments": {"target": "D:"}, "reason": "question_pending",
    }]
    assert seen["planned"] == [] and seen["executed"] == []


@pytest.mark.asyncio
async def test_a_profile_from_the_requests_home_dir_is_pinned_with_its_home(monkeypatch, tmp_path) -> None:
    """``POST /v1/wizard/chat {home_dir, profile}``: the chat resolved ``artist`` from the request's
    home, so the card pins ``profile_home`` beside ``profile`` and the handler's plan reads the same
    profile's ceiling (local backend) instead of refusing a profile the process home never had."""
    from nvh.integrations.wizard.profiles import AgentProfile, get_profile, save_user_profile

    req_home = tmp_path / "mnt-vol"
    save_user_profile(AgentProfile(
        name="artist", title="Artist", description="", system_prompt="You draw.",
        tools_allowed=["generate_image"], max_cost_usd_per_turn=0.05,
    ), home_dir=req_home)
    assert get_profile("artist") is None, "the process home (conftest's hermetic NVH_HOME) has no artist"
    calls = _fake_imagegen(monkeypatch)
    engine = _fake_engine([[
        "Rendering.\n",
        'TOOL_CALL: {"name": "generate_image", "arguments": {"prompt": "a hero image"}}\n',
    ]])
    events = await _events(engine, "make me a hero image", profile="artist", home_dir=req_home)
    done = events[-1]
    assert done["type"] == "done" and done["used_profile"] == "artist"
    [card] = done["tool_calls"]
    assert card["arguments"] == {"prompt": "a hero image", "profile": "artist", "profile_home": str(req_home)}
    assert card["plan"]["pinned_arguments"] == {"profile": "artist", "profile_home": str(req_home)}
    assert card["plan"]["backend"] == "comfyui" and card["plan"]["ok"] is True
    assert calls["plan"] == [{"cost_ceiling_usd": 0.05, "requested": "auto"}] and calls["render"] == []
    # The handler side of the same pin: the ceiling resolves through the pinned home, and Run would too.
    from nvh.integrations.wizard.tools import _image_profile_ceiling

    assert _image_profile_ceiling("artist") == (False, None)
    assert _image_profile_ceiling("artist", home_dir=str(req_home)) == (True, 0.05)


@pytest.mark.asyncio
async def test_unattended_records_redact_secrets_and_only_whitelisted_tools(monkeypatch, tmp_path) -> None:
    """A recorded unattended request carries no key — ``api_key`` is redacted in the envelope — and a
    profile with a whitelist records only the confirm/privileged tools it allows; the rest are
    whitelist refusals, as with a human present."""
    from nvh.integrations.wizard.profiles import AgentProfile, save_user_profile
    from nvh.integrations.wizard.tools import WizardTool, WizardToolRegistry

    monkeypatch.setenv("NVH_HOME", str(tmp_path))
    save_user_profile(AgentProfile(
        name="timer", title="Timer", description="", system_prompt="", tools_allowed=["diagnose", "remember"],
    ), home_dir=tmp_path)

    async def ok(args):
        return {"ran": True}

    reg = WizardToolRegistry()
    reg.register(WizardTool(name="diagnose", description="d", safety_class="auto", parameters={}, handler=ok))
    reg.register(WizardTool(name="remember", description="r", safety_class="confirm", parameters={}, handler=ok))
    reg.register(WizardTool(name="save_provider_key", description="s", safety_class="confirm", parameters={}, handler=ok))
    monkeypatch.setattr("nvh.integrations.wizard.tools.default_registry", lambda **kwargs: reg)
    key = "gsk_" + "Ab12Cd34" * 4
    engine = _fake_engine([[
        "Saving.\n",
        'TOOL_CALL: {"name": "remember", "arguments": {"title": "key", "body": "the groq key is ' + key + '"}}\n',
        'TOOL_CALL: {"name": "save_provider_key", "arguments": {"provider": "groq", "api_key": "' + key + '"}}\n',
    ], ["Done."]])
    events = await _events(engine, "nightly", profile="timer", home_dir=tmp_path, unattended=True)
    done = events[-1]
    assert done["type"] == "done" and done["used_profile"] == "timer"
    assert done["tool_calls"] == [{
        "name": "remember", "arguments": {"title": "key", "body": "the groq key is [REDACTED:groq_key]"},
        "privileged": False, "unattended": True,
    }]
    assert [(r["name"], r["result"]["not_allowed"]) for r in done["tool_results"]] == [("save_provider_key", True)]
    assert key not in json.dumps(done["tool_calls"], default=str)
    assert "approval_token" not in json.dumps(done, default=str)
    assert "ask_user" not in json.dumps([e for e in events if e["type"] == "confirm_required"])
