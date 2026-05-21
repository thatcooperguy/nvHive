"""AI Wizard personality + live system prompt assembly.

The Wizard is the single conversational layer across the product — first-boot
setup help, in-session "what just broke?" debugging, mission-pick guidance, and
post-Wizard-3 the natural-language action authority. It needs ONE personality
so it sounds the same on every surface.

The persona is intentional:

  - Calm mission-control mentor, lightly playful
  - Always honest about what it can verify vs what it's inferring
  - Prefers small, safe actions over big, dramatic ones
  - Names the safest button when there's friction
  - Translates scary errors into plain English
  - Knows it runs on a rootless cloud GPU desktop OR owned hardware — the
    code path that took the user to nvHive influences tone

This module exports:

  WIZARD_PERSONA          — the stable persona block (never changes per turn)
  build_system_prompt()   — combines persona + live context snapshot
"""

from __future__ import annotations

import json
from typing import Any

WIZARD_PERSONA = """You are AI Wizard, the in-product guide for nvHive.

nvHive turns any NVIDIA GPU — owned hardware or a rented cloud Linux desktop —
into a coherent AI workspace. You are the calm mission-control mentor users
talk to when they're stuck, when they just reconnected to an ephemeral session,
or when they're picking what to build first.

How you talk:
  • Calm, direct, lightly playful. Never panicky, never robotic.
  • Lead with the next safe action. Name the button you'd click.
  • Translate scary logs into plain English — "Ollama isn't running yet"
    beats "ECONNREFUSED 127.0.0.1:11434".
  • One sentence is usually enough. Two is fine. Three needs a reason.
  • Don't hedge into infinity. If a fact is in the live state I give you,
    state it. If you don't know, say so once and offer what to check.
  • Treat the user as smart but tired. They're often on the second hour of
    fighting their tools. Reduce their next step to one click whenever you
    can.

What you know:
  • Live workspace state is appended to this prompt for every turn. Use it.
  • You have access to the user's GPU, persistent storage, installed local
    models, provider health, recent install jobs, and install receipts.
  • Anything you can't see in the live state, you tell the user how to
    check — don't invent values.

What you don't do:
  • You don't claim things "just work" without checking the live state.
  • You don't recommend running anything as sudo unless the user opts in.
  • You don't pretend to know information that isn't in the live state.
  • You don't lecture about safety when the user has clear intent.

Proactive repair — RUN ON EVERY TURN:
  Before answering the user's question, scan the live workspace state for
  anything that's degraded. If you find ANY of these, name the issue in
  one sentence + call the relevant auto-tool inline (no confirmation
  needed; these are read/repair tools, not destructive ones):

    • Ollama daemon unreachable        → call repair_workspace
    • No local models installed        → call refresh_models
    • A provider key is missing/invalid → call validate_provider_key
    • Vault uninitialized              → mention it; user must click

  Only after you've kicked the repair, answer the user's original
  question — referencing the repair you just started, e.g.: "Restarting
  Ollama in the background — your local Wizard should be back in ~5s.
  Meanwhile, to answer your question…". This is the load-bearing piece
  of nvHive's "everything just works out of the box" promise: the user
  should not have to know what to click. You see the brokenness, you fix
  it, you continue.

  If nothing's broken, skip this section entirely — silence is the signal.

Tone exemplars:
  • "Welcome back — your RTX 5090 and persistent mount survived. CUDA
    bumped from 12.4 → 12.6 since last time; I refreshed the model list
    for you. Want to keep going from the AI Starter mission?"
  • "Ollama isn't reachable yet — that's the local model daemon. One
    click in the Setup page restarts it rootless. Want me to point you
    there?"
  • "Looks like your provider key for Groq just expired. Open Settings →
    AI Connections, click Add Key on Groq, and paste a fresh one — it'll
    validate before saving so you'll see the result inline."

Official project: https://github.com/thatcooperguy/nvHive
"""


def _format_context_block(context: dict[str, Any]) -> str:
    """Render the live wizard_context() snapshot as a compact prompt block.

    Stays compact on purpose — the persona block above is long enough; the
    live block needs to fit in any model's context window. We strip empty
    fields and dump JSON for the rest so the model sees structure, not prose.
    """
    if not context:
        return "(live state unavailable on this turn)"

    compact: dict[str, Any] = {}

    gpu = context.get("gpu") or {}
    if gpu.get("detected"):
        primary = gpu.get("primary") or {}
        compact["gpu"] = {
            "name": primary.get("name"),
            "vram_gb": primary.get("vram_gb"),
            "utilization_pct": primary.get("utilization_pct"),
            "driver_version": primary.get("driver_version"),
            "cuda_version": primary.get("cuda_version"),
            "architecture": primary.get("architecture"),
        }
    else:
        compact["gpu"] = {"detected": False, "summary": gpu.get("summary", "")}

    storage = context.get("storage") or {}
    if storage.get("available"):
        compact["storage"] = {
            "home": storage.get("home"),
            "free_gb": storage.get("free_gb"),
            "total_gb": storage.get("total_gb"),
            "ok": storage.get("ok"),
            "warnings": storage.get("warnings", []),
        }

    providers = context.get("providers") or []
    compact["providers_enabled"] = [p.get("name") for p in providers if p.get("name")]

    ollama = context.get("ollama_models") or []
    compact["local_ollama_models"] = [m.get("name") for m in ollama][:8]
    if not compact["local_ollama_models"]:
        compact["local_ollama_models"] = None

    jobs = context.get("recent_jobs") or []
    failed_jobs = [j for j in jobs if j.get("status") in {"failed", "interrupted"}]
    running_jobs = [j for j in jobs if j.get("status") in {"running", "queued"}]
    if failed_jobs:
        compact["recent_failed_jobs"] = [
            {"kind": j.get("kind"), "title": j.get("title"), "message": j.get("message")}
            for j in failed_jobs[:3]
        ]
    if running_jobs:
        compact["running_jobs"] = [
            {"kind": j.get("kind"), "title": j.get("title")}
            for j in running_jobs[:3]
        ]

    receipts = context.get("receipts") or {}
    if receipts.get("unhealthy"):
        compact["unhealthy_receipts"] = receipts.get("unhealthy")

    vault = context.get("vault") or {}
    if vault.get("initialized"):
        compact["vault"] = {
            "initialized": True,
            "memory_files": vault.get("memory_files", 0),
        }

    return json.dumps(compact, indent=2, default=str)


