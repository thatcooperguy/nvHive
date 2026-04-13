"""Deep tests for nvh.core.agent_loop — tool call extraction and agent loop logic.

Covers _extract_tool_calls and run_agent_loop with mocked engine/tools.
No real API calls, no real filesystem changes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nvh.core.agent_loop import (
    MAX_TOOL_CALLS_PER_TURN,
    AgentResult,
    AgentStep,
    _extract_tool_calls,
    run_agent_loop,
)
from nvh.core.tools import Tool, ToolResult

# ---------------------------------------------------------------------------
# _extract_tool_calls
# ---------------------------------------------------------------------------


class TestExtractToolCalls:
    """Tests for parsing tool_call blocks from LLM output."""

    def test_extracts_fenced_tool_call(self):
        text = 'Some thought\n```tool_call\n{"tool": "read_file", "args": {"path": "main.py"}}\n```'
        calls = _extract_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["tool"] == "read_file"
        assert calls[0]["args"]["path"] == "main.py"

    def test_extracts_multiple_fenced_blocks(self):
        text = (
            '```tool_call\n{"tool": "read_file", "args": {"path": "a.py"}}\n```\n'
            'some text\n'
            '```tool_call\n{"tool": "list_files", "args": {"pattern": "*.py"}}\n```'
        )
        calls = _extract_tool_calls(text)
        assert len(calls) == 2
        assert calls[0]["tool"] == "read_file"
        assert calls[1]["tool"] == "list_files"

    def test_extracts_inline_tool_call(self):
        text = 'I will use {"tool": "search_files", "args": {"query": "TODO"}} to find items.'
        calls = _extract_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["tool"] == "search_files"

    def test_ignores_invalid_json(self):
        text = '```tool_call\n{not valid json}\n```'
        calls = _extract_tool_calls(text)
        assert len(calls) == 0

    def test_ignores_json_without_tool_key(self):
        text = '```tool_call\n{"name": "foo", "args": {}}\n```'
        calls = _extract_tool_calls(text)
        assert len(calls) == 0

    def test_empty_response(self):
        calls = _extract_tool_calls("")
        assert calls == []

    def test_no_tool_calls_in_normal_text(self):
        text = "Here is my final answer: the result is 42."
        calls = _extract_tool_calls(text)
        assert calls == []

    def test_respects_max_tool_calls_per_turn(self):
        blocks = "\n".join(
            f'```tool_call\n{{"tool": "read_file", "args": {{"path": "f{i}.py"}}}}\n```'
            for i in range(MAX_TOOL_CALLS_PER_TURN + 5)
        )
        calls = _extract_tool_calls(blocks)
        assert len(calls) == MAX_TOOL_CALLS_PER_TURN

    def test_deduplicates_fenced_and_inline(self):
        """If the same call appears both fenced and inline, it should not be duplicated."""
        call_json = '{"tool": "read_file", "args": {"path": "x.py"}}'
        text = f'```tool_call\n{call_json}\n```\nAlso {call_json} inline.'
        calls = _extract_tool_calls(text)
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# run_agent_loop
# ---------------------------------------------------------------------------


def _make_mock_engine(responses: list[str]):
    """Build a mock engine that returns the given responses in sequence."""
    engine = AsyncMock()
    side_effects = []
    for text in responses:
        resp = MagicMock()
        resp.content = text
        side_effects.append(resp)
    engine.query = AsyncMock(side_effect=side_effects)
    return engine


def _make_mock_registry(known_tools: dict[str, bool] | None = None):
    """Build a mock ToolRegistry.

    known_tools maps tool_name -> safe flag.
    """
    if known_tools is None:
        known_tools = {"read_file": True, "write_file": False}

    registry = MagicMock()
    tools_map: dict[str, Tool] = {}
    for name, safe in known_tools.items():
        t = Tool(name=name, description=f"mock {name}", parameters={}, handler=AsyncMock(), safe=safe)
        tools_map[name] = t

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


@pytest.mark.asyncio
async def test_loop_executes_tool_then_finishes():
    """Agent calls a tool, gets result, then gives final answer."""
    engine = _make_mock_engine([
        '```tool_call\n{"tool": "read_file", "args": {"path": "main.py"}}\n```',
        "The file contains a hello world program.",
    ])
    registry = _make_mock_registry()
    result = await run_agent_loop("Read main.py", engine, tools=registry)

    assert result.completed is True
    assert result.total_iterations == 2
    assert result.total_tool_calls == 1
    assert len(result.steps) == 2


@pytest.mark.asyncio
async def test_loop_unknown_tool_produces_error_result():
    """If agent requests a tool that doesn't exist, error result is recorded."""
    engine = _make_mock_engine([
        '```tool_call\n{"tool": "nonexistent_tool", "args": {}}\n```',
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
async def test_loop_respects_max_iterations():
    """Loop stops at max_iterations even if agent keeps calling tools."""
    # Always return a tool call so the loop never naturally ends
    tool_response = '```tool_call\n{"tool": "read_file", "args": {"path": "x.py"}}\n```'
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
        '```tool_call\n{"tool": "read_file", "args": {"path": "a.py"}}\n```',
        '```tool_call\n{"tool": "read_file", "args": {"path": "b.py"}}\n```',
        '```tool_call\n{"tool": "read_file", "args": {"path": "c.py"}}\n```',
        '```tool_call\n{"tool": "read_file", "args": {"path": "d.py"}}\n```',
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
