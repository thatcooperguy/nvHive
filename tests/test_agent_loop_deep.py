"""Deep tests for nvh.core.agent_loop — tool call extraction and agent loop logic.

Covers _extract_tool_calls and run_agent_loop with mocked engine/tools.
No real API calls, no real filesystem changes.

The protocol is ``TOOL_CALL: {"name": ..., "arguments": {...}}`` (0.44 D3,
one text protocol shared with the Wizard chat). The pre-0.44 fenced
```` ```tool_call ```` block and bare ``{"tool": ..., "args": {...}}`` object
were parsed through 0.45 and are ordinary text since 0.46: never a call.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nvh.config.settings import CouncilConfig
from nvh.core.agent_loop import (
    AGENT_SYSTEM_PROMPT,
    MAX_TOOL_CALLS_PER_TURN,
    AgentResult,
    AgentStep,
    _calls_from_response,
    _extract_tool_calls,
    build_agent_system_prompt,
    response_cost_usd,
    run_agent_loop,
)
from nvh.core.tools import Tool, ToolRegistry, ToolResult
from nvh.providers.base import CompletionResponse, Usage
from nvh.providers.registry import ProviderRegistry

# ---------------------------------------------------------------------------
# _extract_tool_calls
# ---------------------------------------------------------------------------


class TestExtractToolCalls:
    """Tests for parsing tool calls from LLM output."""

    def test_extracts_the_one_protocol_line(self):
        text = 'Let me look.\nTOOL_CALL: {"name": "read_file", "arguments": {"path": "main.py"}}'
        calls = _extract_tool_calls(text)
        assert calls == [{"tool": "read_file", "args": {"path": "main.py"}}]

    def test_protocol_line_accepts_the_legacy_key_spelling_and_nested_json(self):
        text = 'TOOL_CALL: {"tool": "write_file", "args": {"path": "a.py", "content": "x = {\\"k\\": {1: 2}}"}}'
        calls = _extract_tool_calls(text)
        assert calls[0]["tool"] == "write_file"
        assert calls[0]["args"]["path"] == "a.py"

    def test_multiple_protocol_lines(self):
        text = (
            'Two reads.\n'
            'TOOL_CALL: {"name": "read_file", "arguments": {"path": "a.py"}}\n'
            'TOOL_CALL: {"name": "read_file", "arguments": {"path": "b.py"}}\n'
        )
        calls = _extract_tool_calls(text)
        assert [c["args"]["path"] for c in calls] == ["a.py", "b.py"]

    def test_fenced_block_is_ordinary_text_not_a_call(self):
        """The pre-0.44 fenced form was dropped in 0.46: no call, text untouched."""
        text = 'Some thought\n```tool_call\n{"tool": "read_file", "args": {"path": "main.py"}}\n```'
        assert _extract_tool_calls(text) == []
        thought, calls = _calls_from_response(SimpleNamespace(content=text, tool_calls=None))
        assert calls == [] and thought == text

    def test_fenced_blocks_stay_in_the_text_while_the_line_beside_them_parses(self):
        text = (
            '```tool_call\n{"tool": "read_file", "args": {"path": "a.py"}}\n```\n'
            'some text\n'
            'TOOL_CALL: {"name": "list_files", "arguments": {"pattern": "*.py"}}\n'
            '```tool_call\n{"tool": "write_file", "args": {"path": "b.py"}}\n```'
        )
        calls = _extract_tool_calls(text)
        assert calls == [{"tool": "list_files", "args": {"pattern": "*.py"}}]
        thought, _calls = _calls_from_response(SimpleNamespace(content=text, tool_calls=None))
        assert thought.count("```tool_call") == 2 and "TOOL_CALL" not in thought

    def test_bare_object_in_prose_is_not_a_call(self):
        """The pre-0.44 bare object was dropped in 0.46: a quoted or pasted one never runs a tool."""
        text = 'I will use {"tool": "search_files", "args": {"query": "TODO"}} to find items.'
        assert _extract_tool_calls(text) == []

    def test_ignores_invalid_json(self):
        text = 'TOOL_CALL: {not valid json}'
        calls = _extract_tool_calls(text)
        assert len(calls) == 0

    def test_ignores_json_without_a_tool_name(self):
        """Neither ``name`` nor ``tool`` — not a call."""
        text = '```tool_call\n{"args": {}}\n```\nTOOL_CALL: {"arguments": {"x": 1}}'
        calls = _extract_tool_calls(text)
        assert len(calls) == 0

    def test_the_prompt_teaches_only_the_protocol_line(self):
        """One text protocol: the fenced form is never taught or emitted."""
        assert "TOOL_CALL:" in AGENT_SYSTEM_PROMPT
        assert "```tool_call" not in AGENT_SYSTEM_PROMPT
        prompt = build_agent_system_prompt(ToolRegistry(include_system=False), "ROLE GUIDANCE")
        assert prompt.startswith("ROLE GUIDANCE")
        assert "TOOL_CALL:" in prompt and "read_file(" in prompt
        assert "```tool_call" not in prompt

    def test_native_tool_calls_on_the_response_win_over_text(self):
        response = CompletionResponse(
            content='TOOL_CALL: {"name": "list_files", "arguments": {}}',
            model="m", provider="p", usage=Usage(),
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}],
        )
        thought, calls = _calls_from_response(response)
        assert calls == [{"tool": "read_file", "args": {"path": "a.py"}}]
        assert "TOOL_CALL" not in thought

    def test_text_protocol_is_the_fallback_without_native_calls(self):
        response = CompletionResponse(
            content='Reading.\nTOOL_CALL: {"name": "list_files", "arguments": {"pattern": "*.py"}}',
            model="m", provider="p", usage=Usage(),
        )
        thought, calls = _calls_from_response(response)
        assert calls == [{"tool": "list_files", "args": {"pattern": "*.py"}}]
        assert thought == "Reading."

    def test_empty_response(self):
        calls = _extract_tool_calls("")
        assert calls == []

    def test_no_tool_calls_in_normal_text(self):
        text = "Here is my final answer: the result is 42."
        calls = _extract_tool_calls(text)
        assert calls == []

    def test_respects_max_tool_calls_per_turn(self):
        blocks = "\n".join(
            f'TOOL_CALL: {{"name": "read_file", "arguments": {{"path": "f{i}.py"}}}}'
            for i in range(MAX_TOOL_CALLS_PER_TURN + 5)
        )
        calls = _extract_tool_calls(blocks)
        assert len(calls) == MAX_TOOL_CALLS_PER_TURN

    def test_deduplicates_repeated_identical_protocol_lines(self):
        """The same call written twice (in either key spelling) collapses to one."""
        text = (
            'TOOL_CALL: {"name": "read_file", "arguments": {"path": "x.py"}}\n'
            'Again:\n'
            'TOOL_CALL: {"tool": "read_file", "args": {"path": "x.py"}}'
        )
        calls = _extract_tool_calls(text)
        assert calls == [{"tool": "read_file", "args": {"path": "x.py"}}]


# ---------------------------------------------------------------------------
# response_cost_usd
# ---------------------------------------------------------------------------


class TestResponseCostUsd:
    """The per-call cost the loop reads off a provider response."""

    def test_a_decimal_passes_through(self):
        resp = CompletionResponse(content="x", model="m", provider="p", usage=Usage(), cost_usd=Decimal("0.0015"))
        assert response_cost_usd(resp) == Decimal("0.0015")

    def test_zero_is_a_known_zero_not_unknown(self):
        """Local models and cache hits report ``Decimal("0")``: that is $0, not None."""
        resp = CompletionResponse(content="x", model="m", provider="p", usage=Usage())
        assert response_cost_usd(resp) == Decimal("0")

    def test_none_and_missing_are_unknown(self):
        assert response_cost_usd(SimpleNamespace(content="x", cost_usd=None)) is None
        assert response_cost_usd(SimpleNamespace(content="x")) is None
        assert response_cost_usd(None) is None

    def test_plain_numbers_and_numeric_strings_become_exact_decimals(self):
        assert response_cost_usd(SimpleNamespace(cost_usd=0.001)) == Decimal("0.001")
        assert response_cost_usd(SimpleNamespace(cost_usd=2)) == Decimal("2")
        assert response_cost_usd(SimpleNamespace(cost_usd="0.5")) == Decimal("0.5")

    def test_garbage_is_unknown_never_zero(self):
        """A MagicMock attribute, a bool, a non-numeric string or NaN add nothing."""
        assert response_cost_usd(MagicMock()) is None
        assert response_cost_usd(SimpleNamespace(cost_usd=True)) is None
        assert response_cost_usd(SimpleNamespace(cost_usd="free")) is None
        assert response_cost_usd(SimpleNamespace(cost_usd=Decimal("NaN"))) is None
        assert response_cost_usd(SimpleNamespace(cost_usd=float("inf"))) is None


# ---------------------------------------------------------------------------
# run_agent_loop
# ---------------------------------------------------------------------------


def _make_mock_engine(responses: list[str]):
    """Build a mock engine that returns the given responses in sequence."""
    engine = AsyncMock()
    engine.registry = ProviderRegistry().scoped(CouncilConfig())
    side_effects = []
    for text in responses:
        resp = MagicMock()
        resp.content = text
        side_effects.append(resp)
    engine.query = AsyncMock(side_effect=side_effects)
    return engine


def _make_costed_engine(responses: list[tuple[str, Decimal | None]]):
    """A mock engine whose replies carry a provider cost — or none at all.

    A ``Decimal`` cost rides on a real ``CompletionResponse``; ``None`` stands
    for a provider (or stream) that reported no cost and is a bare namespace,
    since the response model types ``cost_usd`` as a ``Decimal``.
    """
    engine = AsyncMock()
    engine.registry = ProviderRegistry().scoped(CouncilConfig())
    side_effects: list[object] = []
    for text, cost in responses:
        if cost is None:
            side_effects.append(SimpleNamespace(content=text, cost_usd=None, tool_calls=None))
        else:
            side_effects.append(CompletionResponse(
                content=text, model="m", provider="p", usage=Usage(), cost_usd=cost,
            ))
    engine.query = AsyncMock(side_effect=side_effects)
    return engine


def _make_mock_registry(known_tools: dict[str, bool] | None = None):
    """Build a mock ToolRegistry.

    known_tools maps tool_name -> safe flag.
    """
    if known_tools is None:
        known_tools = {"read_file": True, "write_file": False}

    registry = ToolRegistry(include_system=False, builtins=False)
    tools_map: dict[str, Tool] = {}
    for name, safe in known_tools.items():
        t = Tool(name=name, description=f"mock {name}", parameters={}, handler=AsyncMock(), safe=safe)
        tools_map[name] = t
        registry.register(t)

    registry.get = MagicMock(side_effect=lambda n: tools_map.get(n))
    registry.get_tool_descriptions = MagicMock(return_value="mock tool descriptions")
    registry.execute = AsyncMock(
        return_value=ToolResult(tool_name="read_file", success=True, output="file contents")
    )
    return registry


@pytest.mark.asyncio
async def test_loop_completes_on_no_tool_calls():
    """Agent returns a response with no tool calls => loop ends immediately."""
    engine = _make_mock_engine(["The answer is 42."])
    result = await run_agent_loop("What is 6*7?", engine, tools=_make_mock_registry())

    assert isinstance(result, AgentResult)
    assert result.completed is True
    assert result.total_iterations == 1
    assert result.total_tool_calls == 0
    assert "42" in result.final_response
    # A MagicMock reply has no usable cost_usd: unknown, so the total stays $0
    # and the step says so rather than inventing a figure.
    assert result.total_cost_usd == Decimal("0")
    assert result.steps[0].cost_usd is None


@pytest.mark.asyncio
async def test_loop_executes_tool_then_finishes():
    """Agent calls a tool, gets result, then gives final answer."""
    engine = _make_mock_engine([
        'TOOL_CALL: {"name": "read_file", "arguments": {"path": "main.py"}}',
        "The file contains a hello world program.",
    ])
    registry = _make_mock_registry()
    result = await run_agent_loop("Read main.py", engine, tools=registry)

    assert result.completed is True
    assert result.total_iterations == 2
    assert result.total_tool_calls == 1
    assert len(result.steps) == 2
    # The click was the caller's (auto tool here): execute() is told so.
    registry.execute.assert_called_once_with("read_file", {"path": "main.py"}, confirmed=True)
    # The one prompt reached the engine, with the registry's catalogue.
    assert engine.query.call_args_list[0].kwargs["system_prompt"].count("TOOL_CALL:") >= 1


@pytest.mark.asyncio
async def test_loop_reads_native_tool_calls_from_the_response():
    """A provider that took tools= answers tool_calls; the loop runs them like text calls."""
    engine = AsyncMock()
    first = MagicMock()
    first.content = ""
    first.tool_calls = [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "main.py"}'}}]
    second = MagicMock()
    second.content = "Done."
    second.tool_calls = None
    engine.query = AsyncMock(side_effect=[first, second])
    registry = _make_mock_registry()

    result = await run_agent_loop("Read main.py", engine, tools=registry)

    assert result.completed is True and result.total_tool_calls == 1
    assert result.steps[0].tool_calls == [{"tool": "read_file", "args": {"path": "main.py"}}]


@pytest.mark.asyncio
async def test_result_cost_known_is_the_catalogs_verdict_on_every_step():
    """AgentResult.cost_known (0.46): each step records the model and provider
    that answered, and the result is known only when every step's cost was a
    price the catalog vouches for -- a free local run is a known $0, a $0
    nobody priced or a step with no cost at all makes the total a floor."""
    def reply(provider, cost):
        return CompletionResponse(
            content="Final.", model="nobody/priced-this", provider=provider,
            usage=Usage(input_tokens=5, output_tokens=1, total_tokens=6), cost_usd=Decimal(cost),
        )

    def engine_for(*replies):
        engine = AsyncMock()
        engine.registry = ProviderRegistry().scoped(CouncilConfig())
        engine.query = AsyncMock(side_effect=list(replies))
        return engine

    free = await run_agent_loop("x", engine_for(reply("ollama", "0")), tools=_make_mock_registry())
    assert free.cost_known is True and free.total_cost_usd == 0
    assert (free.steps[0].model, free.steps[0].provider) == ("nobody/priced-this", "ollama")
    unpriced = await run_agent_loop("x", engine_for(reply("openai", "0")), tools=_make_mock_registry())
    assert unpriced.cost_known is False
    billed = await run_agent_loop("x", engine_for(reply("openai", "0.002")), tools=_make_mock_registry())
    assert billed.cost_known is True and billed.total_cost_usd == Decimal("0.002")
    no_cost = await run_agent_loop("x", _make_costed_engine([("Final.", None)]), tools=_make_mock_registry())
    assert no_cost.cost_known is False
    # One unpriced step among priced ones is enough: the total is a floor.
    mixed = await run_agent_loop(
        "x",
        engine_for(
            CompletionResponse(content='TOOL_CALL: {"name": "read_file", "arguments": {"path": "a"}}', model="m",
                               provider="ollama", usage=Usage(input_tokens=5, output_tokens=1, total_tokens=6),
                               cost_usd=Decimal("0")),
            reply("openai", "0"),
        ),
        tools=_make_mock_registry(),
    )
    assert mixed.total_tool_calls == 1 and mixed.cost_known is False


@pytest.mark.asyncio
async def test_loop_passes_role_guidance_ahead_of_the_one_prompt():
    engine = _make_mock_engine(["Final."])
    await run_agent_loop("x", engine, tools=_make_mock_registry(), system_prompt="BE A CODER")
    prompt = engine.query.call_args.kwargs["system_prompt"]
    assert prompt.startswith("BE A CODER")
    assert "mock tool descriptions" in prompt


@pytest.mark.asyncio
async def test_loop_blocks_unsafe_tool_without_approval_callback():
    """Unsafe tools must not execute unless an approval callback is available."""
    engine = _make_mock_engine([
        'TOOL_CALL: {"name": "write_file", "arguments": {"path": "x.py", "content": "x"}}',
        "I could not write the file.",
    ])
    registry = _make_mock_registry()

    result = await run_agent_loop("Write a file", engine, tools=registry)

    assert result.completed is True
    assert result.total_tool_calls == 0
    registry.execute.assert_not_called()
    error = result.steps[0].tool_results[0].error
    assert error is not None
    assert "requires approval" in error


@pytest.mark.asyncio
async def test_loop_runs_unsafe_tool_when_approved():
    """Unsafe tools execute only after the approval callback allows them."""
    engine = _make_mock_engine([
        'TOOL_CALL: {"name": "write_file", "arguments": {"path": "x.py", "content": "x"}}',
        "Done.",
    ])
    registry = _make_mock_registry()
    approvals: list[str] = []

    def approve(tool_name: str, _tool_args: dict) -> bool:
        approvals.append(tool_name)
        return True

    result = await run_agent_loop(
        "Write a file",
        engine,
        tools=registry,
        confirm_unsafe=approve,
    )

    assert result.completed is True
    assert result.total_tool_calls == 1
    assert approvals == ["write_file"]
    registry.execute.assert_called_once()


@pytest.mark.asyncio
async def test_loop_unknown_tool_produces_error_result():
    """If agent requests a tool that doesn't exist, error result is recorded."""
    engine = _make_mock_engine([
        'TOOL_CALL: {"name": "nonexistent_tool", "arguments": {}}',
        "I could not find that tool. Here's my answer instead.",
    ])
    registry = _make_mock_registry()
    result = await run_agent_loop("do something", engine, tools=registry)

    assert result.completed is True
    # The first step should have a failed tool result
    step0 = result.steps[0]
    assert len(step0.tool_results) == 1
    assert step0.tool_results[0].success is False
    assert "Unknown tool" in step0.tool_results[0].error


