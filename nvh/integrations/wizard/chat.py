"""AI Wizard chat — LLM-routed conversation grounded in live workspace state.

This is the upgrade path on top of the existing deterministic
``setup_assistant_reply``. Flow per turn:

  1. Collect live workspace state via ``wizard_context()`` (Wizard-2 context).
  2. Build the system prompt with personality + state via ``build_system_prompt()``.
  3. Route the user message through the engine — uses local Ollama when
     healthy, falls back to any configured cloud provider, and finally
     drops back to the deterministic ``setup_assistant_reply`` if no LLM
     is reachable. The deterministic flow stays the safe net.
  4. If the LLM emitted ``TOOL_CALL:`` markers, **run the auto-class tools
     server-side** (Wizard-5 follow-up loop), append the results to the
     conversation as system messages, and give the LLM one more turn to
     react. Repeats up to ``WIZARD_FOLLOWUP_MAX_ITER`` times so the model
     can chain a small read → think → act → react sequence in a single
     user turn. Confirm-class tools never auto-execute; they're surfaced
     to the caller for UI confirmation.
  5. Return ``{answer, mode, used_provider, used_model, context,
     tool_calls, tool_results?, iterations, fallback_reason?}``.

Cost is one or more completions per user turn; conversation history is
supplied by the caller so the function is stateless.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Cap the read→think→act→react cycle. Real ceiling on how chatty a single
# Wizard turn can be. 3 keeps loops bounded and the user's wait reasonable;
# the model has to converge on an answer within: initial reply →
# react-to-tool-result → final summary.
WIZARD_FOLLOWUP_MAX_ITER = 3

# Matches `TOOL_CALL: {...json...}` (optionally inside a fenced code block).
# Greedy on the JSON object so multi-line argument bodies still match.
_TOOL_CALL_RE = re.compile(
    r"TOOL_CALL\s*:\s*(\{(?:[^{}]|\{[^{}]*\})*\})",
    re.MULTILINE | re.DOTALL,
)


def _extract_tool_calls(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Strip ``TOOL_CALL:`` markers out of the LLM's text response.

    Returns ``(stripped_text, [{name, arguments}, ...])``. Malformed JSON is
    silently dropped — better to show the user a plain answer than to
    surface a parse error mid-chat.
    """
    calls: list[dict[str, Any]] = []
    for match in _TOOL_CALL_RE.finditer(text):
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, dict) and isinstance(parsed.get("name"), str):
                calls.append({
                    "name": parsed["name"],
                    "arguments": parsed.get("arguments", {}) if isinstance(parsed.get("arguments"), dict) else {},
                })
        except (json.JSONDecodeError, ValueError):
            continue
    stripped = _TOOL_CALL_RE.sub("", text).strip()
    return stripped, calls


def _format_tool_result_message(name: str, result: dict[str, Any]) -> str:
    """Render a completed tool result as a system message the model can read.

    Compact JSON keeps the prompt small; the model already knows the tool's
    schema from the system prompt.
    """
    try:
        payload = json.dumps(result, default=str)[:1500]
    except Exception:
        payload = str(result)[:1500]
    return f"TOOL_RESULT {name}: {payload}"


