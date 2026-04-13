"""NVHive Agent Loop — autonomous multi-step task execution.

The agent loop lets an LLM:
1. Receive a task
2. Think about what tools to use
3. Execute tools (with auto-approval for safe ones)
4. Read the results
5. Decide if more work is needed
6. Repeat until the task is complete

This is what makes NVHive hands-free — the LLM drives the process.

Usage:
  nvh do "Find all Python files with TODO comments and create a summary"
  nvh do "Read the README and suggest improvements"
  nvh do "Search the web for Python FastAPI best practices and summarize"

The agent loop uses tool calls in the system prompt to give the LLM
access to file operations, code execution, and web browsing.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from nvh.core.tools import ToolRegistry, ToolResult
from nvh.providers.base import Message

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 15  # safety limit
MAX_TOOL_CALLS_PER_TURN = 5


@dataclass
class AgentStep:
    """One step in the agent's execution."""
    iteration: int
    thought: str           # what the agent is thinking
    tool_calls: list[dict] # tools the agent wants to use
    tool_results: list[ToolResult]
    response: str          # the agent's response after tool use


@dataclass
class AgentResult:
    """Final result of an agent loop execution."""
    task: str
    final_response: str
    steps: list[AgentStep]
    total_iterations: int
    total_tool_calls: int
    completed: bool        # did it finish naturally or hit limit?
    error: str = ""


AGENT_SYSTEM_PROMPT = """You are an autonomous AI agent with access to tools. You can read files, write files, search the web, run code, and more.

When you need to use a tool, respond with a JSON tool call block like this:

```tool_call
{{"tool": "tool_name", "args": {{"param1": "value1"}}}}
```

You can make multiple tool calls in one response. After each tool call, you'll see the result and can decide what to do next.

Available tools:
{tool_descriptions}

Rules:
- Think step by step about what you need to do
- Use tools to gather information before answering
- If a task is complete, provide your final answer WITHOUT any tool calls
- If you're stuck, explain what you tried and why it didn't work
- Be thorough but efficient — don't use tools unnecessarily
- For file modifications, read the file first to understand the context
"""


def _summarize_tool_result(result: ToolResult, max_chars: int = 200) -> str:
    """Summarize a tool result for context compression.

    If the output is short enough, return it as-is. Otherwise, return a
    truncated version with the total character count.  For ``read_file``
    results the summary includes the filename and approximate line count.
    """
    text = result.output if result.success else result.error

    if len(text) <= max_chars:
        return text

    # Special handling for read_file — extract filename & line count
    if result.tool_name == "read_file":
        lines = text.splitlines()
        line_count = len(lines)
        # Try to grab a filename hint from the first line
        first_line = lines[0].strip() if lines else ""
        return (
            f"read_file result ({line_count} lines): "
            f"{first_line[:80]}... ({len(text)} chars total)"
        )

    return text[:100] + f"... ({len(text)} chars total)"


def _compress_history(steps: list[AgentStep], keep_full: int = 2) -> str:
    """Compress agent history, keeping the last *keep_full* steps in full.

    Older steps are reduced to one-line summaries so the LLM still knows
    what happened without paying for the full token cost.
    """
    if len(steps) <= keep_full:
        # Everything is recent — return full details
        parts: list[str] = []
        for s in steps:
            results_text = "\n".join(
                f"  Tool: {r.tool_name} — "
                f"{'Output' if r.success else 'Error'}: "
                f"{r.output if r.success else r.error}"
                for r in s.tool_results
            )
            parts.append(f"Step {s.iteration}:\n{results_text}")
        return "\n".join(parts)

    # Summarise older steps
    compressed: list[str] = []
    older = steps[:-keep_full]
    recent = steps[-keep_full:]

    for s in older:
        tool_names = ", ".join(
            f"{tc.get('tool', '?')}({', '.join(str(v) for v in tc.get('args', {}).values())})"
            for tc in s.tool_calls
        )
        status = "success" if all(r.success for r in s.tool_results) else "partial failure"
        compressed.append(f"Step {s.iteration}: Used {tool_names} → {status}")

    # Full details for recent steps
    for s in recent:
        results_text = "\n".join(
            f"  Tool: {r.tool_name}\n"
            f"  {'Output' if r.success else 'Error'}: "
            f"{r.output if r.success else r.error}"
            for r in s.tool_results
        )
        compressed.append(f"Step {s.iteration} (full):\n{results_text}")

    return "\n".join(compressed)


def _extract_tool_calls(response_text: str) -> list[dict]:
    """Extract tool_call JSON blocks from LLM response text."""
    calls = []
    # Match ```tool_call ... ``` blocks
    pattern = r'```tool_call\s*\n(.*?)\n```'
    matches = re.findall(pattern, response_text, re.DOTALL)
    for match in matches:
        try:
            call = json.loads(match.strip())
            if "tool" in call:
                calls.append(call)
        except json.JSONDecodeError:
            continue

    # Also match inline {"tool": ...} patterns
    inline_pattern = r'\{"tool":\s*"[^"]+",\s*"args":\s*\{[^}]*\}\}'
    for match in re.finditer(inline_pattern, response_text):
        try:
            call = json.loads(match.group())
            if call not in calls:
                calls.append(call)
        except json.JSONDecodeError:
            continue

    return calls[:MAX_TOOL_CALLS_PER_TURN]