@pytest.mark.asyncio
async def test_loop_treats_a_fenced_block_as_the_final_answer_since_0_46():
    """A model still writing the pre-0.44 fenced block runs nothing: the loop
    reads no call, so the text is its final answer and no tool is executed."""
    fenced = 'On it.\n```tool_call\n{"tool": "write_file", "args": {"path": "x.py", "content": "x"}}\n```'
    engine = _make_mock_engine([fenced, "never reached"])
    registry = _make_mock_registry()

    result = await run_agent_loop("Write a file", engine, tools=registry)

    assert result.completed is True and result.total_iterations == 1
    assert result.total_tool_calls == 0
    registry.execute.assert_not_called()
    assert result.final_response == fenced
    assert engine.query.await_count == 1


@pytest.mark.asyncio
async def test_loop_respects_max_iterations():
    """Loop stops at max_iterations even if agent keeps calling tools."""
    # Always return a tool call so the loop never naturally ends
    tool_response = 'TOOL_CALL: {"name": "read_file", "arguments": {"path": "x.py"}}'
    engine = _make_mock_engine([tool_response] * 5)
    registry = _make_mock_registry()

    result = await run_agent_loop("loop forever", engine, tools=registry, max_iterations=3)

    assert result.completed is False
    assert result.total_iterations == 3
    assert "Max iterations" in result.error


