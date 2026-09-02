"""Wizard reconnect — UI-friendly summary of what happened since last boot.

``run_boot_preflight`` in ``nvh.integrations.diagnostics.boot_preflight`` already
records a host fingerprint, diffs against the previous session, and runs the
safe rootless auto-repairs. What it doesn't do is shape the output for a
"Welcome back" panel — that's the job of this module.

Goal: a single endpoint the WebUI can call once on chat mount, returning:

  - ``summary``: one-line plain-English status ("Welcome back — your workspace
    survived and 2 safe repairs ran.")
  - ``what_survived``: facts that matched the previous fingerprint (so the
    user sees that "yes, my mount + models + GPU stayed put").
  - ``what_changed``: facts that drifted (kernel update, CUDA bump, etc.).
  - ``auto_repaired``: repairs that ran successfully without user input.
  - ``needs_attention``: items the user must click — install receipts that
    point at missing files, a still-rotten cache, etc.
  - ``elapsed_ms``: how long the reconnect took.

This is the layer the AI Wizard chat reads from to greet the user — and the
one the WelcomeBackPanel UI component renders.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from nvh.integrations.diagnostics.boot_preflight import host_fingerprint, run_boot_preflight


def _label_for(fact_key: str) -> str:
    """Map an internal fingerprint key to a label the user will read."""
    # Inline so the module is self-contained; mirrors boot_preflight._FACT_LABELS.
    labels = {
        "distro": "Base OS",
        "kernel": "Kernel",
        "machine": "Architecture",
        "python_version": "Python",
        "gpu_name": "GPU",
        "gpu_memory_total_mb": "GPU memory",
        "driver_version": "NVIDIA driver",
        "cuda_version": "CUDA driver API",
        "node_version": "Node.js",
        "npm_version": "npm",
        "storage_home": "Workspace path",
        "storage_total_gb": "Workspace capacity",
        "storage_write_probe_ok": "Workspace writable",
        "torch_profile": "PyTorch profile",
        "gpu_architecture": "GPU architecture",
    }
    return labels.get(fact_key, fact_key.replace("_", " ").title())


def _change_fact(change: dict[str, Any]) -> str:
    """Fingerprint key a change record is about.

    ``boot_preflight.diff_fingerprints`` emits ``id`` / ``before`` / ``after``;
    the older reconnect shape (and the WelcomeBackPanel) uses ``fact`` /
    ``previous`` / ``current``. Accept both so a real preflight diff is read
    correctly and a drifted fact is never also listed as "survived".
    """
    return str(change.get("fact") or change.get("id") or "")


def _change_value(change: dict[str, Any], *keys: str) -> str:
    for key in keys:
        if key in change:
            value = change[key]
            return "—" if value in (None, "", "unknown", "missing") else str(value)
    return "—"


def _fingerprint_from(result: dict[str, Any]) -> dict[str, Any]:
    """The host fingerprint this boot was checked against.

    ``run_boot_preflight`` returns it as ``fingerprint``. A stubbed or older
    preflight may carry it under ``compatibility.fingerprint``; failing both,
    derive it from the compatibility report's ``host`` block with the same
    ``host_fingerprint`` boot_preflight uses — so ``machine`` (architecture),
    GPU and storage facts always reach the Welcome-back panel.
    """
    fingerprint = result.get("fingerprint")
    if isinstance(fingerprint, dict) and fingerprint:
        return fingerprint
    compat = result.get("compatibility") if isinstance(result.get("compatibility"), dict) else {}
    fingerprint = compat.get("fingerprint")
    if isinstance(fingerprint, dict) and fingerprint:
        return fingerprint
    if isinstance(compat.get("host"), dict):
        try:
            return host_fingerprint(compat)
        except Exception:
            return {}
    return {}


def _survived_facts(fingerprint: dict[str, Any], changes: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return the high-signal facts that did NOT drift since last session."""
    changed_keys = {_change_fact(change) for change in changes} - {""}
    high_signal = [
        "gpu_name",
        "gpu_memory_total_mb",
        "machine",  # aarch64 on DGX Spark — the Welcome-back panel should say so
        "driver_version",
        "cuda_version",
        "storage_home",
        "storage_total_gb",
        "python_version",
        "node_version",
    ]
    survived: list[dict[str, str]] = []
    for key in high_signal:
        if key in changed_keys:
            continue
        value = fingerprint.get(key)
        if value in (None, "", "unknown"):
            continue
        survived.append({"label": _label_for(key), "value": str(value)})
    return survived


