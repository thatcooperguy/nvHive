"""AI Wizard chat — LLM-routed conversation grounded in live workspace state.

This is the upgrade path on top of the existing deterministic
``setup_assistant_reply``. Flow per turn (``_prepare_turn`` does 1–3 once for
both the blocking and the streaming entry point):

  1. Collect live workspace state via ``wizard_context()`` (Wizard-2 context).
  2. Let the concierge pick a hidden specialist when nothing is pinned
     (``nvh.integrations.wizard.concierge``), resolve its ``AgentProfile``
     into the overrides this turn enforces, and build the system prompt with
     personality + state via ``build_system_prompt()``.
  3. Route the user message through the engine — uses local Ollama when
     healthy, falls back to any configured cloud provider, and finally
     drops back to the deterministic ``setup_assistant_reply`` if no LLM
     is reachable. The router itself only knows whether Ollama is
     *registered*, so a decision for a local daemon the probe says is down
     is re-routed to a registered cloud provider first
     (:func:`_route_around_dead_local_provider`). The deterministic flow
     stays the safe net.
  4. If the LLM emitted ``TOOL_CALL:`` markers, **run the auto-class tools
     server-side** (Wizard-5 follow-up loop), append the results to the
     conversation as system messages, and give the LLM one more turn to
     react. Repeats up to ``WIZARD_FOLLOWUP_MAX_ITER`` times so the model
     can chain a small read → think → act → react sequence in a single
     user turn. Confirm-class tools never auto-execute; they're surfaced
     to the caller for UI confirmation.
  5. Return ``{answer, mode, used_provider, used_model, context,
     tool_calls, tool_results, deferred_tool_calls, iterations,
     fallback_reason, used_profile, profile_reason}``.

An ``AgentProfile`` (``profile=`` argument, or the concierge's pick) is
enforced on both paths: its ``tools_allowed`` whitelist filters the tool
catalog shown to the model and gates ``_run_auto_tool``; ``temperature`` /
``max_tokens`` override the engine defaults; ``max_cost_usd_per_turn`` aborts
the follow-up loop; its provider/model pin is advisory (honoured when the
provider is *available* — see :func:`_provider_available`; a provider pinned
without a model gets *its own* default model, never the router's
provider-specific pick).

"Available" is registered *and*, for the local provider, reachable.
``ProviderRegistry.setup_from_config`` registers every enabled provider from
config whether or not it answers and never unregisters one, so
``registry.has("ollama")`` only says Ollama is configured. The local daemon
is therefore probed (an *async* ``GET /api/tags``,
:data:`LOCAL_PROBE_TIMEOUT_S`, never blocking the event loop) before a turn
is routed to it; cloud providers are registry membership only — no network.
The answer is cached per base URL — a positive one for
:data:`LOCAL_PROBE_TTL_S`, a negative one only for
:data:`LOCAL_PROBE_NEGATIVE_TTL_S` so "start it and ask again" works within
seconds — and forgotten when a tool that touches the daemon runs
(:data:`LOCAL_PROBE_INVALIDATING_TOOLS`) or a completion on the local
provider fails.

``local-only`` profiles never run on a cloud provider. What happens when
their local provider is unavailable depends on who chose them: an explicit
``/agent`` pin refuses deterministically (the user asked for that
specialist), while a concierge choice is demoted for the turn — the general
Wizard answers, ``used_profile`` is ``None`` and ``profile_reason`` notes
``local-only specialist unavailable: Ollama not running`` (or ``not
configured`` when the provider is not registered at all — the refusal and
the note name the actual problem and its fix). The user pinned nothing, so
hidden routing must never turn an ordinary question into a refusal.

Concierge-chosen specialists keep :data:`WIZARD_CORE_AUTO_TOOLS` (read-only
diagnostics) on top of their whitelist, unless the profile carries the
``strict-tools`` tag — restriction-defined personas (core ``vault-rag``:
"never call web_search") keep their whitelist exactly.

Fallback shapes — the two paths agree on attribution and reason:

  - non-stream: ``{answer, mode: "deterministic", fallback_reason, context,
    tool_calls, tool_results, deferred_tool_calls, iterations,
    used_profile, profile_reason}``;
  - stream: ``{type: "error", error, fallback, fallback_reason,
    used_profile, profile_reason}`` (tool trace events were already emitted
    live, ``confirm_required`` right before the error).

``fallback_reason`` is the LLM error text, ``"engine not initialized"`` or
:data:`LOCAL_ONLY_FALLBACK_REASON`. A deterministic answer is attributed to
no specialist (``used_profile None``) — the offline helper is not the
specialist — except the local-only refusal of an explicit pin, where the
specialist itself declined. ``profile_reason`` is independent of that
attribution: whenever the concierge ran it travels on every path (LLM
answer, deterministic fallback, stream ``error`` event, persisted meta) —
including the demotion note of a local-only specialist and the "general
Wizard: no specialist matched" of a plain turn — and is ``None`` only for
an explicit pin, where no selection ran. The persisted ``wizard-meta``
mirrors the envelope (``_TurnSetup.meta_for``), and auto tools an earlier
iteration executed travel with the fallback instead of being dropped.

Three buckets leave the loop without executing:

  - ``tool_calls`` / ``confirm_required`` — confirm-class calls the UI must
    ask the user about; a ``privileged`` one carries its red card
    (``privileged``, the registry's dry-run ``plan``, the ``approval_token``
    the confirmed execute must send back — see :func:`_surfaced_call`), and
    what the user then did comes back on the next turn's history as
    ``tool_results`` (:func:`_history_tool_results_message`);
  - ``deferred_tool_calls`` — auto-class calls the loop did *not* run
    (``max_iterations=1``, follow-up disabled, cost ceiling), each with a
    ``reason``; the UI shows them and never auto-runs them;
  - whitelist refusals — recorded in ``tool_results`` (and emitted as
    ``tool_result`` events) with ``not_allowed=True``; never executed and
    never counted as an executed tool.

Cost is one or more completions per user turn; conversation history is
supplied by the caller so the function is stateless.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Collection
from dataclasses import dataclass, field, replace
from decimal import Decimal
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

# Auto-class tools the general Wizard keeps when the *concierge* (not the
# user) routed the turn to a specialist. Routing is invisible to the user, so
# it must never take away the ability to look at the box: a hidden
# specialist's whitelist is unioned with these. Read-only / diagnostic only —
# never a tool that changes the workspace (``repair_workspace``) or leaves it
# (``web_search``): restriction-defined personas exist to forbid exactly
# those, and the union must not re-enable them. An explicit ``/agent`` pin
# keeps the specialist's strict whitelist.
WIZARD_CORE_AUTO_TOOLS: frozenset[str] = frozenset({
    "diagnose", "refresh_models", "rag_ask_vault",
})

# Profiles carrying this tag keep their whitelist exactly, even when the
# concierge chose them: no core-tool union. Set on built-ins whose prompt
# forbids a tool (core ``vault-rag``: "Never call web_search").
STRICT_TOOLS_TAG = "strict-tools"

# Profiles carrying this tag must never be routed to a cloud provider: when
# their local provider is not registered, an explicit pin answers
# deterministically and a concierge choice is demoted to the general Wizard.
LOCAL_ONLY_TAG = "local-only"
# The provider a local-only profile means when it does not pin one itself.
LOCAL_PROVIDER = "ollama"
LOCAL_ONLY_FALLBACK_REASON = "profile_local_only_provider_unavailable"
# Reachability probe of the local provider (see ``_provider_available``):
# one async ``GET /api/tags`` with this timeout, remembered per base URL so a
# multi-iteration turn or a quick follow-up never probes twice. A positive
# answer is trusted for LOCAL_PROBE_TTL_S; a negative one only for
# LOCAL_PROBE_NEGATIVE_TTL_S — the refusal tells the user to start the daemon
# and ask again, and that has to work within seconds, not half a minute. The
# cache is also dropped when a Wizard tool that (re)starts or re-queries the
# daemon runs (LOCAL_PROBE_INVALIDATING_TOOLS) and when a completion on the
# local provider fails, so a stale positive never outlives the daemon.
LOCAL_PROBE_TIMEOUT_S = 1.5
LOCAL_PROBE_TTL_S = 30.0
LOCAL_PROBE_NEGATIVE_TTL_S = 5.0
LOCAL_PROBE_INVALIDATING_TOOLS: frozenset[str] = frozenset({"refresh_models", "repair_workspace"})

# ``reason`` values on ``deferred_tool_calls`` entries — auto-class calls the
# loop did not execute. The UI renders them as a trace; it never runs them.
DEFER_MAX_ITERATIONS = "max_iterations"
DEFER_FOLLOWUP_DISABLED = "followup_disabled"
DEFER_COST_CEILING = "cost_ceiling"


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


def _format_tool_result_message(name: str, result: Any) -> str:
    """Render a completed tool result as a system message the model can read.

    Compact JSON keeps the prompt small; the model already knows the tool's
    schema from the system prompt. Secrets are redacted *before* the cut to
    ``TOOL_RESULT_CHARS`` (the one window ``fit_tool_window`` sizes privileged
    results to), so a key split by the cut can never slip past the patterns.
    """
    from nvh.core.agent_guardrails import redact_secrets
    from nvh.integrations.wizard.tools import TOOL_RESULT_CHARS

    try:
        payload = json.dumps(result, default=str)
    except Exception:
        payload = str(result)
    return f"TOOL_RESULT {name}: {redact_secrets(payload)[:TOOL_RESULT_CHARS]}"


#: A history ``tool_results`` entry's ``summary`` is cut here (the WebUI cuts
#: at the same length before sending) and at most this many entries per turn
#: reach the prompt.
HISTORY_TOOL_SUMMARY_CHARS = 300
HISTORY_TOOL_RESULTS_MAX = 8


def _history_tool_results_message(entry: dict[str, Any]) -> str | None:
    """``TOOL_RESULT`` lines for the cards a prior assistant turn's user acted on, or ``None``.

    Confirm and privileged cards run in the WebUI (``/v1/wizard/tools/execute``)
    after the turn that proposed them has ended, so the model never saw the
    outcome. The next turn's history entry carries it as ``tool_results:
    [{name, ok, summary}]`` and this renders it in the same ``TOOL_RESULT``
    shape the in-turn loop uses — redacted, each summary cut to
    :data:`HISTORY_TOOL_SUMMARY_CHARS`. A ``needs a terminal:`` summary is
    how the model learns which exact command to repeat.
    """
    raw = entry.get("tool_results")
    if not isinstance(raw, list) or not raw:
        return None
    from nvh.core.agent_guardrails import redact_secrets

    lines: list[str] = []
    for item in raw[:HISTORY_TOOL_RESULTS_MAX]:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        summary = item.get("summary")
        text = redact_secrets(summary.strip())[:HISTORY_TOOL_SUMMARY_CHARS] if isinstance(summary, str) else ""
        payload = json.dumps({"ok": item.get("ok") is True, "summary": text})
        lines.append(f"TOOL_RESULT {name.strip()[:64]}: {payload}")
    return "\n".join(lines) or None


def _tool_not_allowed(name: str, profile_name: str | None) -> dict[str, Any]:
    """Refusal payload for a tool outside the active profile's whitelist."""
    return {
        "ok": False,
        "error": f"tool '{name}' is not allowed for profile '{profile_name}'",
        "not_allowed": True,
    }