@pytest.mark.asyncio
async def test_loop_handles_engine_error():
    """If the engine raises an exception, the loop returns an error result."""
    engine = AsyncMock()
    engine.query = AsyncMock(side_effect=RuntimeError("API is down"))
    registry = _make_mock_registry()

    result = await run_agent_loop("test", engine, tools=registry)

    assert result.completed is False
    assert "API is down" in result.error


@pytest.mark.asyncio
async def test_on_step_callback_called():
    """The on_step callback is invoked for each step."""
    engine = _make_mock_engine(["Final answer."])
    steps_received = []

    result = await run_agent_loop(
        "test", engine,
        tools=_make_mock_registry(),
        on_step=lambda s: steps_received.append(s),
    )

    assert len(steps_received) == 1
    assert isinstance(steps_received[0], AgentStep)


@pytest.mark.asyncio
async def test_loop_compresses_history_after_iteration_3():
    """After iteration 3, the message list is compressed to save tokens."""
    responses = [
        'TOOL_CALL: {"name": "read_file", "arguments": {"path": "a.py"}}',
        'TOOL_CALL: {"name": "read_file", "arguments": {"path": "b.py"}}',
        'TOOL_CALL: {"name": "read_file", "arguments": {"path": "c.py"}}',
        'TOOL_CALL: {"name": "read_file", "arguments": {"path": "d.py"}}',
        "Done reading all files.",
    ]
    engine = _make_mock_engine(responses)
    registry = _make_mock_registry()

    result = await run_agent_loop("read everything", engine, tools=registry)

    assert result.completed is True
    assert result.total_iterations == 5
    # Verify that the engine was called with compressed context
    # (the 5th call should have "compressed" in the prompt)
    calls = engine.query.call_args_list
    assert len(calls) == 5
    # After iteration 3, prompts should contain "compressed"
    last_prompt = calls[4].kwargs.get("prompt", calls[4].args[0] if calls[4].args else "")
    assert "compressed" in last_prompt.lower() or "Progress" in last_prompt


