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
import os
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

# Threshold above which a top-1 vault chunk is folded into the system prompt
# verbatim. Higher = fewer, more confident recalls. 0.7 picks up "obvious
# match" notes without false-positives on tangentially related ones.
VAULT_AUTOFOLD_MIN_SCORE = 0.7
VAULT_AUTOFOLD_ENV = "NVH_WIZARD_AUTOFOLD_VAULT"
VAULT_AUTOFOLD_MAX_CHARS = 500


def _autofold_enabled() -> bool:
    return os.environ.get(VAULT_AUTOFOLD_ENV, "1").strip() not in ("0", "false", "no")


async def _auto_fold_vault_chunk(
    question: str,
    *,
    home_dir: str | Path | None,
    min_score: float = VAULT_AUTOFOLD_MIN_SCORE,
) -> str | None:
    """Return a formatted "Relevant note" block if the vault has a strong hit.

    Best-effort: any failure (no vault, embedder down, RAG store empty) returns
    None and lets the chat proceed without recall. We never break a chat turn
    over a recall miss.
    """
    if len(question.strip()) < 10:
        return None
    try:
        from nvh.integrations.rag import ask_vault

        result = await ask_vault(question, top_k=1, home_dir=home_dir)
    except Exception as exc:
        logger.debug("autofold: ask_vault raised (%s)", exc)
        return None
    if not result.get("ok"):
        return None
    chunks = result.get("chunks") or []
    if not chunks:
        return None
    top = chunks[0]
    score = float(top.get("score") or 0.0)
    if score < min_score:
        return None
    source = Path(str(top.get("source", "vault note"))).name
    text = str(top.get("text", "")).strip()[:VAULT_AUTOFOLD_MAX_CHARS]
    if not text:
        return None
    return f"Relevant note: {source} (score {score:.2f}) — {text}"