async def _run_auto_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute a single auto-class tool via the default registry.

    Returns ``{ok, result?, error?, safety_class}``. Confirm-class tools
    are *not* auto-executed — the caller surfaces them to the UI instead.
    """
    try:
        from nvh.integrations.wizard.tools import default_registry

        registry = default_registry()
    except Exception as exc:
        logger.debug("auto-tool: registry not available (%s)", exc)
        return {"ok": False, "error": "tool registry unavailable"}

    tool = registry.get(name)
    if tool is None:
        return {"ok": False, "error": f"unknown tool: {name}"}
    if tool.safety_class != "auto":
        # Defer to UI — never silently auto-execute a confirm-class tool.
        return {
            "ok": False,
            "deferred_to_user": True,
            "safety_class": tool.safety_class,
            "error": "confirm-class — surfaced to user instead of auto-running",
        }
    return await registry.execute(name, arguments=arguments, confirmed=True)


async def wizard_chat(
    question: str,
    *,
    history: list[dict[str, str]] | None = None,
    home_dir: str | Path | None = None,
    enable_followup: bool = True,
) -> dict[str, Any]:
    """Answer a Wizard question with the live-state-grounded LLM path.

    Args:
        question: The user's message for this turn.
        history: Optional prior turns as a list of ``{role, content}`` dicts.
            ``role`` is one of ``"user" | "assistant"``. Empty/None starts
            a fresh conversation.
        home_dir: NVH_HOME override; defaults to the current workspace.
        enable_followup: If True (default), auto-class tool calls run
            server-side and the LLM gets follow-up turns to react. Set to
            False to keep the original one-shot behavior — useful for
            tests that mock a single ``provider.complete`` call.

    Returns:
        ``{answer, mode, used_provider?, used_model?, context, tool_calls,
        tool_results?, iterations, fallback_reason?}`` where ``mode`` is:

          - ``"llm"``  — answered by a routed LLM (preferred path)
          - ``"deterministic"`` — fell back to setup_assistant_reply

        ``tool_calls`` holds confirm-class calls the UI needs to surface;
        auto-class calls that ran server-side are in ``tool_results``.
        ``iterations`` is the count of LLM round-trips for this user turn.
    """
    from nvh.integrations.wizard.context import wizard_context
    from nvh.integrations.wizard.personality import build_system_prompt

    snapshot = wizard_context(home_dir=home_dir)

    # Pull the Wizard tool catalog so the system prompt can teach the model
    # how to request actions. Best-effort — if the registry isn't available
    # (e.g. import order during tests) we just omit the tools block.
    tool_schemas: list[dict[str, Any]] = []
    try:
        from nvh.integrations.wizard.tools import default_registry

        tool_schemas = [t.as_public_dict() for t in default_registry().list_tools()]
    except Exception as exc:
        logger.debug("wizard_chat: tool registry not available (%s)", exc)

    system_prompt = build_system_prompt(snapshot, tools=tool_schemas)
    history = history or []

    # Try the LLM path first.
    try:
        from nvh.api.server import get_engine  # type: ignore

        engine = get_engine()
    except Exception as exc:
        logger.debug("wizard_chat: engine unavailable (%s)", exc)
        engine = None

    fallback_reason: str | None = None

    if engine is not None:
        try:
            from nvh.providers.base import Message

            messages: list[Message] = [Message(role="system", content=system_prompt)]
            for turn in history:
                role = turn.get("role")
                content = turn.get("content", "")
                if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                    messages.append(Message(role=role, content=content))
            messages.append(Message(role="user", content=question))

            await engine.initialize()
            await engine._check_budget()

            # Use the engine's router so we get the same local-first behavior
            # as `nvh ask` — Ollama if healthy, cheapest free cloud otherwise.
            decision = engine.router.route(query=question)
            provider = engine.registry.get(decision.provider)
            if provider is None:
                raise RuntimeError(f"Router picked provider '{decision.provider}' but it isn't registered")

            iterations = 0
            tool_results: list[dict[str, Any]] = []
            pending_confirm_calls: list[dict[str, Any]] = []
            final_text = ""
            response = None

            while iterations < WIZARD_FOLLOWUP_MAX_ITER:
                iterations += 1
                response = await provider.complete(
                    messages=messages,
                    model=decision.model or None,
                    system_prompt=system_prompt,
                    temperature=engine.config.defaults.temperature,
                    max_tokens=engine.config.defaults.max_tokens,
                )
                try:
                    await engine._log_query(response, mode="wizard-chat")
                except Exception:
                    logger.debug("wizard_chat: query logging failed", exc_info=True)

                cleaned_text, tool_calls = _extract_tool_calls(response.content)
                final_text = cleaned_text

                if not tool_calls or not enable_followup:
                    # No tool calls (or follow-up disabled) — done.
                    pending_confirm_calls = tool_calls
                    break

                # Append the assistant's reply (with markers stripped) to the
                # conversation so it sees its own reasoning on the next turn.
                messages.append(Message(role="assistant", content=cleaned_text))

                # Execute auto-class tools, defer confirm-class to the UI.
                ran_any_auto = False
                deferred: list[dict[str, Any]] = []
                for call in tool_calls:
                    result = await _run_auto_tool(call["name"], call.get("arguments", {}))
                    if result.get("deferred_to_user"):
                        deferred.append(call)
                        continue
                    ran_any_auto = True
                    tool_results.append({
                        "name": call["name"],
                        "arguments": call.get("arguments", {}),
                        "result": result,
                    })
                    # Feed the result back to the LLM as a system message so
                    # the next iteration can react to what just happened.
                    messages.append(Message(
                        role="system",
                        content=_format_tool_result_message(call["name"], result),
                    ))

                if not ran_any_auto:
                    # Only confirm-class calls — stop iterating; surface to UI.
                    pending_confirm_calls = deferred
                    break

                # Stash any deferred confirm-class calls for the UI even if
                # we keep iterating on the auto-class side.
                pending_confirm_calls.extend(deferred)

            return {
                "answer": final_text,
                "mode": "llm",
                "used_provider": decision.provider,
                "used_model": decision.model,
                "context": snapshot,
                "tool_calls": pending_confirm_calls,
                "tool_results": tool_results,
                "iterations": iterations,
            }
        except Exception as exc:
            logger.info("wizard_chat: LLM path failed, falling back to deterministic (%s)", exc)
            fallback_reason = str(exc)[:300]
    else:
        fallback_reason = "engine not initialized"

    # Deterministic fallback — guaranteed offline-safe.
    try:
        from nvh.integrations.wizard.setup_agent import setup_assistant_reply

        det = setup_assistant_reply(question, home_dir=home_dir)
        answer = det.get("answer") if isinstance(det, dict) else str(det)
    except Exception as exc:
        logger.warning("wizard_chat: deterministic fallback failed too: %s", exc)
        answer = (
            "Sorry — I couldn't reach a model and the offline helper also "
            "errored. Open Setup → Diagnostics for the latest status, or "
            "share a support snapshot from the wizard."
        )

    return {
        "answer": answer,
        "mode": "deterministic",
        "fallback_reason": fallback_reason,
        "context": snapshot,
        "tool_calls": [],
        "tool_results": [],
        "iterations": 0,
    }


# ────────────────────────────────────────────────────────────────────────────
# Streaming variant — emits incremental events for the UI to render.
# ────────────────────────────────────────────────────────────────────────────

_TOOL_LINE_RE = re.compile(r"^\s*TOOL_CALL\s*:", re.IGNORECASE)


async def _filtered_token_stream(
    stream: AsyncIterator[Any],
) -> AsyncIterator[tuple[str, str]]:
    """Yield ``("token", text)`` for user-visible token deltas, hiding any
    line that starts with ``TOOL_CALL:`` from the live stream. Also yields
    ``("full", accumulated)`` once at the end so the caller can parse markers
    from the complete response.

    The filter buffers up to a newline so a TOOL_CALL line never bleeds into
    the user-visible stream even if the provider chunks mid-line.
    """
    full_parts: list[str] = []
    line_buf = ""
    in_tool_line = False

    async for chunk in stream:
        delta = getattr(chunk, "delta", "") or ""
        if not delta:
            continue
        full_parts.append(delta)
        line_buf += delta
        while "\n" in line_buf:
            line, line_buf = line_buf.split("\n", 1)
            if in_tool_line:
                # We were already inside a multi-chunk TOOL_CALL line; drop it.
                in_tool_line = False
                continue
            if _TOOL_LINE_RE.match(line):
                # Tool-call line — suppress entirely.
                continue
            yield ("token", line + "\n")
        # If we haven't seen a newline yet, check whether the buffer COULD be a
        # tool-call line. If so, hold it until newline. Otherwise, flush.
        if line_buf:
            if _TOOL_LINE_RE.match(line_buf):
                in_tool_line = True
            elif in_tool_line:
                pass  # keep buffering until newline ends the tool line
            elif "TOOL_CALL".startswith(line_buf.strip()) or line_buf.strip().startswith("TOOL_CALL"):
                # Could be start of TOOL_CALL — keep buffering.
                pass
            else:
                yield ("token", line_buf)
                line_buf = ""

    # End of stream — flush any remainder unless it's a tool line.
    if line_buf and not _TOOL_LINE_RE.match(line_buf) and not in_tool_line:
        yield ("token", line_buf)

    yield ("full", "".join(full_parts))


async def wizard_chat_stream(
    question: str,
    *,
    history: list[dict[str, str]] | None = None,
    home_dir: str | Path | None = None,
    enable_followup: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """Stream a Wizard turn as a sequence of events.

    Event types yielded:

      - ``{"type": "iteration", "n": int}``
      - ``{"type": "token", "text": str}``                — user-visible delta
      - ``{"type": "tool_call", "name": str, "arguments": dict}``
      - ``{"type": "tool_result", "name": str, "result": dict}``
      - ``{"type": "confirm_required", "tool_calls": [...]}``  — for UI cards
      - ``{"type": "done", "answer": str, "used_provider": str,
            "used_model": str, "tool_calls": [...], "tool_results": [...],
            "iterations": int}``
      - ``{"type": "error", "error": str, "fallback": str}``  — falls back to
        a deterministic answer; fallback is the plain-text answer.

    Streaming is best-effort: if no engine is reachable, the function yields a
    single ``error`` event with a non-streamed deterministic fallback.
    """
    from nvh.integrations.wizard.context import wizard_context
    from nvh.integrations.wizard.personality import build_system_prompt

    snapshot = wizard_context(home_dir=home_dir)

    tool_schemas: list[dict[str, Any]] = []
    try:
        from nvh.integrations.wizard.tools import default_registry

        tool_schemas = [t.as_public_dict() for t in default_registry().list_tools()]
    except Exception as exc:
        logger.debug("wizard_chat_stream: tool registry not available (%s)", exc)

    system_prompt = build_system_prompt(snapshot, tools=tool_schemas)
    history = history or []

    try:
        from nvh.api.server import get_engine  # type: ignore

        engine = get_engine()
    except Exception as exc:
        logger.debug("wizard_chat_stream: engine unavailable (%s)", exc)
        engine = None

    if engine is None:
        # No engine — fall back to deterministic, emit as a single chunk so the
        # UI can show the answer without a streaming render path.
        det = await _deterministic_fallback(question, home_dir=home_dir)
        yield {"type": "error", "error": "engine not initialized", "fallback": det}
        return

    try:
        from nvh.providers.base import Message

        messages: list[Message] = [Message(role="system", content=system_prompt)]
        for turn in history:
            role = turn.get("role")
            content = turn.get("content", "")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                messages.append(Message(role=role, content=content))
        messages.append(Message(role="user", content=question))

        await engine.initialize()
        await engine._check_budget()

        decision = engine.router.route(query=question)
        provider = engine.registry.get(decision.provider)
        if provider is None:
            raise RuntimeError(
                f"Router picked provider '{decision.provider}' but it isn't registered",
            )

        iterations = 0
        tool_results: list[dict[str, Any]] = []
        pending_confirm_calls: list[dict[str, Any]] = []
        final_text = ""

        while iterations < WIZARD_FOLLOWUP_MAX_ITER:
            iterations += 1
            yield {"type": "iteration", "n": iterations}

            stream_ctx = provider.stream(
                messages=messages,
                model=decision.model or None,
                system_prompt=system_prompt,
                temperature=engine.config.defaults.temperature,
                max_tokens=engine.config.defaults.max_tokens,
            )

            full_text = ""
            async for kind, payload in _filtered_token_stream(stream_ctx):
                if kind == "token":
                    yield {"type": "token", "text": payload}
                else:
                    full_text = payload

            cleaned_text, tool_calls = _extract_tool_calls(full_text)
            final_text = cleaned_text

            if not tool_calls or not enable_followup:
                pending_confirm_calls = tool_calls
                break

            messages.append(Message(role="assistant", content=cleaned_text))

            ran_any_auto = False
            deferred: list[dict[str, Any]] = []
            for call in tool_calls:
                yield {
                    "type": "tool_call",
                    "name": call["name"],
                    "arguments": call.get("arguments", {}),
                }
                result = await _run_auto_tool(call["name"], call.get("arguments", {}))
                if result.get("deferred_to_user"):
                    deferred.append(call)
                    continue
                ran_any_auto = True
                tool_results.append({
                    "name": call["name"],
                    "arguments": call.get("arguments", {}),
                    "result": result,
                })
                yield {"type": "tool_result", "name": call["name"], "result": result}
                messages.append(Message(
                    role="system",
                    content=_format_tool_result_message(call["name"], result),
                ))

            if not ran_any_auto:
                pending_confirm_calls = deferred
                if deferred:
                    yield {"type": "confirm_required", "tool_calls": deferred}
                break

            if deferred:
                pending_confirm_calls.extend(deferred)
                yield {"type": "confirm_required", "tool_calls": deferred}

        yield {
            "type": "done",
            "answer": final_text,
            "used_provider": decision.provider,
            "used_model": decision.model,
            "tool_calls": pending_confirm_calls,
            "tool_results": tool_results,
            "iterations": iterations,
        }
    except Exception as exc:
        logger.info("wizard_chat_stream: LLM path failed (%s)", exc)
        det = await _deterministic_fallback(question, home_dir=home_dir)
        yield {"type": "error", "error": str(exc)[:300], "fallback": det}


async def _deterministic_fallback(question: str, *, home_dir: str | Path | None) -> str:
    try:
        from nvh.integrations.wizard.setup_agent import setup_assistant_reply

        det = setup_assistant_reply(question, home_dir=home_dir)
        return det.get("answer") if isinstance(det, dict) else str(det)
    except Exception as exc:
        logger.warning("wizard_chat_stream: deterministic fallback failed: %s", exc)
        return (
            "Sorry — I couldn't reach a model and the offline helper also "
            "errored. Open Setup → Diagnostics for the latest status."
        )