# ---------------------------------------------------------------------------
# Per-call cost: surfaced on each step, summed on the result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_total_cost_is_the_sum_of_its_two_calls():
    """Two provider calls, two reported costs: each step carries its own, the result the sum."""
    engine = _make_costed_engine([
        ('TOOL_CALL: {"name": "read_file", "arguments": {"path": "main.py"}}', Decimal("0.0015")),
        ("The file prints hello.", Decimal("0.0025")),
    ])
    result = await run_agent_loop("Read main.py", engine, tools=_make_mock_registry())

    assert result.completed is True and result.total_iterations == 2
    assert [s.cost_usd for s in result.steps] == [Decimal("0.0015"), Decimal("0.0025")]
    assert result.total_cost_usd == Decimal("0.004")
    assert isinstance(result.total_cost_usd, Decimal)


@pytest.mark.asyncio
async def test_loop_tolerates_a_call_that_reports_no_cost():
    """A None cost is unknown: the step records None and the sum skips it (no $0 pretence)."""
    engine = _make_costed_engine([
        ('TOOL_CALL: {"name": "read_file", "arguments": {"path": "main.py"}}', None),
        ("Done.", Decimal("0.002")),
    ])
    result = await run_agent_loop("Read main.py", engine, tools=_make_mock_registry())

    assert result.completed is True
    assert result.steps[0].cost_usd is None
    assert result.steps[1].cost_usd == Decimal("0.002")
    assert result.total_cost_usd == Decimal("0.002")