def _filter_tool_schemas(
    schemas: list[dict[str, Any]], tools_allowed: Collection[str] | None,
) -> list[dict[str, Any]]:
    """Restrict the tool catalog shown to the model to the profile whitelist.

    ``None`` means "no whitelist" (all tools); an empty collection means the
    model sees no tools at all.
    """
    if tools_allowed is None:
        return schemas
    return [s for s in schemas if s.get("name") in tools_allowed]


def _split_by_whitelist(
    tool_calls: list[dict[str, Any]],
    tools_allowed: Collection[str] | None,
    profile_name: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition parsed tool calls into ``(allowed, refused_results)``.

    ``refused_results`` are ready-made ``tool_results`` entries so a call the
    profile forbids is recorded honestly instead of leaking into the
    ``tool_calls`` / ``confirm_required`` payload the UI would act on.
    """
    if tools_allowed is None:
        return tool_calls, []
    allowed: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for call in tool_calls:
        if call["name"] in tools_allowed:
            allowed.append(call)
        else:
            refused.append({
                "name": call["name"],
                "arguments": call.get("arguments", {}),
                "result": _tool_not_allowed(call["name"], profile_name),
            })
    return allowed, refused


def _split_by_safety_class(
    tool_calls: list[dict[str, Any]], registry: Any | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition calls into ``(needs_confirmation, auto_class)``.

    Unknown tools count as needing confirmation — the UI already treats a
    tool it can't look up that way, so nothing unrecognised ever runs
    silently. Without a registry nothing can be classified, so everything
    needs confirmation.
    """
    if registry is None:
        return list(tool_calls), []
    confirm: list[dict[str, Any]] = []
    auto: list[dict[str, Any]] = []
    for call in tool_calls:
        tool = registry.get(call["name"])
        if tool is not None and tool.safety_class == "auto":
            auto.append(call)
        else:
            confirm.append(call)
    return confirm, auto


def _same_call(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Same tool, same arguments — whatever else (plan, token) one of them carries."""
    return a.get("name") == b.get("name") and (a.get("arguments") or {}) == (b.get("arguments") or {})


async def _surfaced_call(call: dict[str, Any], registry: Any | None) -> dict[str, Any]:
    """The dict the UI gets for one confirm-bucket call.

    Confirm-class (and unknown) calls pass through as ``{name, arguments}``.
    A ``privileged`` call is surfaced with what its red card needs —
    ``privileged: True``, the registry's dry-run ``plan`` and the
    ``approval_token`` / ``approval_expires_at`` the confirmed execute must
    bring back — read off the very card ``execute(confirmed=False)`` returns,
    so the registry stays the one place that plans, mints tokens and re-reads
    the kill switch (a switched-off tier surfaces ``disabled: True`` and its
    refusal instead of a plan). None of this reaches the model: surfaced
    calls go to the UI only.
    """
    tool = registry.get(call["name"]) if registry is not None else None
    if tool is None or tool.safety_class != "privileged":
        return call
    arguments = call.get("arguments", {})
    surfaced: dict[str, Any] = {"name": call["name"], "arguments": arguments, "privileged": True}
    try:
        card = await registry.execute(call["name"], arguments=arguments, confirmed=False)
    except Exception as exc:
        logger.warning("privileged card for '%s' not built: %s", call["name"], exc)
        return surfaced
    if card.get("disabled"):
        surfaced["disabled"] = True
        surfaced["error"] = card.get("error")
        return surfaced
    for key in ("plan", "approval_token", "approval_expires_at"):
        if key in card:
            surfaced[key] = card[key]
    return surfaced


async def _surface_confirm_calls(
    pending: list[dict[str, Any]], calls: list[dict[str, Any]], registry: Any | None,
) -> None:
    """Append confirm-bucket calls the UI hasn't seen yet, privileged ones with their card.

    Deferred calls accumulate across follow-up iterations (a confirm-class
    call surfaced on iteration 1 must still be in ``done.tool_calls`` after
    iteration 2 answered); a model re-emitting the same call after seeing an
    auto-tool result must not produce a duplicate card, so identity is the
    tool name plus arguments, not the surfaced dict.
    """
    for call in calls:
        if not any(_same_call(call, seen) for seen in pending):
            pending.append(await _surfaced_call(call, registry))


def _persistable_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Surfaced calls without their approval tokens, for the conversation store.

    A stored token is of no use to a reload (cards are not rehydrated) and a
    fifteen-minute, single-use secret has no business in a file.
    """
    return [
        {k: v for k, v in call.items() if k not in ("approval_token", "approval_expires_at")}
        if isinstance(call, dict) else call
        for call in calls
    ]


def _cost_ceiling_reached(ceiling: float | None, total_cost_usd: float) -> bool:
    """True when a profile's per-turn ceiling is set and the running cost hit it."""
    return ceiling is not None and total_cost_usd >= ceiling


def _tool_result_event(entry: dict[str, Any]) -> dict[str, Any]:
    """Stream event for one ``tool_results`` entry (executed or refused)."""
    return {"type": "tool_result", "name": entry["name"], "result": entry["result"]}


async def _run_auto_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    tools_allowed: Collection[str] | None = None,
    profile_name: str | None = None,
    registry: Any | None = None,
) -> dict[str, Any]:
    """Execute a single auto-class tool.

    Returns ``{ok, result?, error?, safety_class}``. Confirm-class tools
    are *not* auto-executed — the caller surfaces them to the UI instead.
    When ``tools_allowed`` is given (the active profile's whitelist), a tool
    outside it is refused with ``not_allowed=True`` and never executed,
    whatever its safety class: a forbidden confirm-class tool must not reach
    the UI as a confirm card either. ``registry`` is the turn's already-built
    ``WizardToolRegistry``; it is built on demand only when omitted.
    """
    if registry is None:
        try:
            from nvh.integrations.wizard.tools import default_registry

            registry = default_registry()
        except Exception as exc:
            logger.debug("auto-tool: registry not available (%s)", exc)
            return {"ok": False, "error": "tool registry unavailable"}

    tool = registry.get(name)
    if tool is None:
        return {"ok": False, "error": f"unknown tool: {name}"}
    if tools_allowed is not None and name not in tools_allowed:
        return _tool_not_allowed(name, profile_name)
    if tool.safety_class != "auto":
        # Defer to UI — never silently auto-execute a confirm-class tool.
        return {
            "ok": False,
            "deferred_to_user": True,
            "safety_class": tool.safety_class,
            "error": "confirm-class — surfaced to user instead of auto-running",
        }
    return await registry.execute(name, arguments=arguments, confirmed=True)


# ────────────────────────────────────────────────────────────────────────────
# Profiles — resolution + the overrides a turn enforces
# ────────────────────────────────────────────────────────────────────────────


def _load_profiles(home_dir: str | Path | None) -> tuple[Any, ...]:
    """The profile catalog, loaded once per turn (concierge + resolution share it).

    A failing profile store yields an empty catalog: the chat must never die
    on a lookup, and with no catalog the general Wizard answers.
    """
    try:
        from nvh.integrations.wizard.profiles import list_profiles

        return tuple(list_profiles(home_dir=home_dir))
    except Exception as exc:
        logger.debug("profile catalog unavailable (%s); default Wizard only", exc)
        return ()


def _resolve_profile(
    profile_name: str | None,
    home_dir: str | Path | None,
    *,
    profiles: Collection[Any] | None = None,
) -> Any:
    """Look up a non-default profile; None for the default Wizard, unknown
    names, or a failing profile store (the chat must never die on lookup).

    ``profiles`` is an already-loaded catalog (see :func:`_load_profiles`);
    without it the store is consulted directly.
    """
    if not profile_name or profile_name == "wizard":
        return None
    try:
        if profiles is not None:
            return next((p for p in profiles if p.name == profile_name), None)
        from nvh.integrations.wizard.profiles import get_profile

        return get_profile(profile_name, home_dir=home_dir)
    except Exception as exc:
        logger.debug("profile lookup failed (%s); falling back to default", exc)
        return None


def _apply_prompt_template(question: str, profile: Any | None) -> str:
    """Wrap the user's message in the resolved profile's ``prompt_template``.

    Routing and vault recall still see the raw question; only the message
    handed to the model is rendered. ``None`` (general Wizard) is identity.
    """
    if profile is None:
        return question
    return profile.render_prompt(question)


@dataclass(frozen=True)
class ProfileOverrides:
    """Everything an ``AgentProfile`` changes about one chat turn, resolved once.

    ``None`` in an optional field means "inherit the engine / router default".
    ``tools_allowed=None`` means every registry tool is available; a set
    (even an empty one) is a hard whitelist. The default instance is the
    no-profile / default-``wizard`` case. ``provider`` / ``model`` are
    advisory pins (see :func:`_pin_provider`); ``local_only`` marks a profile
    that must never run on a cloud provider; ``strict_tools`` marks one whose
    whitelist is never widened, even by concierge routing.
    """

    profile_name: str | None = None
    title: str = ""
    persona_addon: str = ""
    provider: str | None = None
    model: str | None = None
    cost_ceiling_usd: float | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    tools_allowed: frozenset[str] | None = None
    local_only: bool = False
    strict_tools: bool = False

    def apply_to_prompt(self, system_prompt: str) -> str:
        """Append the profile persona to the global Wizard system prompt."""
        if not self.persona_addon:
            return system_prompt
        return (
            f"{system_prompt}\n\n--- Agent profile: {self.title} ---\n"
            f"{self.persona_addon}\n--- end profile ---"
        )

    def sampling_params(self, engine: Any) -> tuple[float, int]:
        """``(temperature, max_tokens)`` — profile override, else engine default."""
        defaults = engine.config.defaults
        temperature = self.temperature if self.temperature is not None else defaults.temperature
        max_tokens = self.max_tokens if self.max_tokens is not None else defaults.max_tokens
        return temperature, max_tokens

    def with_core_tools(self) -> ProfileOverrides:
        """The whitelist widened with :data:`WIZARD_CORE_AUTO_TOOLS`.

        Applied when the concierge — not the user — picked this profile, so
        hidden routing never removes the general Wizard's ability to diagnose.
        No whitelist stays no whitelist, and a ``strict-tools`` profile keeps
        its whitelist exactly: its persona is defined by what it must not do.
        """
        if self.tools_allowed is None or self.strict_tools:
            return self
        return replace(self, tools_allowed=self.tools_allowed | WIZARD_CORE_AUTO_TOOLS)


def _overrides_for(profile: Any | None) -> ProfileOverrides:
    """The overrides one resolved ``AgentProfile`` imposes (default for None)."""
    if profile is None:
        return ProfileOverrides()
    ceiling = profile.max_cost_usd_per_turn
    if ceiling is not None and ceiling <= 0:
        ceiling = None
    tools_allowed = profile.tools_allowed  # normalised by the loader: list[str] | None
    tags = frozenset(profile.tags or ())
    return ProfileOverrides(
        profile_name=profile.name,
        title=profile.title,
        persona_addon=profile.system_prompt.strip(),
        provider=profile.provider or None,
        model=profile.model or None,
        cost_ceiling_usd=ceiling,
        temperature=profile.temperature,
        max_tokens=profile.max_tokens,
        tools_allowed=None if tools_allowed is None else frozenset(tools_allowed),
        local_only=LOCAL_ONLY_TAG in tags,
        strict_tools=STRICT_TOOLS_TAG in tags,
    )


def _resolve_profile_overrides(
    profile_name: str | None,
    home_dir: str | Path | None,
    *,
    profiles: Collection[Any] | None = None,
) -> ProfileOverrides:
    """Resolve a profile name into the overrides the chat paths enforce.

    Missing or unknown names (and the default ``wizard`` profile) return a
    default ``ProfileOverrides`` — no persona addon, no overrides, all tools.
    """
    return _overrides_for(_resolve_profile(profile_name, home_dir, profiles=profiles))


def _local_label(provider: str) -> str:
    """User-facing name of a provider (``"Ollama"`` for the default local one)."""
    return "Ollama" if provider == LOCAL_PROVIDER else provider


def _pinned_provider(prof: ProfileOverrides) -> str | None:
    """The provider a profile pins — the default local one for a bare ``local-only`` profile."""
    return prof.provider or (LOCAL_PROVIDER if prof.local_only else None)


# Reachability of the local daemon, per base URL: ``{url: (monotonic, ok)}``.
_LOCAL_PROBE_CACHE: dict[str, tuple[float, bool]] = {}


def _reset_local_probe_cache() -> None:
    """Forget every cached local-provider probe (tests; after starting the daemon)."""
    _LOCAL_PROBE_CACHE.clear()


def _local_provider_base_url(engine: Any) -> str:
    """The local daemon's base URL the engine would talk to.

    ``providers.ollama.base_url`` from the engine's config when it is set,
    else the ``OLLAMA_BASE_URL`` / ``OLLAMA_HOST`` override or the default —
    the same resolution the Ollama adapter itself uses. Never raises.
    """
    from nvh.utils.ollama import ollama_base_url

    configured: str | None = None
    try:
        pconf = engine.config.providers.get(LOCAL_PROVIDER)
        value = getattr(pconf, "base_url", None)
        if isinstance(value, str) and value.strip():
            configured = value
    except Exception as exc:
        logger.debug("wizard: no configured base_url for %r (%s)", LOCAL_PROVIDER, exc)
    return ollama_base_url(configured)


async def _probe_local_provider(base_url: str) -> bool:
    """One network probe of the local daemon: ``GET /api/tags`` answered 200.

    Delegates to :func:`nvh.utils.ollama.probe_installed_models_async`
    (``None`` = unreachable) with :data:`LOCAL_PROBE_TIMEOUT_S`. A coroutine
    on purpose: both chat paths run on uvicorn's event loop, and the blocking
    ``httpx.get`` twin would stall every other request in the process for up
    to the timeout. Tests replace this.
    """
    from nvh.utils.ollama import probe_installed_models_async

    return await probe_installed_models_async(base_url, timeout=LOCAL_PROBE_TIMEOUT_S) is not None


def _probe_ttl(reachable: bool) -> float:
    """How long a cached probe answer is trusted — short when it said "down"."""
    return LOCAL_PROBE_TTL_S if reachable else LOCAL_PROBE_NEGATIVE_TTL_S


async def _local_provider_reachable(base_url: str) -> bool:
    """Cached answer to "is the local daemon at ``base_url`` up?".

    A positive probe is remembered for :data:`LOCAL_PROBE_TTL_S`, a negative
    one only for :data:`LOCAL_PROBE_NEGATIVE_TTL_S` (the user was told to
    start the daemon and ask again); a probe that raises counts as
    unreachable. Never raises, never blocks the event loop.
    """
    cached = _LOCAL_PROBE_CACHE.get(base_url)
    if cached is not None and time.monotonic() - cached[0] < _probe_ttl(cached[1]):
        return cached[1]
    try:
        reachable = bool(await _probe_local_provider(base_url))
    except Exception as exc:
        logger.debug("wizard: probe of %s failed (%s); treating as unreachable", base_url, exc)
        reachable = False
    # Stamped when the probe *finished*: a probe that ran into
    # LOCAL_PROBE_TIMEOUT_S would otherwise enter the cache already that far
    # into its TTL (30% of the negative one), so "down" answers expire early
    # and the daemon gets re-probed sooner than the TTL promises.
    _LOCAL_PROBE_CACHE[base_url] = (time.monotonic(), reachable)
    return reachable


async def _provider_unavailability(engine: Any, name: str) -> str | None:
    """Why ``name`` cannot take this turn — ``None`` when it can.

    ``"not registered"`` when the registry does not know it (or cannot be
    asked); ``"not running"`` when it is the local provider and its daemon
    does not answer :func:`_probe_local_provider`. Registration alone is not
    enough for the local provider: ``ProviderRegistry.setup_from_config``
    registers every enabled provider from config regardless of reachability
    and never unregisters it (only the engine's auto-detect path is
    reachability-gated), so a box with Ollama in config but not running
    would otherwise route local-only specialists to a dead daemon. Cloud
    providers are registry membership only — no network. Never raises.
    """
    try:
        registered = bool(engine.registry.has(name))
    except Exception as exc:
        logger.debug("wizard: registry probe for %r failed (%s)", name, exc)
        return "not registered"
    if not registered:
        return "not registered"
    if name == LOCAL_PROVIDER and not await _local_provider_reachable(_local_provider_base_url(engine)):
        return "not running"
    return None


async def _provider_available(engine: Any, name: str) -> bool:
    """Registered and — for the local provider — reachable. Never raises.

    The one predicate both the local-only demotion/refusal and the advisory
    provider pin consult (see :func:`_provider_unavailability`).
    """
    return await _provider_unavailability(engine, name) is None


async def _missing_local_provider(prof: ProfileOverrides, engine: Any | None) -> tuple[str, str] | None:
    """``(provider, why)`` for the local provider a ``local-only`` profile
    needs when it is *not* available.

    ``None`` when the profile is not local-only, its provider is available
    (registered and reachable, :func:`_provider_available`), or there is no
    engine to ask (the turn answers deterministically anyway). ``why`` is
    :func:`_provider_unavailability`'s verdict so the demotion note can say
    what is actually wrong. Never raises.
    """
    if not prof.local_only or engine is None:
        return None
    provider = _pinned_provider(prof) or LOCAL_PROVIDER
    why = await _provider_unavailability(engine, provider)
    return None if why is None else (provider, why)


# ``why`` values of :func:`_provider_unavailability`, in the user's words: an
# unregistered provider is a config fact ("not configured"), and each problem
# has its own fix — telling someone to start a daemon they never enabled
# sends them down the wrong path.
_UNAVAILABILITY_STATE = {"not registered": "not configured", "not running": "not running"}
_UNAVAILABILITY_ADVICE = {
    "not registered": "Enable it on the Providers page and ask again",
    "not running": "Start it and ask again",
}


def _unavailability_state(why: str) -> str:
    """User-facing state for a ``why`` (``"not registered"`` → ``"not configured"``)."""
    return _UNAVAILABILITY_STATE.get(why, why)


def _local_only_refusal(prof: ProfileOverrides, provider: str, why: str) -> str:
    """The deterministic answer of a *pinned* local-only specialist whose
    local provider is unavailable — names the problem ``why`` describes and
    the matching fix, then the ``/agent`` way out."""
    who = prof.title or prof.profile_name or "This specialist"
    advice = _UNAVAILABILITY_ADVICE.get(why, "Make it available and ask again")
    return (
        f"{who} needs a local model; {_local_label(provider)} is {_unavailability_state(why)}. "
        f"{advice}, or pick another agent with /agent."
    )


# The router passes over a provider whose health score is under this
# (``RoutingEngine.route``: ``if health < 0.1: skipped "unhealthy"``); the
# wizard's fallback pick must not send a turn where the router itself would
# not. Kept as one constant here because the router spells it inline.
ROUTER_MIN_HEALTH = 0.1


def _provider_health(engine: Any, name: str) -> float | None:
    """The router's health score for ``name`` (0.0-1.0), ``None`` when there is none.

    Asks the router's own rate manager first (``engine.router.rate_manager``,
    what ``RoutingEngine.route`` consults), then the engine's; anything that
    is not a number — no manager, a raising ``get_health_score``, a test
    double — counts as "no health data". Never raises.
    """
    for owner in (getattr(engine, "router", None), engine):
        manager = getattr(owner, "rate_manager", None)
        if manager is None:
            continue
        try:
            score = manager.get_health_score(name)
        except Exception as exc:
            logger.debug("wizard: no health score for %r (%s)", name, exc)
            continue
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            return float(score)
    return None


def _provider_has_models(engine: Any, name: str) -> bool | None:
    """Whether the router would find a model for ``name``; ``None`` when unknown.

    Mirrors ``RoutingEngine.route``: the registry's catalog for the provider,
    else the provider config's ``default_model`` resolved through the registry.
    ``False`` only when both come up empty — a registry that cannot answer
    (or answers with something that is not a list) is "unknown", never "no
    models". Never raises.
    """
    try:
        models = engine.registry.get_models_for_provider(name)
    except Exception as exc:
        logger.debug("wizard: cannot list models for %r (%s)", name, exc)
        return None
    if not isinstance(models, (list, tuple, set, frozenset)):
        return None
    if models:
        return True
    try:
        pconf = engine.config.providers.get(name)
        default_model = getattr(pconf, "default_model", None) if pconf is not None else None
        if default_model is None:
            return False
        if not isinstance(default_model, str):
            return None
        if not default_model.strip():
            return False
        return engine.registry.get_model_info(default_model) is not None
    except Exception as exc:
        logger.debug("wizard: cannot resolve a default model for %r (%s)", name, exc)
        return None


def _router_would_skip(engine: Any, name: str) -> str | None:
    """Why ``RoutingEngine.route`` would pass over ``name`` — ``None`` when it
    would not, or when there is no data to say so (no health score, unknown
    catalog). The two gates are the router's own: health under
    :data:`ROUTER_MIN_HEALTH` and no model to run on."""
    health = _provider_health(engine, name)
    if health is not None and health < ROUTER_MIN_HEALTH:
        return f"unhealthy (health {health:.2f} < {ROUTER_MIN_HEALTH})"
    if _provider_has_models(engine, name) is False:
        return "no models available"
    return None


def _best_non_local_provider(engine: Any) -> str | None:
    """The registered provider a turn goes to when the local one is down.

    Candidates are the registered non-local providers the router itself
    would not skip (:func:`_router_would_skip`: health at or above
    :data:`ROUTER_MIN_HEALTH` and at least one model — a provider whose
    circuit breaker is open, or with nothing to run, is never the fallback).
    Where the router has no health data or no catalog the gate is open, so
    an engine without a rate manager keeps the plain heuristic. Among the
    candidates: the configured default provider (``defaults.provider``) when
    it is one, else the first in registry (config) order; ``None`` when
    nothing but the local provider is registered or every alternative is
    gated out. Never raises.
    """
    try:
        registered = [
            p for p in engine.registry.list_enabled() if isinstance(p, str) and p != LOCAL_PROVIDER
        ]
    except Exception as exc:
        logger.debug("wizard: cannot list registered providers (%s)", exc)
        return None
    candidates: list[str] = []
    for name in registered:
        why = _router_would_skip(engine, name)
        if why is None:
            candidates.append(name)
        else:
            logger.info("wizard: %r is not a fallback for the local provider: %s", name, why)
    if not candidates:
        return None
    try:
        default = engine.config.defaults.provider
    except Exception:
        default = None
    if isinstance(default, str) and default in candidates:
        return default
    return candidates[0]


async def _route_around_dead_local_provider(decision: Any, engine: Any, question: str) -> None:
    """Keep a turn off the local provider when its daemon is known to be down.

    The router's local-first broker (``RoutingEngine._try_local_first``)
    hands short or simple queries to ``ollama`` whenever it is *registered*
    — it has no reachability signal and no exclusion parameter — so on a box
    with Ollama in config but not running every plain question would
    otherwise go to a dead daemon, fail, and end at the offline helper. The
    demotion of a concierge-chosen local-only specialist
    (:func:`_prepare_turn`) assumes the general Wizard then answers on a
    live provider; this is where that assumption is made true. Applied to
    every turn on both paths, after the profile pin (so a pin to a cloud
    provider never probes anything).

    Only when the cached probe (:func:`_local_provider_reachable`) says the
    daemon is down: the decision moves to :func:`_best_non_local_provider`
    with that provider's *own* model (:func:`_model_for_provider`) and the
    reason records ``local provider unreachable, using <provider>``. With
    nothing else registered — or every alternative gated out by the router's
    own health/model checks — the decision stands: the completion fails fast
    and the turn falls back deterministically, as before. Never raises.
    """
    if getattr(decision, "provider", None) != LOCAL_PROVIDER:
        return
    if await _local_provider_reachable(_local_provider_base_url(engine)):
        return
    alternative = _best_non_local_provider(engine)
    if alternative is None:
        logger.info(
            "wizard: %r is unreachable and no other registered provider is usable; keeping the router's decision",
            LOCAL_PROVIDER,
        )
        return
    decision.model = _model_for_provider(engine, question, alternative)
    decision.provider = alternative
    note = f"local provider unreachable, using {alternative}"
    reason = getattr(decision, "reason", None)
    decision.reason = f"{reason}; {note}" if isinstance(reason, str) and reason else note
    logger.info("wizard: router chose %r but it is unreachable; using %r", LOCAL_PROVIDER, alternative)


def _model_for_provider(engine: Any, question: str, provider: str) -> str:
    """The model the router picks for ``question`` when constrained to ``provider``.

    Model ids are provider-specific, so a profile that pins a provider
    without a model must not inherit the router's model — that one belongs
    to the provider the router chose. The router's ``provider_override``
    path resolves the pinned provider's own default
    (``providers.<name>.default_model``); an empty string lets the provider
    apply its built-in default. Never raises.
    """
    try:
        constrained = engine.router.route(query=question, provider_override=provider)
    except Exception as exc:
        logger.debug("wizard: model lookup for pinned provider %r failed (%s)", provider, exc)
        return ""
    model = getattr(constrained, "model", "")
    return model if isinstance(model, str) else ""


async def _pin_provider(decision: Any, prof: ProfileOverrides, engine: Any, question: str) -> str | None:
    """Apply a profile's advisory provider/model pin to the router's decision.

    An *available* pin (:func:`_provider_available`: registered and, for
    the local provider, reachable) replaces the router's provider. The model
    is the profile's own pin when it has one; otherwise, whenever the router
    had chosen a *different* provider, it is re-resolved for the pinned
    provider (:func:`_model_for_provider`) — provider A's model id never
    reaches provider B. When the router already chose the pinned provider
    its task-aware model pick stands.

    An unavailable pin — unregistered, or the local daemon not answering —
    keeps the router's decision and records ``profile_provider_unavailable``
    in its ``reason`` — the ~20 library profiles pinned to ``ollama`` must
    still answer on a box without Ollama. (When the router itself chose the
    same unavailable provider the note does not claim to be "using" it:
    :func:`_route_around_dead_local_provider` moves the turn off it next.)
    The exception is a ``local-only`` profile the *user* pinned: rather than
    run it on a cloud provider, return the deterministic refusal text
    (:func:`_local_only_refusal`, worded for ``why``; ``None`` otherwise).
    A local-only specialist the *concierge* chose never reaches that
    refusal: :func:`_prepare_turn` already demoted the turn to the general
    Wizard when the local provider was unavailable.
    """
    provider = _pinned_provider(prof)
    if not provider:
        return None
    why = await _provider_unavailability(engine, provider)
    if why is None:
        if prof.model:
            decision.model = prof.model
        elif decision.provider != provider:
            decision.model = _model_for_provider(engine, question, provider)
        decision.provider = provider
        return None
    if prof.local_only:
        return _local_only_refusal(prof, provider, why)
    note = f"profile_provider_unavailable: '{provider}' is {why}"
    if decision.provider != provider:
        note = f"{note}, using the router's {decision.provider}"
    reason = getattr(decision, "reason", None)
    decision.reason = f"{reason}; {note}" if isinstance(reason, str) and reason else note
    logger.info(
        "wizard: profile %r pins provider %r which is %s; keeping %r",
        prof.profile_name, provider, why, decision.provider,
    )
    return None


# ────────────────────────────────────────────────────────────────────────────
# Turn setup — shared by wizard_chat and wizard_chat_stream
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class _TurnSetup:
    """Everything a turn needs before the first completion, computed once."""

    snapshot: dict[str, Any]
    findings: list[Any]
    registry: Any | None
    tool_schemas: list[dict[str, Any]]
    profile: Any | None  # resolved AgentProfile; None = the general Wizard
    choice: Any | None  # SpecialistChoice when the concierge chose; None = explicit pin
    prof: ProfileOverrides  # effective overrides (whitelist already widened for auto routing)
    system_prompt: str
    user_message: str
    history: list[dict[str, Any]]

    @property
    def profile_reason(self) -> str | None:
        return self.choice.reason if self.choice is not None else None

    def meta(self, **extra: Any) -> dict[str, Any]:
        """Persistence metadata for an LLM-answered turn: attribution first so
        a reload can restore it. Deterministic answers use :meth:`meta_for`."""
        if isinstance(extra.get("tool_calls"), list):
            extra["tool_calls"] = _persistable_calls(extra["tool_calls"])
        return {
            "source": "wizard",
            "used_profile": self.prof.profile_name,
            "profile_reason": self.profile_reason,
            **extra,
        }

    def meta_for(self, result: dict[str, Any], **extra: Any) -> dict[str, Any]:
        """Persistence metadata for a deterministic envelope (:func:`_deterministic_result`).

        Attribution and the tool trace are copied from the envelope, so what
        the live response says and what a reload restores can never disagree:
        a deterministic answer is attributed to no specialist unless the
        envelope itself names one (the local-only refusal of an explicit pin).
        """
        return {
            "source": "wizard",
            "used_profile": result["used_profile"],
            "profile_reason": result["profile_reason"],
            "mode": result["mode"],
            "fallback_reason": result["fallback_reason"],
            "iterations": result["iterations"],
            "tool_calls": _persistable_calls(result["tool_calls"]),
            "tool_results": result["tool_results"],
            "deferred_tool_calls": result["deferred_tool_calls"],
            **extra,
        }


async def _prepare_turn(
    question: str,
    *,
    history: list[dict[str, Any]] | None,
    home_dir: str | Path | None,
    enable_followup: bool,
    profile: str | None,
    label: str,
    engine: Any | None = None,
) -> _TurnSetup:
    """Live state → specialist → overrides → filtered catalog → system prompt.

    ``history`` is handed to the concierge unchanged (entries may carry
    ``used_profile`` for continuity) and stored for message building.
    ``engine`` (the turn's engine, ``None`` when there is none) lets a
    concierge-chosen ``local-only`` specialist be demoted to the general
    Wizard *before* the system prompt is built when its local provider is
    unavailable (unregistered or not running, :func:`_provider_available`);
    an explicit pin is never demoted here.
    """
    from nvh.integrations.wizard.concierge import SpecialistChoice, resolve_auto_profile
    from nvh.integrations.wizard.context import wizard_context
    from nvh.integrations.wizard.findings import derive_findings
    from nvh.integrations.wizard.personality import build_system_prompt

    # ``wizard_context`` is blocking by design (httpx.get of Ollama's /api/tags,
    # nvidia-smi / pynvml, disk and provider probes — seconds in the worst
    # case) and both chat paths run on uvicorn's event loop: called inline it
    # stalls every other request in the process for the whole collection.
    # A worker thread keeps the loop free; the module attribute is looked up
    # at call time, so tests that patch ``context.wizard_context`` still win.
    snapshot = await asyncio.to_thread(wizard_context, home_dir=home_dir)
    findings = derive_findings(snapshot)

    # Build the Wizard tool registry once for the whole turn: the system
    # prompt teaches the model from its catalog, the follow-up loop executes
    # against it and classifies deferred calls with it. Best-effort — if it
    # isn't available (e.g. import order during tests) the tools block is
    # omitted and nothing can run.
    registry: Any | None = None
    tool_schemas: list[dict[str, Any]] = []
    try:
        from nvh.integrations.wizard.tools import default_registry

        registry = default_registry()
        tool_schemas = [t.as_public_dict() for t in registry.list_tools()]
    except Exception as exc:
        logger.debug("%s: tool registry not available (%s)", label, exc)

    # Concierge: with no explicit pin (None / "" / "auto") pick a hidden
    # specialist for this turn from the question, workspace state and the
    # previous turn's specialist. Explicit profile names pass through with
    # ``choice=None``. The catalog is loaded once and shared.
    profiles = _load_profiles(home_dir)
    profile_name, choice = resolve_auto_profile(
        profile,
        question,
        context=snapshot,
        findings=[f.to_dict() for f in findings],
        history=history,
        home_dir=home_dir,
        profiles=profiles,
    )

    # Resolve the agent profile once: its tool whitelist shapes the catalog
    # the model sees AND gates execution in the follow-up loop. A specialist
    # the concierge chose keeps the general Wizard's core tools; a pin is strict.
    resolved = _resolve_profile(profile_name, home_dir, profiles=profiles)
    prof = _overrides_for(resolved)
    if choice is not None:
        missing = await _missing_local_provider(prof, engine)
        if missing is not None:
            # The user pinned nothing, so a hidden specialist whose local
            # provider is down must not turn an ordinary question into a
            # refusal: the general Wizard answers this turn (no persona addon,
            # no whitelist, used_profile None) and the reason says why —
            # "not running" or "not configured", whichever is true.
            provider, why = missing
            logger.info(
                "%s: concierge chose local-only %r but %r is %s; general Wizard answers",
                label, prof.profile_name, provider, why,
            )
            choice = SpecialistChoice(
                profile=None,
                reason=(
                    f"general Wizard: local-only specialist unavailable: {_local_label(provider)} "
                    f"{_unavailability_state(why)}; would have been {choice.reason}"
                ),
                confidence=float(getattr(choice, "confidence", 0.0) or 0.0),
                matched=tuple(getattr(choice, "matched", ()) or ()),
            )
            resolved = None
            prof = ProfileOverrides()
        else:
            prof = prof.with_core_tools()
    tool_schemas = _filter_tool_schemas(tool_schemas, prof.tools_allowed)

    # Auto-fold the top vault chunk if the user's question matches anything
    # they've already written down. Free recall — saves a tool round-trip.
    vault_recall: str | None = None
    if enable_followup and _autofold_enabled():
        vault_recall = await _auto_fold_vault_chunk(question, home_dir=home_dir)

    system_prompt = prof.apply_to_prompt(build_system_prompt(
        snapshot, tools=tool_schemas, vault_recall=vault_recall, findings=findings,
    ))
    return _TurnSetup(
        snapshot=snapshot,
        findings=findings,
        registry=registry,
        tool_schemas=tool_schemas,
        profile=resolved,
        choice=choice,
        prof=prof,
        system_prompt=system_prompt,
        user_message=_apply_prompt_template(question, resolved),
        history=list(history or []),
    )


def _get_engine(label: str) -> Any | None:
    try:
        from nvh.api.server import get_engine  # type: ignore

        return get_engine()
    except Exception as exc:
        logger.debug("%s: engine unavailable (%s)", label, exc)
        return None


def _build_messages(turn: _TurnSetup) -> list[Any]:
    """System prompt + prior user/assistant turns + this turn's user message."""
    from nvh.providers.base import Message

    messages: list[Any] = [Message(role="system", content=turn.system_prompt)]
    for entry in turn.history:
        role = entry.get("role")
        content = entry.get("content", "")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            messages.append(Message(role=role, content=content))
        if role == "assistant":
            # What the user did with that turn's cards (ran / refused / handed
            # to a terminal) — the only way a privileged outcome reaches the model.
            outcomes = _history_tool_results_message(entry)
            if outcomes:
                messages.append(Message(role="system", content=outcomes))
    messages.append(Message(role="user", content=turn.user_message))
    return messages


@dataclass
class _LoopState:
    """What the follow-up loop accumulates across iterations."""

    tool_results: list[dict[str, Any]] = field(default_factory=list)
    # Confirm-class calls for the UI's confirm cards (``tool_calls`` /
    # ``confirm_required``). Accumulate across iterations, never duplicate.
    pending_confirm: list[dict[str, Any]] = field(default_factory=list)
    # Auto-class calls the loop did not execute, each with a ``reason``. The
    # UI shows them; it never auto-runs them.
    deferred_auto: list[dict[str, Any]] = field(default_factory=list)
    # True while the latest iteration actually executed an auto tool —
    # refusals and confirm-class deferrals never count.
    ran_any_auto: bool = False
    # LLM round-trips attempted so far (incremented before each completion),
    # so a deterministic fallback after a failed later iteration still
    # reports how far the loop got.
    iterations: int = 0


async def _defer_calls_to_ui(
    tool_calls: list[dict[str, Any]],
    *,
    state: _LoopState,
    turn: _TurnSetup,
    reason: str,
) -> list[dict[str, Any]]:
    """Route tool calls the loop will NOT execute to their buckets.

    Used when follow-up is disabled, ``max_iterations=1``, or the cost
    ceiling fired. Whitelist refusals are recorded in ``tool_results`` (and
    returned so the stream can emit them); confirm-class or unknown calls go
    to the confirm bucket (privileged ones with their card, see
    :func:`_surfaced_call`); auto-class calls go to ``deferred_auto`` with
    ``reason`` so the UI can explain why they did not run.
    """
    prof = turn.prof
    allowed, refused = _split_by_whitelist(tool_calls, prof.tools_allowed, prof.profile_name)
    state.tool_results.extend(refused)
    confirm_calls, auto_calls = _split_by_safety_class(allowed, turn.registry)
    await _surface_confirm_calls(state.pending_confirm, confirm_calls, turn.registry)
    for call in auto_calls:
        entry = {"name": call["name"], "arguments": call.get("arguments", {}), "reason": reason}
        if entry not in state.deferred_auto:
            state.deferred_auto.append(entry)
    return refused


async def _execute_tool_calls(
    tool_calls: list[dict[str, Any]],
    *,
    turn: _TurnSetup,
    state: _LoopState,
    messages: list[Any],
) -> AsyncIterator[dict[str, Any]]:
    """One iteration's tool step, yielding ``tool_call`` / ``tool_result`` events.

    Auto-class tools run server-side and their results are fed back to the
    model as system messages; confirm-class calls are deferred to the UI;
    whitelist refusals are recorded (and shown) but never executed and never
    counted as an executed tool — a turn that only produced refusals stops
    iterating instead of paying for another completion.
    """
    from nvh.providers.base import Message

    prof = turn.prof
    state.ran_any_auto = False
    deferred: list[dict[str, Any]] = []
    for call in tool_calls:
        yield {"type": "tool_call", "name": call["name"], "arguments": call.get("arguments", {})}
        result = await _run_auto_tool(
            call["name"], call.get("arguments", {}),
            tools_allowed=prof.tools_allowed, profile_name=prof.profile_name,
            registry=turn.registry,
        )
        if result.get("deferred_to_user"):
            deferred.append(call)
            continue
        entry = {"name": call["name"], "arguments": call.get("arguments", {}), "result": result}
        state.tool_results.append(entry)
        yield _tool_result_event(entry)
        # Feed the result back so the next iteration (if any) can react —
        # refusals included, so the model can explain what this profile
        # cannot do if something else did run.
        messages.append(Message(
            role="system", content=_format_tool_result_message(call["name"], result),
        ))
        if result.get("not_allowed"):
            continue
        state.ran_any_auto = True
        if call["name"] in LOCAL_PROBE_INVALIDATING_TOOLS:
            # The tool may have (re)started or re-queried the daemon: forget
            # what the probe said so the next check asks again instead of
            # trusting an answer from before the tool ran.
            _reset_local_probe_cache()

    # Confirm-class calls accumulate across iterations and go out with the
    # single confirm_required event / ``tool_calls``, so nothing is lost when
    # a later iteration answers without re-emitting them.
    await _surface_confirm_calls(state.pending_confirm, deferred, turn.registry)


def _clamp_max_iterations(value: int | None) -> int:
    """Clamp a user-supplied max_iterations to the safe [1, WIZARD_FOLLOWUP_MAX_ITER]
    range. None or anything non-numeric falls back to the global cap."""
    if value is None:
        return WIZARD_FOLLOWUP_MAX_ITER
    try:
        v = int(value)
    except (TypeError, ValueError):
        return WIZARD_FOLLOWUP_MAX_ITER
    return max(1, min(WIZARD_FOLLOWUP_MAX_ITER, v))


def _defer_reason(enable_followup: bool) -> str:
    """Why auto-class calls were not run when the loop cannot follow up."""
    return DEFER_MAX_ITERATIONS if enable_followup else DEFER_FOLLOWUP_DISABLED


def _deterministic_result(
    turn: _TurnSetup,
    answer: str,
    fallback_reason: str | None,
    *,
    state: _LoopState | None = None,
    used_profile: str | None = None,
) -> dict[str, Any]:
    """The non-streaming envelope for a turn no LLM answered.

    ``state`` is the follow-up loop's state at the moment the LLM path gave
    up: auto tools an earlier iteration executed, refusals, deferred calls,
    pending confirm-class calls and the number of round-trips attempted all
    travel with the deterministic answer instead of being dropped. ``None``
    (or a fresh state) means nothing ran. The stream's ``error`` event is
    derived from the same envelope (:func:`_error_event`), as is the
    persisted meta (``_TurnSetup.meta_for``).
    """
    state = state if state is not None else _LoopState()
    return {
        "answer": answer,
        "mode": "deterministic",
        "fallback_reason": fallback_reason,
        "context": turn.snapshot,
        "tool_calls": list(state.pending_confirm),
        "tool_results": list(state.tool_results),
        "deferred_tool_calls": list(state.deferred_auto),
        "iterations": state.iterations,
        # Set only when a specialist itself declined (local-only refusal of
        # an explicit pin); the offline helper is not the specialist.
        "used_profile": used_profile,
        # Independent of attribution: the concierge's reason (a demotion
        # note, "general Wizard: no specialist matched", the specialist it
        # chose before the LLM failed) always travels; None only for an
        # explicit pin, where no selection ran.
        "profile_reason": turn.profile_reason,
    }


def _error_event(result: dict[str, Any], *, error: str) -> dict[str, Any]:
    """The stream's ``error`` event for a deterministic envelope.

    Same ``fallback_reason`` and attribution as the non-stream envelope, so
    a client reading either path sees one story.
    """
    return {
        "type": "error",
        "error": error,
        "fallback": result["answer"],
        "fallback_reason": result["fallback_reason"],
        "used_profile": result["used_profile"],
        "profile_reason": result["profile_reason"],
    }


async def wizard_chat(
    question: str,
    *,
    history: list[dict[str, str]] | None = None,
    home_dir: str | Path | None = None,
    enable_followup: bool = True,
    conversation_id: str | None = None,
    profile: str | None = None,
    max_iterations: int | None = None,
) -> dict[str, Any]:
    """Answer a Wizard question with the live-state-grounded LLM path.

    Args:
        question: The user's message for this turn.
        history: Optional prior turns as a list of ``{role, content}`` dicts
            (assistant entries may carry ``used_profile`` so the concierge
            can keep the previous specialist). ``role`` is one of
            ``"user" | "assistant"``. Empty/None starts a fresh conversation.
        home_dir: NVH_HOME override; defaults to the current workspace.
        enable_followup: If True (default), auto-class tool calls run
            server-side and the LLM gets follow-up turns to react. Set to
            False to keep the original one-shot behavior — useful for
            tests that mock a single ``provider.complete`` call.
        profile: ``None`` / ``""`` / ``"auto"`` let the concierge choose;
            any other name pins that profile (``"wizard"`` = general persona).
            A concierge-chosen ``local-only`` specialist whose local provider
            is unavailable (unregistered or not running) is demoted to the
            general Wizard for the turn; the same profile *pinned* refuses
            deterministically instead.
        max_iterations: Clamp on LLM round-trips; 1 = "just answer, no tools".

    Returns:
        ``{answer, mode, used_provider?, used_model?, context, tool_calls,
        tool_results, deferred_tool_calls, iterations, fallback_reason?,
        used_profile, profile_reason}`` where ``mode`` is:

          - ``"llm"``  — answered by a routed LLM (preferred path)
          - ``"deterministic"`` — fell back to setup_assistant_reply (or a
            local-only specialist declined because its provider is down)

        ``tool_calls`` holds confirm-class calls the UI needs to surface;
        auto-class calls that ran server-side are in ``tool_results``;
        auto-class calls the loop did not run are in ``deferred_tool_calls``
        with a ``reason``. ``iterations`` is the count of LLM round-trips.
    """
    engine = _get_engine("wizard_chat")
    turn = await _prepare_turn(
        question, history=history, home_dir=home_dir,
        enable_followup=enable_followup, profile=profile, label="wizard_chat", engine=engine,
    )
    prof = turn.prof
    fallback_reason: str | None = None
    state = _LoopState()
    decision: Any = None

    if engine is not None:
        try:
            from nvh.providers.base import Message

            messages = _build_messages(turn)

            await engine.initialize()
            await engine._check_budget()

            # Use the engine's router so we get the same local-first behavior
            # as `nvh ask` — Ollama if registered, cheapest free cloud
            # otherwise. A profile's provider/model pin is advisory on top of
            # that, and a local daemon the probe says is down is routed around.
            decision = engine.router.route(query=question)
            refusal = await _pin_provider(decision, prof, engine, question)
            if refusal is not None:
                result = _deterministic_result(
                    turn, refusal, LOCAL_ONLY_FALLBACK_REASON, state=state, used_profile=prof.profile_name,
                )
                await _persist_wizard_turn(
                    conversation_id, user_question=question, assistant_text=refusal,
                    metadata=turn.meta_for(result),
                )
                return result
            await _route_around_dead_local_provider(decision, engine, question)
            provider = engine.registry.get(decision.provider)
            if provider is None:
                raise RuntimeError(f"Router picked provider '{decision.provider}' but it isn't registered")
            temperature, max_tokens = prof.sampling_params(engine)

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
            # Set to True if a profile's max_cost_usd_per_turn aborted the
            # follow-up loop early. The UI uses this to explain why the
            # answer is the first iteration's draft instead of a tool-grounded
            # final reply.
            cost_ceiling_hit = False

            iter_cap = _clamp_max_iterations(max_iterations)
            # max_iterations=1 = "just answer me, don't chain tools."
            # We honor that by short-circuiting the follow-up loop the same
            # way enable_followup=False would.
            effective_followup = enable_followup and iter_cap > 1
            while state.iterations < iter_cap:
                state.iterations += 1
                response = await provider.complete(
                    messages=messages,
                    model=decision.model or None,
                    system_prompt=turn.system_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
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

                # Profile cost ceiling: if the running cost has crossed the
                # per-turn limit, stop iterating. We keep the answer that
                # already landed so the user gets *something*, and the
                # envelope's cost_ceiling_hit flag lets the UI explain why
                # follow-up tool calls didn't run.
                if _cost_ceiling_reached(prof.cost_ceiling_usd, total_cost_usd):
                    cost_ceiling_hit = True
                    await _defer_calls_to_ui(tool_calls, state=state, turn=turn, reason=DEFER_COST_CEILING)
                    logger.info(
                        "wizard_chat: profile cost ceiling reached "
                        "(%.4f >= %.4f) — stopping follow-up loop",
                        total_cost_usd, prof.cost_ceiling_usd,
                    )
                    break

                if not tool_calls or not effective_followup:
                    # No tool calls (or follow-up disabled / capped at 1) —
                    # done. Confirm-class calls still reach the UI as cards;
                    # auto-class ones are reported as deferred, not run.
                    await _defer_calls_to_ui(
                        tool_calls, state=state, turn=turn, reason=_defer_reason(enable_followup),
                    )
                    break

                # Append the assistant's reply (with markers stripped) to the
                # conversation so it sees its own reasoning on the next turn.
                messages.append(Message(role="assistant", content=cleaned_text))

                # Execute auto-class tools, defer confirm-class to the UI.
                async for _event in _execute_tool_calls(
                    tool_calls, turn=turn, state=state, messages=messages,
                ):
                    pass
                if not state.ran_any_auto:
                    # Only confirm-class calls / refusals — stop iterating.
                    break

            await _persist_wizard_turn(
                conversation_id,
                user_question=question,
                assistant_text=final_text,
                provider=decision.provider or "",
                model=decision.model or "",
                metadata=turn.meta(
                    iterations=state.iterations,
                    tool_calls=state.pending_confirm,
                    tool_results=state.tool_results,
                    deferred_tool_calls=state.deferred_auto,
                    cost_usd=total_cost_usd,
                    latency_ms=total_latency_ms,
                ),
            )
            return {
                "answer": final_text,
                "mode": "llm",
                "used_provider": decision.provider,
                "used_model": decision.model,
                "routing_reason": getattr(decision, "reason", None),
                "context": turn.snapshot,
                "tool_calls": state.pending_confirm,
                "tool_results": state.tool_results,
                "deferred_tool_calls": state.deferred_auto,
                "iterations": state.iterations,
                "cost_usd": total_cost_usd,
                "latency_ms": total_latency_ms,
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "fallback_from": fallback_from,
                "cost_ceiling_hit": cost_ceiling_hit,
                "cost_ceiling_usd": prof.cost_ceiling_usd,
                # None = the general Wizard persona answered.
                "used_profile": prof.profile_name,
                "profile_reason": turn.profile_reason,
            }
        except Exception as exc:
            logger.info("wizard_chat: LLM path failed, falling back to deterministic (%s)", exc)
            fallback_reason = str(exc)[:300]
            if getattr(decision, "provider", None) == LOCAL_PROVIDER:
                # The daemon may have died since the probe said "up": a stale
                # positive must not send the next turn there too.
                _reset_local_probe_cache()
    else:
        fallback_reason = "engine not initialized"

    # Deterministic fallback — guaranteed offline-safe.
    try:
        from nvh.integrations.wizard.setup_agent import setup_assistant_reply

        det = await asyncio.to_thread(setup_assistant_reply, question, home_dir=home_dir)
        answer = det.get("answer") if isinstance(det, dict) else str(det)
    except Exception as exc:
        logger.warning("wizard_chat: deterministic fallback failed too: %s", exc)
        answer = (
            "Sorry — I couldn't reach a model and the offline helper also "
            "errored. Open Setup → Diagnostics for the latest status, or "
            "share a support snapshot from the wizard."
        )

    # Everything a failed later iteration already did — executed auto tools,
    # refusals, pending confirm-class calls, round-trips attempted — still
    # reaches the caller and the persisted meta (mirrors the stream's
    # confirm_required-before-error). Attributed to no specialist.
    result = _deterministic_result(turn, answer, fallback_reason, state=state)
    await _persist_wizard_turn(
        conversation_id, user_question=question, assistant_text=answer, metadata=turn.meta_for(result),
    )
    return result


# ────────────────────────────────────────────────────────────────────────────
# Streaming variant — emits incremental events for the UI to render.
# ────────────────────────────────────────────────────────────────────────────

_TOOL_LINE_RE = re.compile(r"^\s*TOOL_CALL\s*:", re.IGNORECASE)


def _meter_from_chunk(chunk: Any) -> tuple[float | None, int | None, int | None]:
    """``(cost_usd, input_tokens, output_tokens)`` from a ``StreamChunk``.

    Each is ``None`` when the chunk doesn't carry a real number — providers
    only attach usage/cost to the final chunk, and test doubles may expose
    arbitrary attributes, so we only trust genuine numeric values.
    """
    def _num(value: Any) -> bool:
        return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)

    cost = getattr(chunk, "cost_usd", None)
    usage = getattr(chunk, "usage", None)
    in_tok = getattr(usage, "input_tokens", None) if usage is not None else None
    out_tok = getattr(usage, "output_tokens", None) if usage is not None else None
    return (
        float(cost) if _num(cost) else None,
        int(in_tok) if _num(in_tok) else None,
        int(out_tok) if _num(out_tok) else None,
    )


async def _filtered_token_stream(
    stream: AsyncIterator[Any],
) -> AsyncIterator[tuple[str, Any]]:
    """Yield ``("token", text)`` for user-visible token deltas, hiding any
    line that starts with ``TOOL_CALL:`` from the live stream. At the end
    yields ``("meter", {cost_usd, input_tokens, output_tokens})`` — read from
    the last chunk that reported usage/cost, zeros if none did — and finally
    ``("full", accumulated)`` so the caller can parse markers from the
    complete response.

    The filter buffers up to a newline so a TOOL_CALL line never bleeds into
    the user-visible stream even if the provider chunks mid-line.
    """
    full_parts: list[str] = []
    line_buf = ""
    in_tool_line = False
    cost_usd = 0.0
    input_tokens = 0
    output_tokens = 0

    async for chunk in stream:
        # Read the meter before the empty-delta short-circuit: the final
        # chunk that carries usage/cost usually has no text of its own.
        chunk_cost, chunk_in, chunk_out = _meter_from_chunk(chunk)
        if chunk_cost is not None:
            cost_usd = chunk_cost
        if chunk_in is not None:
            input_tokens = chunk_in
        if chunk_out is not None:
            output_tokens = chunk_out
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

    yield ("meter", {
        "cost_usd": cost_usd,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    })
    yield ("full", "".join(full_parts))


async def wizard_chat_stream(
    question: str,
    *,
    history: list[dict[str, str]] | None = None,
    home_dir: str | Path | None = None,
    enable_followup: bool = True,
    conversation_id: str | None = None,
    profile: str | None = None,
    max_iterations: int | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream a Wizard turn as a sequence of events.

    Event types yielded:

      - ``{"type": "iteration", "n": int}``
      - ``{"type": "token", "text": str}``                — user-visible delta
      - ``{"type": "tool_call", "name": str, "arguments": dict}``
      - ``{"type": "tool_result", "name": str, "result": dict}`` — executed
        auto tools *and* whitelist refusals (``result.not_allowed=True``), so
        the trace shows what the profile declined
      - ``{"type": "confirm_required", "tool_calls": [...]}``  — for UI cards;
        confirm-class calls only, emitted at most once per turn, right before
        ``done`` (or before ``error`` if a later iteration failed), and covers
        every deferred confirm-class call from any iteration
      - ``{"type": "done", "answer": str, "used_provider": str,
            "used_model": str, "tool_calls": [...], "tool_results": [...],
            "deferred_tool_calls": [{name, arguments, reason}, ...],
            "iterations": int, "cost_usd": float, "latency_ms": int,
            "cost_ceiling_hit": bool, "cost_ceiling_usd": float | None,
            "used_profile": str | None, "profile_reason": str | None}`` —
        ``deferred_tool_calls`` are auto-class calls the loop did not run
        (``max_iterations=1``, cost ceiling); the UI must not auto-run them
      - ``{"type": "error", "error": str, "fallback": str,
            "fallback_reason": str | None, "used_profile": str | None,
            "profile_reason": str | None}`` — the turn fell back to a
        deterministic answer (``fallback`` is its plain text). Same shape and
        attribution rule as the non-stream envelope: ``fallback_reason`` is
        the LLM error, ``"engine not initialized"`` or
        :data:`LOCAL_ONLY_FALLBACK_REASON`; ``used_profile`` is ``None``
        unless a *pinned* local-only specialist itself declined.

    Streaming is best-effort: if no engine is reachable, the function yields a
    single ``error`` event with a non-streamed deterministic fallback.
    """
    engine = _get_engine("wizard_chat_stream")
    turn = await _prepare_turn(
        question, history=history, home_dir=home_dir,
        enable_followup=enable_followup, profile=profile, label="wizard_chat_stream", engine=engine,
    )
    prof = turn.prof
    state = _LoopState()

    if engine is None:
        # No engine — fall back to deterministic, emit as a single chunk so the
        # UI can show the answer without a streaming render path.
        det = await _deterministic_fallback(question, home_dir=home_dir)
        result = _deterministic_result(turn, det, "engine not initialized", state=state)
        await _persist_wizard_turn(
            conversation_id, user_question=question, assistant_text=det,
            metadata=turn.meta_for(result, streamed=True),
        )
        yield _error_event(result, error="engine not initialized")
        return

    confirm_emitted = False
    decision: Any = None
    try:
        from nvh.providers.base import Message

        messages = _build_messages(turn)

        await engine.initialize()
        await engine._check_budget()

        decision = engine.router.route(query=question)
        # Honor the profile's advisory provider/model pin and route around a
        # dead local daemon (mirrors the non-streaming path so behavior stays
        # identical regardless of which endpoint the caller used).
        refusal = await _pin_provider(decision, prof, engine, question)
        if refusal is not None:
            result = _deterministic_result(
                turn, refusal, LOCAL_ONLY_FALLBACK_REASON, state=state, used_profile=prof.profile_name,
            )
            await _persist_wizard_turn(
                conversation_id, user_question=question, assistant_text=refusal,
                metadata=turn.meta_for(result, streamed=True),
            )
            yield _error_event(result, error=refusal)
            return
        await _route_around_dead_local_provider(decision, engine, question)
        provider = engine.registry.get(decision.provider)
        if provider is None:
            raise RuntimeError(
                f"Router picked provider '{decision.provider}' but it isn't registered",
            )
        temperature, max_tokens = prof.sampling_params(engine)

        final_text = ""
        # Same per-turn meter as the non-streaming path. Providers attach
        # usage + cost to the final StreamChunk, so the profile ceiling is
        # enforceable here too; latency is wall-clock per iteration.
        total_cost_usd: float = 0.0
        total_latency_ms: int = 0
        total_input_tokens: int = 0
        total_output_tokens: int = 0
        cost_ceiling_hit = False

        iter_cap = _clamp_max_iterations(max_iterations)
        effective_followup = enable_followup and iter_cap > 1
        while state.iterations < iter_cap:
            state.iterations += 1
            yield {"type": "iteration", "n": state.iterations}

            started = time.monotonic()
            stream_ctx = provider.stream(
                messages=messages,
                model=decision.model or None,
                system_prompt=turn.system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )

            full_text = ""
            async for kind, payload in _filtered_token_stream(stream_ctx):
                if kind == "token":
                    yield {"type": "token", "text": payload}
                elif kind == "meter":
                    total_cost_usd += payload["cost_usd"]
                    total_input_tokens += payload["input_tokens"]
                    total_output_tokens += payload["output_tokens"]
                else:
                    full_text = payload
            total_latency_ms += int((time.monotonic() - started) * 1000)

            cleaned_text, tool_calls = _extract_tool_calls(full_text)
            final_text = cleaned_text

            if _cost_ceiling_reached(prof.cost_ceiling_usd, total_cost_usd):
                cost_ceiling_hit = True
                for refused in await _defer_calls_to_ui(
                    tool_calls, state=state, turn=turn, reason=DEFER_COST_CEILING,
                ):
                    yield _tool_result_event(refused)
                logger.info(
                    "wizard_chat_stream: profile cost ceiling reached "
                    "(%.4f >= %.4f) — stopping follow-up loop",
                    total_cost_usd, prof.cost_ceiling_usd,
                )
                break

            if not tool_calls or not effective_followup:
                for refused in await _defer_calls_to_ui(
                    tool_calls, state=state, turn=turn, reason=_defer_reason(enable_followup),
                ):
                    yield _tool_result_event(refused)
                break

            messages.append(Message(role="assistant", content=cleaned_text))

            async for event in _execute_tool_calls(
                tool_calls, turn=turn, state=state, messages=messages,
            ):
                yield event
            if not state.ran_any_auto:
                break

        # One confirm_required event for everything the UI must confirm,
        # whichever iteration produced it (including iteration 1 with
        # max_iterations=1), emitted exactly once and always before done.
        if state.pending_confirm:
            confirm_emitted = True
            yield {"type": "confirm_required", "tool_calls": list(state.pending_confirm)}

        await _persist_wizard_turn(
            conversation_id,
            user_question=question,
            assistant_text=final_text,
            provider=decision.provider or "",
            model=decision.model or "",
            metadata=turn.meta(
                iterations=state.iterations,
                tool_calls=state.pending_confirm,
                tool_results=state.tool_results,
                deferred_tool_calls=state.deferred_auto,
                cost_usd=total_cost_usd,
                latency_ms=total_latency_ms,
                streamed=True,
            ),
        )
        yield {
            "type": "done",
            "answer": final_text,
            "used_provider": decision.provider,
            "used_model": decision.model,
            "routing_reason": getattr(decision, "reason", None),
            "tool_calls": state.pending_confirm,
            "tool_results": state.tool_results,
            "deferred_tool_calls": state.deferred_auto,
            "iterations": state.iterations,
            # Read from the final StreamChunk's usage/cost per iteration; a
            # provider that reports none leaves these at 0 and the UI hides
            # the meter.
            "cost_usd": total_cost_usd,
            "latency_ms": total_latency_ms,
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "fallback_from": None,
            "cost_ceiling_hit": cost_ceiling_hit,
            "cost_ceiling_usd": prof.cost_ceiling_usd,
            # None = the general Wizard persona answered.
            "used_profile": prof.profile_name,
            "profile_reason": turn.profile_reason,
        }
    except Exception as exc:
        logger.info("wizard_chat_stream: LLM path failed (%s)", exc)
        if getattr(decision, "provider", None) == LOCAL_PROVIDER:
            # Same as the non-stream path: a stale positive probe must not
            # outlive the daemon.
            _reset_local_probe_cache()
        # A later iteration failing must not swallow confirm-class calls an
        # earlier one surfaced: the user still gets to decide on them.
        if state.pending_confirm and not confirm_emitted:
            yield {"type": "confirm_required", "tool_calls": list(state.pending_confirm)}
        error = str(exc)[:300]
        det = await _deterministic_fallback(question, home_dir=home_dir)
        # Same envelope as the non-stream fallback: executed tools, refusals
        # and round-trips travel with it; attributed to no specialist.
        result = _deterministic_result(turn, det, error, state=state)
        await _persist_wizard_turn(
            conversation_id, user_question=question, assistant_text=det,
            metadata=turn.meta_for(result, streamed=True),
        )
        yield _error_event(result, error=error)


async def _deterministic_fallback(question: str, *, home_dir: str | Path | None) -> str:
    try:
        from nvh.integrations.wizard.setup_agent import setup_assistant_reply

        det = await asyncio.to_thread(setup_assistant_reply, question, home_dir=home_dir)
        return det.get("answer") if isinstance(det, dict) else str(det)
    except Exception as exc:
        logger.warning("wizard_chat_stream: deterministic fallback failed: %s", exc)
        return (
            "Sorry — I couldn't reach a model and the offline helper also "
            "errored. Open Setup → Diagnostics for the latest status."
        )
