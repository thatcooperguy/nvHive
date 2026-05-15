"""Derive explicit diagnostic findings from the Wizard's live context.

The Wizard already gets a structural snapshot of workspace state via
:func:`nvh.integrations.wizard.context.wizard_context`. That snapshot tells
the Wizard "what is true" (e.g. ``gpu.detected = False``, ``providers = []``)
but does not surface "what is broken and why."

This module bridges that gap. Each :class:`Finding` is a single, actionable
issue with:

* a stable ``id`` (e.g. ``gpu-missing``, ``no-providers``) — used by the
  setup page to link to ``/wizard?issue=<id>`` so the Wizard can pre-load
  the right starter prompt;
* a ``severity`` (``info`` / ``warn`` / ``error``) — drives setup-page
  badge color and the Wizard's tone;
* a ``category`` (``gpu`` / ``storage`` / ``providers`` / ``models`` /
  ``runtime``) — used for grouping in System Check;
* a ``title`` and ``detail`` the user reads verbatim;
* ``suggested_tool`` — if any Wizard tool can fix it autonomously, the
  Wizard prompt knows to suggest that exact action.

The same finding list feeds three surfaces:

1. The ``/v1/wizard/diagnostics`` endpoint (consumed by the setup page).
2. The Wizard system prompt (so chat answers reflect current findings).
3. The ``diagnose`` Wizard tool (so the agent can refresh mid-turn).
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)


Severity = Literal["info", "warn", "error"]
Category = Literal["gpu", "storage", "providers", "models", "runtime", "workspace"]


@dataclass(frozen=True)
class Finding:
    """A single explicit diagnostic surface item.

    ``id`` must be stable across runs — the WebUI uses it as a deep-link key
    in ``/wizard?issue=<id>``. New finding types may be added freely, but
    don't rename existing ids without checking the setup-page render.
    """

    id: str
    severity: Severity
    category: Category
    title: str
    detail: str
    suggested_tool: str | None = None
    suggested_tool_args: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def derive_findings(context: dict[str, Any]) -> list[Finding]:
    """Return the ordered list of active findings for this context snapshot.

    Order is "most actionable first" so a user reading top-down hits the
    likely first fix early. Within a severity tier, the order is
    runtime → providers → models → gpu → storage → workspace so the
    user solves "is anything running?" before "should I install another
    model?".
    """
    findings: list[Finding] = []

    findings.extend(_runtime_findings(context))
    findings.extend(_provider_findings(context))
    findings.extend(_model_findings(context))
    findings.extend(_gpu_findings(context))
    findings.extend(_storage_findings(context))
    findings.extend(_workspace_findings(context))

    # Stable within severity: errors before warns before info.
    severity_order = {"error": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: severity_order.get(f.severity, 99))
    return findings


# ---------------------------------------------------------------------------
# Per-category derivations
# ---------------------------------------------------------------------------


def _runtime_findings(context: dict[str, Any]) -> list[Finding]:
    """Recent install jobs that failed are runtime issues the Wizard can repair."""
    out: list[Finding] = []
    jobs = context.get("recent_jobs") or []
    failed = [j for j in jobs if j.get("status") in {"failed", "interrupted"}]
    for j in failed[:3]:
        kind = j.get("kind") or "job"
        title = j.get("title") or kind
        out.append(Finding(
            id=f"job-failed-{j.get('id', kind)}",
            severity="error",
            category="runtime",
            title=f"Install job failed: {title}",
            detail=(j.get("message") or "Last run failed. The Wizard can re-attempt the "
                    "repair flow or surface the underlying log."),
            suggested_tool="repair_workspace",
        ))
    return out


def _provider_findings(context: dict[str, Any]) -> list[Finding]:
    """Provider-key + provider-health diagnostics."""
    out: list[Finding] = []
    providers = context.get("providers") or []

    if not providers:
        out.append(Finding(
            id="no-providers",
            severity="warn",
            category="providers",
            title="No cloud providers configured yet",
            detail=("nvHive runs without a cloud key — local Ollama still works — but "
                    "configuring at least one free-tier provider unlocks smart routing "
                    "and council mode. The Wizard can validate a key inline."),
            suggested_tool="validate_provider_key",
        ))
        return out

    unhealthy = [p for p in providers if p.get("healthy") is False]
    for p in unhealthy[:5]:
        name = p.get("name") or "provider"
        out.append(Finding(
            id=f"provider-unhealthy-{name}",
            severity="error",
            category="providers",
            title=f"Provider not healthy: {name}",
            detail=(p.get("error") or "Health check failed. Re-validate the key or "
                    "check the provider's status page."),
            suggested_tool="validate_provider_key",
            suggested_tool_args={"provider": name},
        ))
    return out


def _model_findings(context: dict[str, Any]) -> list[Finding]:
    """Local Ollama model presence."""
    out: list[Finding] = []
    models = context.get("ollama_models") or []
    if not models:
        out.append(Finding(
            id="no-local-models",
            severity="warn",
            category="models",
            title="No local Ollama models installed",
            detail=("Without a local model, safe mode and offline replies aren't "
                    "available. The Wizard can refresh the Ollama list and suggest "
                    "the smallest model that fits your GPU."),
            suggested_tool="refresh_models",
        ))
    return out


def _gpu_findings(context: dict[str, Any]) -> list[Finding]:
    """GPU presence — informational unless a GPU profile was selected."""
    out: list[Finding] = []
    gpu = context.get("gpu") or {}
    if not gpu.get("detected"):
        out.append(Finding(
            id="gpu-missing",
            severity="info",
            category="gpu",
            title="No NVIDIA GPU detected",
            detail=("nvHive still runs in CPU mode — local Ollama uses the CPU "
                    "(slow but functional). If you expected a GPU, check that the "
                    "rented instance was provisioned with one and ``nvidia-smi`` "
                    "runs from this shell."),
        ))
    return out


def _storage_findings(context: dict[str, Any]) -> list[Finding]:
    """Persistent-mount state for the rented-GPU-desktop case."""
    out: list[Finding] = []
    storage = context.get("storage") or {}
    if storage.get("available") is False:
        out.append(Finding(
            id="storage-unavailable",
            severity="error",
            category="storage",
            title="NVH_HOME storage probe failed",
            detail="Could not read storage status. Workspace may be misconfigured.",
            suggested_tool="repair_workspace",
        ))
        return out
    if storage.get("warnings"):
        out.append(Finding(
            id="storage-warnings",
            severity="warn",
            category="storage",
            title="Storage has warnings",
            detail="; ".join(storage.get("warnings") or [])[:300],
            suggested_tool="repair_workspace",
        ))
    elif storage.get("ok") is False:
        out.append(Finding(
            id="storage-not-ok",
            severity="warn",
            category="storage",
            title="Storage not on a persistent mount",
            detail=("On ephemeral cloud desktops, your NVH_HOME should point at the "
                    "persistent block mount, not ``~/.nvh``. Otherwise every reboot "
                    "loses your installed models and config."),
        ))
    return out


def _workspace_findings(context: dict[str, Any]) -> list[Finding]:
    """Unhealthy install receipts surface as workspace findings."""
    out: list[Finding] = []
    receipts = context.get("receipts") or {}
    unhealthy = receipts.get("unhealthy") or 0
    if unhealthy:
        out.append(Finding(
            id="receipts-unhealthy",
            severity="warn",
            category="workspace",
            title=f"{unhealthy} install receipt(s) need attention",
            detail=("One or more components were installed but the last receipt check "
                    "failed. The Wizard can run the safe-repair loop."),
            suggested_tool="repair_workspace",
        ))
    return out


# ---------------------------------------------------------------------------
# Rendering for the Wizard system prompt
# ---------------------------------------------------------------------------


def render_findings_block(findings: list[Finding]) -> str:
    """Render findings as a compact, model-readable block.

    Empty when there are no findings — the absence of the block is the
    signal "everything looks healthy this turn." This keeps the prompt
    short on the happy path.
    """
    if not findings:
        return ""

    lines = ["--- Active diagnostic findings (this turn) ---"]
    for f in findings:
        tool_hint = ""
        if f.suggested_tool:
            tool_hint = f" [fix via tool `{f.suggested_tool}`]"
        lines.append(
            f"  [{f.severity}] {f.id} ({f.category}): {f.title}{tool_hint}"
        )
        lines.append(f"      {f.detail}")
    lines.append("--- end findings ---")
    lines.append(
        "Be concrete: when the user asks 'what's wrong' or 'fix it', name "
        "the specific finding ids above and (if a tool is suggested) call it."
    )
    return "\n".join(lines)
