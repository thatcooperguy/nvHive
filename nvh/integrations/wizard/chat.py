"""AI Wizard chat — LLM-routed conversation grounded in live workspace state.

This is the upgrade path on top of the existing deterministic
``setup_assistant_reply``. Flow per turn:

  1. Collect live workspace state via ``wizard_context()`` (Wizard-2 context).
  2. Build the system prompt with personality + state via ``build_system_prompt()``.
  3. Route the user message through the engine — uses local Ollama when
     healthy, falls back to any configured cloud provider, and finally
     drops back to the deterministic ``setup_assistant_reply`` if no LLM
     is reachable. The deterministic flow stays the safe net.
  4. Return ``{answer, mode, used_provider, used_model, context, fallback_reason?}``.

Cost is a single completion per turn; conversation history is supplied by the
caller so the function is stateless.

Wizard-3 will add the tool-call layer on top of this — the LLM will be able
to emit structured action requests that the UI renders as one-click cards.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


async def wizard_chat(
    question: str,
    *,
    history: list[dict[str, str]] | None = None,
    home_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Answer a Wizard question with the live-state-grounded LLM path.

    Args:
        question: The user's message for this turn.
        history: Optional prior turns as a list of ``{role, content}`` dicts.
            ``role`` is one of ``"user" | "assistant"``. Empty/None starts
            a fresh conversation.
        home_dir: NVH_HOME override; defaults to the current workspace.

    Returns:
        ``{answer, mode, used_provider?, used_model?, context, fallback_reason?}``
        where ``mode`` is:

          - ``"llm"``  — answered by a routed LLM (preferred path)
          - ``"deterministic"`` — fell back to setup_assistant_reply
    """
    from nvh.integrations.wizard.context import wizard_context
    from nvh.integrations.wizard.personality import build_system_prompt

    snapshot = wizard_context(home_dir=home_dir)
    system_prompt = build_system_prompt(snapshot)
    history = history or []

    # Try the LLM path first.
    try:
        from nvh.api.server import get_engine  # type: ignore

        engine = get_engine()
    except Exception as exc:
        logger.debug("wizard_chat: engine unavailable (%s)", exc)
        engine = None

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

            return {
                "answer": response.content,
                "mode": "llm",
                "used_provider": decision.provider,
                "used_model": decision.model,
                "context": snapshot,
            }
        except Exception as exc:
            logger.info("wizard_chat: LLM path failed, falling back to deterministic (%s)", exc)
            fallback_reason: str | None = str(exc)[:300]
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
    }