def _change_label(change: dict[str, Any], fact: str) -> str:
    """User-facing label for one change record.

    ``boot_preflight.diff_fingerprints`` already supplies a ``label`` from its
    full ``_FACT_LABELS`` table, which knows preflight-only facts
    (``gpu_detection_status``, ``libc``, ``python_strategy``, ...). Prefer it;
    fall back to this module's smaller table, then to a title-cased key — so a
    preflight-only fact never surfaces as "Gpu Detection Status".
    """
    supplied = change.get("label")
    if isinstance(supplied, str) and supplied.strip():
        return supplied.strip()
    return _label_for(fact) if fact else ""


def _shape_changes(changes: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Translate raw fingerprint diffs into user-readable change records."""
    shaped: list[dict[str, str]] = []
    for change in changes:
        fact = _change_fact(change)
        shaped.append({
            "fact": fact,
            "label": _change_label(change, fact),
            "previous": _change_value(change, "previous", "before"),
            "current": _change_value(change, "current", "after"),
            "severity": str(change.get("severity", "info")),
        })
    return shaped


def _shape_auto_repair(repair: dict[str, Any] | None) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Split the auto-repair result into (auto_repaired, needs_attention)."""
    auto_repaired: list[dict[str, str]] = []
    needs_attention: list[dict[str, str]] = []
    if not isinstance(repair, dict):
        return auto_repaired, needs_attention

    # When run_safe_repairs has been called, the result has `completed` etc.
    completed = repair.get("completed") or []
    for item in completed:
        if not isinstance(item, dict):
            continue
        auto_repaired.append({
            "id": str(item.get("id", "")),
            "title": str(item.get("title", "")),
            "summary": str(item.get("summary", "")),
        })

    # The plan portion always reports actions that still need the user.
    plan = repair.get("plan") if isinstance(repair.get("plan"), dict) else repair
    for action in (plan.get("actions") or []) if isinstance(plan, dict) else []:
        if not isinstance(action, dict):
            continue
        if action.get("status") == "needs-user":
            needs_attention.append({
                "id": str(action.get("id", "")),
                "title": str(action.get("title", "")),
                "summary": str(action.get("summary", "")),
                "button_action_id": str(action.get("button_action_id", action.get("id", ""))),
            })
    return auto_repaired, needs_attention


def _greeting(
    *,
    first_run: bool,
    change_count: int,
    auto_count: int,
    needs_user_count: int,
) -> str:
    """Pick the right one-liner for the Welcome-back panel."""
    if first_run:
        if needs_user_count:
            return f"Welcome — your workspace is initializing. {needs_user_count} item(s) need your attention."
        return "Welcome — your workspace is initializing."
    if not change_count and not needs_user_count:
        if auto_count:
            return f"Welcome back — your workspace survived. {auto_count} safe repair(s) ran in the background."
        return "Welcome back — your workspace survived and everything looks healthy."
    parts: list[str] = ["Welcome back"]
    if auto_count:
        parts.append(f"— I ran {auto_count} safe repair(s)")
    if change_count:
        parts.append(f"and noticed {change_count} change(s) since last session")
    if needs_user_count:
        parts.append(f"({needs_user_count} item(s) need your input)")
    sentence = ", ".join(parts)
    if not sentence.endswith("."):
        sentence += "."
    return sentence


def wizard_reconnect(home_dir: str | Path | None = None) -> dict[str, Any]:
    """Run the boot-preflight reconnect routine and shape it for the UI.

    Calls ``run_boot_preflight`` (which fingerprints, diffs, and auto-repairs)
    then translates the raw result into the "Welcome back" payload described
    in this module's docstring.
    """
    started = time.monotonic()
    result = run_boot_preflight(home_dir=home_dir)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    changes_raw = result.get("changes") or []
    changes = _shape_changes(changes_raw if isinstance(changes_raw, list) else [])
    auto_repaired, needs_attention = _shape_auto_repair(result.get("auto_repair"))

    # The fingerprint boot_preflight just computed (see _fingerprint_from for
    # the fallbacks) is the source for "what survived".
    fingerprint = _fingerprint_from(result)
    survived = _survived_facts(fingerprint, changes_raw if isinstance(changes_raw, list) else [])

    summary = _greeting(
        first_run=bool(result.get("first_run")),
        change_count=len(changes),
        auto_count=len(auto_repaired),
        needs_user_count=len(needs_attention),
    )

    return {
        "summary": summary,
        "first_run": bool(result.get("first_run")),
        "changed": bool(result.get("changed")),
        "needs_attention": needs_attention,
        "auto_repaired": auto_repaired,
        "what_changed": changes,
        "what_survived": survived,
        "elapsed_ms": elapsed_ms,
        # Keep the raw preflight result accessible for clients that want it.
        "preflight": result,
    }