@pytest.mark.asyncio
async def test_loop_keeps_the_cost_already_spent_when_the_engine_fails():
    """Money spent before an engine error is still reported on the error result."""
    first = CompletionResponse(
        content='TOOL_CALL: {"name": "read_file", "arguments": {"path": "main.py"}}',
        model="m", provider="p", usage=Usage(), cost_usd=Decimal("0.001"),
    )
    engine = AsyncMock()
    engine.registry = ProviderRegistry().scoped(CouncilConfig())
    engine.query = AsyncMock(side_effect=[first, RuntimeError("API is down")])
    result = await run_agent_loop("Read main.py", engine, tools=_make_mock_registry())

    assert result.completed is False and "API is down" in result.error
    assert result.total_cost_usd == Decimal("0.001")


@pytest.mark.asyncio
async def test_loop_sums_cost_across_every_iteration_up_to_the_limit():
    tool_response = 'TOOL_CALL: {"name": "read_file", "arguments": {"path": "x.py"}}'
    engine = _make_costed_engine([(tool_response, Decimal("0.001"))] * 3)
    result = await run_agent_loop("loop", engine, tools=_make_mock_registry(), max_iterations=3)

    assert result.completed is False and "Max iterations" in result.error
    assert result.total_cost_usd == Decimal("0.003")


