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

The agent loop teaches the model the ONE tool-description prompt
(:data:`AGENT_SYSTEM_PROMPT`, built by :func:`build_agent_system_prompt`) and
the ONE text protocol — ``TOOL_CALL: {"name": ..., "arguments": {...}}`` —
parsed by :func:`nvh.core.tools.parse_tool_calls`. A response that carries
native ``tool_calls`` (a provider that took ``tools=``) is read first; the
text protocol is the fallback that works on any model.
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any

from nvh.core.tools import (
    TOOL_CALL_MARKER,
    ToolRegistry,
    ToolResult,
    normalize_tool_calls,
    parse_tool_calls,
)
from nvh.providers.base import Message

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 15  # safety limit
MAX_TOOL_CALLS_PER_TURN = 5


@dataclass
class AgentStep:
    """One step in the agent's execution."""
    iteration: int
    thought: str           # what the agent is thinking
    tool_calls: list[dict] # tools the agent wants to use: [{"tool": name, "args": {...}}]
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


DESKTOP_AGENT_SUPPLEMENT = """
You can also control the desktop computer. For visual/GUI tasks, follow this workflow:
1. Use capture_screenshot to see the current screen state
2. Use analyze_image on the screenshot to understand what is visible
3. Based on the analysis, use mouse_click, keyboard_type, keyboard_press, or scroll to interact
4. Take another screenshot to verify your action worked
5. Repeat until the task is complete

Coordinate estimation for clicking:
- When asking analyze_image for element positions, ask: "What percentage from the left
  edge and top edge of the screen is the [element]?" (e.g., "the OK button is at ~75%
  from left, ~85% from top")
- First determine screen resolution with: shell "xdpyinfo | grep dimensions" or
  shell "xrandr | grep '*'"
- Then compute pixel coordinates: x = percentage * width, y = percentage * height
- Raw pixel coordinate guesses from vision models are unreliable — always use
  relative positioning and compute the actual pixels yourself

Desktop tips:
- Always screenshot BEFORE and AFTER actions to verify state
- For installing software: use the shell tool to run git clone, pip install, etc.
- For launching apps: use the shell tool, then use screenshots to verify the app opened
- Use keyboard_press for hotkeys like "ctrl+c", "alt+tab", "enter"
- Before destructive actions (closing windows, pressing Enter on dialogs), explain
  what you are about to do
"""


#: The one tool-description prompt. ``{role_preamble}`` is the caller's role
#: guidance (the coding agent's approach and rules), ``{tool_descriptions}``
#: the registry's catalogue, ``{desktop_supplement}`` the desktop workflow when
#: the desktop tools are registered.
AGENT_SYSTEM_PROMPT = """{role_preamble}You are an autonomous AI agent with access to tools. You can read files, write files, search the web, run code, and more.

When you need to use a tool, put each call on its own line, exactly like this:

TOOL_CALL: {{"name": "tool_name", "arguments": {{"param1": "value1"}}}}

You can make multiple tool calls in one response. After each tool call, you'll see the result and can decide what to do next.

{tool_descriptions}
{desktop_supplement}
Rules:
- Think step by step about what you need to do
- Use tools to gather information before answering
- If a task is complete, provide your final answer WITHOUT any tool calls
- If you're stuck, explain what you tried and why it didn't work
- Be thorough but efficient — don't use tools unnecessarily
- For file modifications, read the file first to understand the context
"""


def build_agent_system_prompt(tools: ToolRegistry, preamble: str | None = None) -> str:
    """The system prompt for one run: ``preamble`` (role guidance) + the one tool prompt.

    The desktop workflow supplement is included when the registry carries the
    desktop tools (``capture_screenshot``).
    """
    has_desktop = tools.get("capture_screenshot") is not None
    role = preamble.strip() + "\n\n" if preamble and preamble.strip() else ""
    return AGENT_SYSTEM_PROMPT.format(
        role_preamble=role,
        tool_descriptions=tools.get_tool_descriptions(),
        desktop_supplement=DESKTOP_AGENT_SUPPLEMENT if has_desktop else "",
    )


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


def _as_step_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``[{name, arguments}]`` as the loop's ``[{tool, args}]`` step shape, capped per turn."""
    return [
        {"tool": call["name"], "args": dict(call.get("arguments") or {})}
        for call in calls[:MAX_TOOL_CALLS_PER_TURN]
    ]


