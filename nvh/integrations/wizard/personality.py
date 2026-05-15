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


def build_system_prompt(context: dict[str, Any]) -> str:
    """Combine the persona block with this turn's live state block."""
    block = _format_context_block(context)
    return (
        f"{WIZARD_PERSONA}\n\n"
        "--- Live workspace state (this turn) ---\n"
        f"{block}\n"
        "--- end live state ---\n"
        "Answer the user's next message using the live state when it's "
        "relevant. If a fact isn't in the live state, say so once and "
        "name what to check."
    )