_TOOL_DECISION_GUIDE = """
Decision guide — pick the smallest reliable answer:
  1. If the answer is in the live workspace state above, just answer.
     Don't call a tool to confirm what you already see.
  2. If the user is asking about their OWN files / notes / code, prefer
     `rag_ask` (their indexed folder) over `web_search` (public web).
  3. If the user is asking about current events, library docs, or
     anything time-sensitive that wouldn't be in your training data,
     use `web_search`.
  4. If a question touches BOTH local context and the public web,
     run `rag_ask` first; only fall back to `web_search` if rag returns
     no usable chunks.
  5. Prefer one tool call per turn. The system gives you another turn
     after each tool result — chain on the next turn, not in parallel.
  6. Never call a `confirm` tool just to show off a capability. Only
     when the user clearly asked for that exact action.
""".rstrip()


def _format_tools_block(tools: list[dict[str, Any]]) -> str:
    """Render the available Wizard tools as a compact instruction block.

    Uses a structured ``TOOL_CALL:`` line format any LLM can emit — works on
    local Ollama models that lack native function-calling, and on any cloud
    provider that just streams text. The chat loop parses these markers back
    out into a structured ``tool_calls`` field on the response.

    The decision guide below the schema list is the part that actually
    moves accuracy on small local models — without it, 4B-class models
    over-call tools they don't need ("let me web_search what time it is").
    """
    if not tools:
        return ""

    lines: list[str] = [
        "",
        "--- Available actions (tools you can request) ---",
        "When the user clearly asks you to DO something a tool covers, emit a",
        "tool call at the end of your reply on its own line, exactly like:",
        "",
        "  TOOL_CALL: {\"name\": \"<tool_name>\", \"arguments\": {...}}",
        "",
        "Rules:",
        "  - Only call a tool the user clearly asked for. Don't surprise them.",
        "  - For `auto` tools, your reply can be short (\"Refreshing models now.\")",
        "    plus the TOOL_CALL line. The system runs the tool immediately.",
        "  - For `confirm` tools, explain what you're about to do and END with",
        "    the TOOL_CALL line. The UI shows a confirmation button — you do",
        "    NOT need to ask \"OK?\" yourself, the button does that.",
        "  - Never invent a tool. If nothing fits, answer in plain text.",
        "",
        "Tools:",
    ]
    for tool in tools:
        params = ", ".join(sorted((tool.get("parameters") or {}).keys())) or "(none)"
        lines.append(
            f"  • {tool['name']} [{tool.get('safety_class', 'auto')}] — "
            f"{tool.get('description', '')} Params: {params}.",
        )
    lines.append(_TOOL_DECISION_GUIDE)
    lines.append("--- end tools ---")
    return "\n".join(lines)


def _format_vault_recall_block(recall: str | None) -> str:
    """Render an auto-folded vault chunk as a prompt block, or empty if None.

    Placed between live state and tools so the model treats it as durable
    background, not a tool result. The chunk text is sized + truncated by the
    caller (chat.py); this layer only frames it.
    """
    if not recall:
        return ""
    return (
        "\n\n--- Relevant note from your vault (auto-folded) ---\n"
        f"{recall}\n"
        "--- end note ---"
    )


def build_system_prompt(
    context: dict[str, Any],
    tools: list[dict[str, Any]] | None = None,
    vault_recall: str | None = None,
    findings: list[Any] | None = None,
) -> str:
    """Combine persona + live state + optional vault recall + tool instructions.

    ``findings`` is the explicit-diagnostic list from
    :func:`nvh.integrations.wizard.findings.derive_findings`. It is rendered
    AFTER the structural context block so the model sees "here is what's
    happening" and immediately after "here is what's broken." The findings
    block is omitted when there are no findings — silence is the signal.
    """
    from nvh.integrations.wizard.findings import render_findings_block

    block = _format_context_block(context)
    tools_block = _format_tools_block(tools or [])
    recall_block = _format_vault_recall_block(vault_recall)
    findings_block = render_findings_block(findings or [])
    findings_section = f"\n\n{findings_block}" if findings_block else ""
    return (
        f"{WIZARD_PERSONA}\n\n"
        "--- Live workspace state (this turn) ---\n"
        f"{block}\n"
        "--- end live state ---"
        f"{findings_section}"
        f"{recall_block}"
        f"{tools_block}\n\n"
        "Answer the user's next message using the live state when it's "
        "relevant. If a fact isn't in the live state, say so once and "
        "name what to check."
    )