async def run_agent_loop(
    task: str,
    engine,
    tools: ToolRegistry | None = None,
    provider: str | None = None,
    model: str | None = None,
    max_iterations: int = MAX_ITERATIONS,
    auto_approve_safe: bool = True,
    on_step: Any = None,  # callback(step: AgentStep) for live updates
    confirm_unsafe: Any = None,  # callback(tool_name, args) -> bool
) -> AgentResult:
    """Run the agentic execution loop.

    Args:
        task: The task description
        engine: NVHive Engine instance
        tools: Tool registry (uses default if None)
        provider: Specific advisor to use
        model: Specific model to use
        max_iterations: Safety limit on loop iterations
        auto_approve_safe: Auto-run safe tools without confirmation
        on_step: Callback for live step updates
        confirm_unsafe: Callback to confirm unsafe tool execution
    """
    if tools is None:
        tools = ToolRegistry()

    # Build system prompt with tool descriptions
    system_prompt = AGENT_SYSTEM_PROMPT.format(
        tool_descriptions=tools.get_tool_descriptions()
    )

    # Conversation history for the agent
    messages: list[Message] = [
        Message(role="user", content=f"Task: {task}"),
    ]

    steps: list[AgentStep] = []
    total_tool_calls = 0

    for iteration in range(max_iterations):
        # Get LLM response
        try:
            response = await engine.query(
                prompt=messages[-1].content if messages else task,
                provider=provider,
                model=model,
                system_prompt=system_prompt,
                stream=False,
                use_cache=False,  # don't cache agent steps
            )
        except Exception as e:
            return AgentResult(
                task=task,
                final_response="",
                steps=steps,
                total_iterations=iteration + 1,
                total_tool_calls=total_tool_calls,
                completed=False,
                error=str(e),
            )

        response_text = response.content

        # Extract tool calls
        tool_calls = _extract_tool_calls(response_text)

        if not tool_calls:
            # No tool calls — agent is done
            final = response_text.strip()

            step = AgentStep(
                iteration=iteration + 1,
                thought="Task complete",
                tool_calls=[],
                tool_results=[],
                response=final,
            )
            steps.append(step)
            if on_step:
                on_step(step)

            return AgentResult(
                task=task,
                final_response=final,
                steps=steps,
                total_iterations=iteration + 1,
                total_tool_calls=total_tool_calls,
                completed=True,
            )

        # Execute tool calls
        tool_results: list[ToolResult] = []
        for call in tool_calls:
            tool_name = call.get("tool", "")
            tool_args = call.get("args", {})
            tool = tools.get(tool_name)

            if tool is None:
                tool_results.append(ToolResult(
                    tool_name=tool_name,
                    success=False,
                    output="",
                    error=f"Unknown tool: {tool_name}",
                ))
                continue

            # Check if we need approval for unsafe tools
            if not tool.safe and not auto_approve_safe:
                if confirm_unsafe:
                    approved = confirm_unsafe(tool_name, tool_args)
                    if not approved:
                        tool_results.append(ToolResult(
                            tool_name=tool_name,
                            success=False,
                            output="",
                            error="User denied tool execution",
                        ))
                        continue

            # Execute the tool
            result = await tools.execute(tool_name, tool_args)
            tool_results.append(result)
            total_tool_calls += 1

        # Build thought from the response text (before tool calls)
        thought = response_text
        for tc in tool_calls:
            thought = thought.replace(json.dumps(tc), "").strip()
        thought = re.sub(r'```tool_call.*?```', '', thought, flags=re.DOTALL).strip()
        thought = thought[:200] if thought else f"Using {len(tool_calls)} tool(s)"

        step = AgentStep(
            iteration=iteration + 1,
            thought=thought,
            tool_calls=tool_calls,
            tool_results=tool_results,
            response=response_text,
        )
        steps.append(step)
        if on_step:
            on_step(step)

        # Feed tool results back to the agent.
        # After the 3rd iteration, compress older history to save tokens.
        messages.append(Message(role="assistant", content=response_text))

        if iteration < 3:
            # First 3 iterations: include full tool results
            results_text = "\n".join(
                f"Tool: {r.tool_name}\n"
                f"{'Output' if r.success else 'Error'}: {r.output if r.success else r.error}\n"
                for r in tool_results
            )
            messages.append(Message(
                role="user",
                content=(
                    f"Tool results:\n{results_text}\n\n"
                    "Continue with the task. If complete, provide your final answer without any tool calls."
                ),
            ))
        else:
            # Compress: summarise current results and rebuild context
            summarized_results = "\n".join(
                f"Tool: {r.tool_name}\n"
                f"{'Output' if r.success else 'Error'}: {_summarize_tool_result(r)}\n"
                for r in tool_results
            )
            history_summary = _compress_history(steps, keep_full=2)

            # Rebuild messages: keep system-level original task + compressed history
            messages = [
                Message(role="user", content=f"Task: {task}"),
                Message(
                    role="user",
                    content=(
                        f"Progress so far (compressed):\n{history_summary}\n\n"
                        f"Latest tool results:\n{summarized_results}\n\n"
                        "Continue with the task. If complete, provide "
                        "your final answer without any tool calls."
                    ),
                ),
            ]

    # Hit max iterations
    return AgentResult(
        task=task,
        final_response="Agent reached maximum iterations without completing the task.",
        steps=steps,
        total_iterations=max_iterations,
        total_tool_calls=total_tool_calls,
        completed=False,
        error="Max iterations reached",
    )
