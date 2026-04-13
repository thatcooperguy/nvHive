"""Tests for agent loop context management helpers."""

from __future__ import annotations

from nvh.core.agent_loop import AgentStep, _compress_history, _summarize_tool_result  # noqa: E501
from nvh.core.tools import ToolResult

# -- _summarize_tool_result ---------------------------------------------------

def test_summarize_short_output():
    """Output under max_chars is returned unchanged."""
    r = ToolResult(tool_name="list_files", success=True, output="file1.py\nfile2.py")
    assert _summarize_tool_result(r) == "file1.py\nfile2.py"


def test_summarize_long_output():
    """Output over max_chars is truncated with a char count."""
    long_text = "x" * 500
    r = ToolResult(tool_name="run_code", success=True, output=long_text)
    summary = _summarize_tool_result(r, max_chars=200)
    assert summary.startswith("x" * 100)
    assert "(500 chars total)" in summary
    assert len(summary) < 500


def test_summarize_read_file_result():
    """read_file results include filename hint and line count."""
    content = "\n".join(f"line {i}" for i in range(300))
    r = ToolResult(tool_name="read_file", success=True, output=content)
    summary = _summarize_tool_result(r, max_chars=200)
    assert "300 lines" in summary
    assert "chars total" in summary


def test_summarize_error_result():
    """Errors over max_chars are also truncated."""
    err = "E" * 400
    r = ToolResult(tool_name="run_code", success=False, output="", error=err)
    summary = _summarize_tool_result(r, max_chars=200)
    assert "400 chars total" in summary


# -- _compress_history ---------------------------------------------------------

def _make_step(iteration: int, tool_names: list[str], success: bool = True) -> AgentStep:
    """Helper to build a minimal AgentStep."""
    calls = [{"tool": t, "args": {"path": f"{t}.py"}} for t in tool_names]
    results = [
        ToolResult(tool_name=t, success=success, output=f"ok from {t}")
        for t in tool_names
    ]
    return AgentStep(
        iteration=iteration,
        thought="thinking",
        tool_calls=calls,
        tool_results=results,
        response="resp",
    )


def test_compress_history_keeps_recent():
    """Last 2 steps should have full details; older ones are one-liners."""
    steps = [
        _make_step(1, ["read_file"]),
        _make_step(2, ["write_file"]),
        _make_step(3, ["list_files"]),
        _make_step(4, ["run_code"]),
    ]
    compressed = _compress_history(steps, keep_full=2)

    # Older steps are one-line summaries
    assert "Step 1: Used" in compressed
    assert "Step 2: Used" in compressed
    # Recent steps have full output
    assert "Step 3 (full):" in compressed
    assert "Step 4 (full):" in compressed
    assert "ok from list_files" in compressed
    assert "ok from run_code" in compressed
    # Older steps should NOT have full output
    assert "ok from read_file" not in compressed


def test_compress_history_few_steps():
    """When steps <= keep_full, all are returned in full."""
    steps = [
        _make_step(1, ["read_file"]),
        _make_step(2, ["write_file"]),
    ]
    compressed = _compress_history(steps, keep_full=2)

    # Both should have full details
    assert "ok from read_file" in compressed
    assert "ok from write_file" in compressed
    # Should NOT have one-line summary format
    assert "→" not in compressed