@pytest.mark.asyncio
async def test_on_step_sees_the_step_cost():
    engine = _make_costed_engine([("Final answer.", Decimal("0.0007"))])
    seen: list[Decimal | None] = []
    await run_agent_loop("x", engine, tools=_make_mock_registry(), on_step=lambda s: seen.append(s.cost_usd))
    assert seen == [Decimal("0.0007")]


# ---------------------------------------------------------------------------
# Tool-call block extraction and end-to-end loops with a real ToolRegistry
# ---------------------------------------------------------------------------


class TestAgentLoopDeep:
    def test_extract_tool_calls_multiline_json_object(self):
        '''The object after the marker may span lines.'''
        text = '''Let me read the file.
TOOL_CALL: {
  "name": "read_file",
  "arguments": {"path": "main.py"}
}
'''
        calls = _extract_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["tool"] == "read_file"

    def test_extract_tool_calls_multiple(self):
        text = '''
TOOL_CALL: {"name": "read_file", "arguments": {"path": "a.py"}}
Then I'll write:
TOOL_CALL: {"name": "write_file", "arguments": {"path": "b.py", "content": "hello"}}
'''
        calls = _extract_tool_calls(text)
        assert len(calls) == 2

    def test_extract_tool_calls_none(self):
        calls = _extract_tool_calls("Just a regular response with no tools.")
        assert calls == []

    def test_extract_tool_calls_malformed_json(self):
        text = 'TOOL_CALL: {"name": "read_file", "arguments": {invalid json}}'
        calls = _extract_tool_calls(text)
        assert calls == []

    def test_extract_tool_calls_max_limit(self):
        # Generate more than MAX_TOOL_CALLS_PER_TURN
        blocks = "\n".join(
            f'TOOL_CALL: {{"name": "read_file", "arguments": {{"path": "file{i}.py"}}}}'
            for i in range(MAX_TOOL_CALLS_PER_TURN + 3)
        )
        calls = _extract_tool_calls(blocks)
        assert len(calls) == MAX_TOOL_CALLS_PER_TURN

    @pytest.mark.asyncio
    async def test_agent_loop_completes_without_tools(self):
        class MockEngine:
            registry = ProviderRegistry().scoped(CouncilConfig())

            async def query(self, prompt="", **kw):
                return CompletionResponse(
                    content="The answer is 42.", model="m", provider="mock",
                    usage=Usage(total_tokens=10), cost_usd=Decimal("0"), latency_ms=1,
                )
        result = await run_agent_loop(task="What is 6*7?", engine=MockEngine(), max_iterations=3)
        assert result.completed is True
        assert "42" in result.final_response

    @pytest.mark.asyncio
    async def test_agent_loop_uses_tools(self):
        call_count = 0

        class MockEngine:
            registry = ProviderRegistry().scoped(CouncilConfig())

            async def query(self, prompt="", **kw):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return CompletionResponse(
                        content='TOOL_CALL: {"name": "list_files", "arguments": {"pattern": "*.py"}}',
                        model="m", provider="mock",
                        usage=Usage(total_tokens=10), cost_usd=Decimal("0"), latency_ms=1,
                    )
                return CompletionResponse(
                    content="Found the files. Task complete.",
                    model="m", provider="mock",
                    usage=Usage(total_tokens=10), cost_usd=Decimal("0"), latency_ms=1,
                )

        tools = ToolRegistry(workspace=".", include_system=False)
        result = await run_agent_loop(
            task="List Python files", engine=MockEngine(),
            tools=tools, max_iterations=5,
        )
        assert result.completed is True
        assert result.total_tool_calls >= 1