def _extract_tool_calls(response_text: str) -> list[dict]:
    """The tool calls in a model's text — ``TOOL_CALL: {json}`` lines (the deprecated
    fenced block and bare object too, for one release: ``legacy=True`` is this
    loop's alone) — as ``[{tool, args}]``."""
    _stripped, calls = parse_tool_calls(response_text or "", legacy=True)
    return _as_step_calls(calls)


def _calls_from_response(response: Any) -> tuple[str, list[dict]]:
    """``(thought text, [{tool, args}])`` from one completion.

    Native ``tool_calls`` on the response (a provider that took ``tools=``)
    win; otherwise the text protocol is parsed. Either way the text handed
    back has the markers stripped.
    """
    text = getattr(response, "content", "") or ""
    stripped, text_calls = parse_tool_calls(text, legacy=True)
    native = normalize_tool_calls(getattr(response, "tool_calls", None))
    calls = native or text_calls
    return stripped, _as_step_calls(calls)


def _step_result(name: str, outcome: Any) -> ToolResult:
    """Whatever ``execute()`` (or a test double) returned, as the loop's ``ToolResult``."""
    if isinstance(outcome, ToolResult):
        return outcome
    if isinstance(outcome, dict):
        return ToolResult(outcome)
    if hasattr(outcome, "success"):
        return ToolResult(
            tool_name=getattr(outcome, "tool_name", name),
            success=bool(getattr(outcome, "success", False)),
            output=getattr(outcome, "output", ""),
            error=getattr(outcome, "error", "") or None,
        )
    return ToolResult(tool_name=name, success=True, output=str(outcome))


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
    system_prompt: str | None = None,
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
        system_prompt: Role guidance prepended to the one tool prompt (the
            coding agent's approach and rules); the tool catalogue and the
            protocol are never duplicated by the caller.
    """
    if tools is None:
        tools = ToolRegistry()

    # Build system prompt with tool descriptions
    # Include desktop agent guidance if vision/desktop tools are available
    full_system_prompt = build_agent_system_prompt(tools, system_prompt)

    # Native function calling degrades (D3): the same catalogue goes out as
    # ``tools=`` too. The engine hands it to the provider, which sends it only
    # when the resolved model can take it (and never to the Responses
    # surface); everywhere else the prompt's text protocol is what the model
    # sees, and both channels are read back below.
    native_tools = tools.openai_tools()

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
                system_prompt=full_system_prompt,
                stream=False,
                use_cache=False,  # don't cache agent steps
                tools=native_tools or None,
                tool_choice="auto" if native_tools else None,
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

        response_text = response.content or ""

        # Extract tool calls — native ones first, then the text protocol
        thought_text, tool_calls = _calls_from_response(response)

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

            # Anything that is not exactly ``auto`` requires an explicit
            # approval callback. ``auto_approve_safe`` only controls
            # read/search-style tools.
            if tool.safety_class != "auto":
                if not confirm_unsafe:
                    tool_results.append(ToolResult(
                        tool_name=tool_name,
                        success=False,
                        output="",
                        error=f"Tool requires approval: {tool_name}",
                    ))
                    continue

                approved = confirm_unsafe(tool_name, tool_args)
                if inspect.isawaitable(approved):
                    approved = await approved
                if not approved:
                    tool_results.append(ToolResult(
                        tool_name=tool_name,
                        success=False,
                        output="",
                        error="User denied tool execution",
                    ))
                    continue
            elif not auto_approve_safe and confirm_unsafe:
                approved = confirm_unsafe(tool_name, tool_args)
                if inspect.isawaitable(approved):
                    approved = await approved
                if not approved:
                    tool_results.append(ToolResult(
                        tool_name=tool_name,
                        success=False,
                        output="",
                        error="User denied tool execution",
                    ))
                    continue

            # Execute the tool. The caller's callback was the click, so the
            # call is confirmed; a privileged tool (none in the agent registry)
            # would still be refused without its card's token.
            result = _step_result(tool_name, await tools.execute(tool_name, tool_args, confirmed=True))
            tool_results.append(result)
            total_tool_calls += 1

        # Build thought from the response text (before tool calls)
        thought = thought_text
        for tc in tool_calls:
            thought = thought.replace(json.dumps(tc), "").strip()
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


__all__ = [
    "AGENT_SYSTEM_PROMPT",
    "DESKTOP_AGENT_SUPPLEMENT",
    "MAX_ITERATIONS",
    "MAX_TOOL_CALLS_PER_TURN",
    "TOOL_CALL_MARKER",
    "AgentResult",
    "AgentStep",
    "build_agent_system_prompt",
    "run_agent_loop",
]