async def _persist_wizard_turn(
    conversation_id: str | None,
    *,
    user_question: str,
    assistant_text: str,
    provider: str = "",
    model: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Write the user + assistant pair to the conversations store.

    Best-effort: if the conversation doesn't exist or the repo is unreachable,
    we log and continue. Persistence is a feature, not a correctness invariant.
    """
    if not conversation_id:
        return
    try:
        from nvh.storage import repository as repo

        await repo.add_message(
            conversation_id=conversation_id,
            role="user",
            content=user_question,
            provider=provider,
            model=model,
        )
        # Append metadata as a fenced JSON tail so the conversations store's
        # plain text content column still renders cleanly in the legacy UI but
        # downstream readers can parse the tool trace.
        content = assistant_text
        if metadata:
            try:
                content = f"{assistant_text}\n\n<!-- wizard-meta: {json.dumps(metadata, default=str)} -->"
            except Exception:
                content = assistant_text
        await repo.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=content,
            provider=provider,
            model=model,
        )
    except Exception as exc:
        logger.info("wizard: persistence skipped (%s)", exc)

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


def _apply_profile(system_prompt: str, profile_name: str | None, home_dir: str | Path | None) -> tuple[str, str | None, str | None]:
    """Apply an agent profile's overrides on top of the global persona.

    Returns ``(system_prompt, preferred_provider, preferred_model)``. Missing
    or unknown profile names fall back to the unchanged system prompt — the
    Wizard's default persona — and ``(None, None)`` for routing.
    """
    if not profile_name or profile_name == "wizard":
        return system_prompt, None, None
    try:
        from nvh.integrations.wizard.profiles import get_profile

        profile = get_profile(profile_name, home_dir=home_dir)
    except Exception as exc:
        logger.debug("profile lookup failed (%s); falling back to default", exc)
        return system_prompt, None, None
    if profile is None:
        return system_prompt, None, None
    persona_addon = profile.system_prompt.strip()
    if persona_addon:
        system_prompt = f"{system_prompt}\n\n--- Agent profile: {profile.title} ---\n{persona_addon}\n--- end profile ---"
    return system_prompt, profile.provider or None, profile.model or None


async def wizard_chat(
    question: str,
    *,
    history: list[dict[str, str]] | None = None,
    home_dir: str | Path | None = None,
    enable_followup: bool = True,
    conversation_id: str | None = None,
    profile: str | None = None,
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
    from nvh.integrations.wizard.findings import derive_findings
    from nvh.integrations.wizard.personality import build_system_prompt

    snapshot = wizard_context(home_dir=home_dir)
    findings = derive_findings(snapshot)

    # Pull the Wizard tool catalog so the system prompt can teach the model
    # how to request actions. Best-effort — if the registry isn't available
    # (e.g. import order during tests) we just omit the tools block.
    tool_schemas: list[dict[str, Any]] = []
    try:
        from nvh.integrations.wizard.tools import default_registry

        tool_schemas = [t.as_public_dict() for t in default_registry().list_tools()]
    except Exception as exc:
        logger.debug("wizard_chat: tool registry not available (%s)", exc)

    # Auto-fold the top vault chunk if the user's question matches anything
    # they've already written down. Free recall — saves a tool round-trip.
    vault_recall: str | None = None
    if enable_followup and _autofold_enabled():
        vault_recall = await _auto_fold_vault_chunk(question, home_dir=home_dir)

    system_prompt = build_system_prompt(
        snapshot, tools=tool_schemas, vault_recall=vault_recall, findings=findings,
    )
    system_prompt, profile_provider, profile_model = _apply_profile(system_prompt, profile, home_dir)
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
            # Profile override: if the profile names a specific provider/model
            # and it's actually registered, prefer it over the router pick.
            if profile_provider:
                cand = engine.registry.get(profile_provider)
                if cand is not None:
                    decision.provider = profile_provider
                    if profile_model:
                        decision.model = profile_model
            provider = engine.registry.get(decision.provider)
            if provider is None:
                raise RuntimeError(f"Router picked provider '{decision.provider}' but it isn't registered")

            iterations = 0
            tool_results: list[dict[str, Any]] = []
            pending_confirm_calls: list[dict[str, Any]] = []
            final_text = ""
            response = None
            # Accumulate cost/latency across the (possibly multi-iteration)
            # follow-up loop so the UI can show the *full* turn cost, not just
            # the last iteration's slice.
            total_cost_usd: float = 0.0
            total_latency_ms: int = 0
            total_input_tokens: int = 0
            total_output_tokens: int = 0
            fallback_from: str | None = None

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
                # Roll up the meter so the chat footer can render a single
                # honest "1.2s · 500 tokens · $0.001" badge for the turn.
                try:
                    total_cost_usd += float(response.cost_usd or 0)
                except Exception:
                    pass
                total_latency_ms += int(response.latency_ms or 0)
                if response.usage is not None:
                    total_input_tokens += int(response.usage.input_tokens or 0)
                    total_output_tokens += int(response.usage.output_tokens or 0)
                if getattr(response, "fallback_from", None) and not fallback_from:
                    fallback_from = response.fallback_from

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

            await _persist_wizard_turn(
                conversation_id,
                user_question=question,
                assistant_text=final_text,
                provider=decision.provider or "",
                model=decision.model or "",
                metadata={
                    "source": "wizard",
                    "iterations": iterations,
                    "tool_calls": pending_confirm_calls,
                    "tool_results": tool_results,
                    "cost_usd": total_cost_usd,
                    "latency_ms": total_latency_ms,
                },
            )
            return {
                "answer": final_text,
                "mode": "llm",
                "used_provider": decision.provider,
                "used_model": decision.model,
                "context": snapshot,
                "tool_calls": pending_confirm_calls,
                "tool_results": tool_results,
                "iterations": iterations,
                "cost_usd": total_cost_usd,
                "latency_ms": total_latency_ms,
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "fallback_from": fallback_from,
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

    await _persist_wizard_turn(
        conversation_id,
        user_question=question,
        assistant_text=answer,
        metadata={"source": "wizard", "mode": "deterministic", "fallback_reason": fallback_reason},
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
    conversation_id: str | None = None,
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
    from nvh.integrations.wizard.findings import derive_findings
    from nvh.integrations.wizard.personality import build_system_prompt

    snapshot = wizard_context(home_dir=home_dir)
    findings = derive_findings(snapshot)

    tool_schemas: list[dict[str, Any]] = []
    try:
        from nvh.integrations.wizard.tools import default_registry

        tool_schemas = [t.as_public_dict() for t in default_registry().list_tools()]
    except Exception as exc:
        logger.debug("wizard_chat_stream: tool registry not available (%s)", exc)

    vault_recall: str | None = None
    if enable_followup and _autofold_enabled():
        vault_recall = await _auto_fold_vault_chunk(question, home_dir=home_dir)

    system_prompt = build_system_prompt(
        snapshot, tools=tool_schemas, vault_recall=vault_recall, findings=findings,
    )
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

        await _persist_wizard_turn(
            conversation_id,
            user_question=question,
            assistant_text=final_text,
            provider=decision.provider or "",
            model=decision.model or "",
            metadata={
                "source": "wizard",
                "iterations": iterations,
                "tool_calls": pending_confirm_calls,
                "tool_results": tool_results,
                "streamed": True,
            },
        )
        yield {
            "type": "done",
            "answer": final_text,
            "used_provider": decision.provider,
            "used_model": decision.model,
            "tool_calls": pending_confirm_calls,
            "tool_results": tool_results,
            "iterations": iterations,
            # Streaming providers don't always emit usage in the same place
            # complete() does. The UI hides these fields when 0, so a missing
            # meter is graceful — but we surface what we know.
            "cost_usd": 0.0,
            "latency_ms": 0,
            "fallback_from": None,
        }
    except Exception as exc:
        logger.info("wizard_chat_stream: LLM path failed (%s)", exc)
        det = await _deterministic_fallback(question, home_dir=home_dir)
        await _persist_wizard_turn(
            conversation_id,
            user_question=question,
            assistant_text=det,
            metadata={"source": "wizard", "mode": "deterministic-stream-fallback"},
        )
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
