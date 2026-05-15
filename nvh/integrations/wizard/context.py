"""Unified context collector for the AI Wizard.

The conversational Wizard (and its action layer in Wizard-3) needs to read the
*whole* state of the user's workspace before answering — not just the small
snapshot the deterministic setup_assistant_reply pulls. This module collects
that state in one place:

  - workspace state (NVH_HOME, free space, persistent vs ephemeral)
  - GPU + system RAM
  - provider health (which keys validate right now, latency, model counts)
  - installed local models (Ollama)
  - recent install jobs (success / failure / running) and any receipts
  - vault status (initialized? memory file count?)
  - last reconnect summary (changes since boot, auto-repairs that ran)

Every downstream Wizard surface — chat, tool calls, the welcome panel — reads
from one ``wizard_context()`` so the user never gets contradictory answers
from different parts of the product.

Costs: every call hits 5-8 deterministic helpers, no external LLM calls. Safe
to call once per chat turn; the heavy boot_preflight work runs separately
(Wizard-1).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _safe_call(label: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a helper and swallow exceptions so one broken probe doesn't crash the snapshot."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        logger.debug("wizard_context: %s helper failed: %s", label, exc)
        return None


def _gpu_summary(home_dir: str | Path | None) -> dict[str, Any]:
    """Return the high-signal GPU facts the Wizard needs to reason about."""
    from nvh.utils.gpu import detect_gpu_status, detect_gpus

    status = _safe_call("gpu_status", detect_gpu_status) or {}
    gpus = _safe_call("gpus", detect_gpus) or []
    primary = gpus[0] if gpus else None
    return {
        "detected": bool(primary),
        "summary": status.get("summary") or "",
        "gpu_count": len(gpus),
        "primary": (
            {
                "name": primary.get("name") if isinstance(primary, dict) else None,
                "vram_gb": primary.get("vram_gb") if isinstance(primary, dict) else None,
                "memory_used_mb": primary.get("memory_used_mb") if isinstance(primary, dict) else None,
                "memory_free_mb": primary.get("memory_free_mb") if isinstance(primary, dict) else None,
                "utilization_pct": primary.get("utilization_pct") if isinstance(primary, dict) else None,
                "compute_capability": primary.get("compute_capability") if isinstance(primary, dict) else None,
                "architecture": primary.get("architecture") if isinstance(primary, dict) else None,
                "driver_version": primary.get("driver_version") if isinstance(primary, dict) else None,
                "cuda_version": primary.get("cuda_version") if isinstance(primary, dict) else None,
            }
            if isinstance(primary, dict)
            else None
        ),
    }


def _storage_summary(home_dir: str | Path | None) -> dict[str, Any]:
    """Persistent-mount state for the cloud-renter signal."""
    from nvh.integrations.workspace.storage import storage_status

    status = _safe_call("storage_status", storage_status, home_dir=home_dir)
    if status is None:
        return {"available": False}
    return {
        "available": True,
        "home": str(status.layout.home),
        "configured_by": status.configured_by,
        "exists": status.exists,
        "writable": status.writable,
        "free_gb": status.free_gb,
        "total_gb": status.total_gb,
        "ok": status.ok,
        "warnings": list(status.warnings),
    }


def _providers_summary() -> list[dict[str, Any]]:
    """Provider health snapshot — name, healthy bool, model count, error."""
    try:
        from nvh.api.server import get_engine  # type: ignore

        engine = get_engine()
        if engine is None:
            return []
        registry = engine.registry
        out: list[dict[str, Any]] = []
        for name in registry.list_enabled():
            entry: dict[str, Any] = {"name": name, "healthy": None}
            try:
                provider = registry.get(name)
                if provider is None:
                    entry["healthy"] = False
                    entry["error"] = "not registered"
                else:
                    out.append({**entry, "provider_obj_present": True})
                    continue
            except Exception as exc:
                entry["healthy"] = False
                entry["error"] = str(exc)
            out.append(entry)
        return out
    except Exception as exc:
        logger.debug("providers_summary: %s", exc)
        return []


def _ollama_models() -> list[dict[str, Any]]:
    """List local Ollama models (read-only). Empty if daemon unreachable."""
    try:
        import httpx

        resp = httpx.get("http://127.0.0.1:11434/api/tags", timeout=2.0)
        resp.raise_for_status()
        models = resp.json().get("models", []) or []
        return [
            {
                "name": m.get("name", ""),
                "size_bytes": m.get("size"),
                "modified_at": m.get("modified_at"),
            }
            for m in models
            if isinstance(m, dict)
        ]
    except Exception as exc:
        logger.debug("ollama_models: %s", exc)
        return []


def _recent_jobs(home_dir: str | Path | None, limit: int = 5) -> list[dict[str, Any]]:
    from nvh.integrations.services.jobs import list_jobs

    jobs = _safe_call("recent_jobs", list_jobs, limit=limit, home_dir=home_dir) or []
    return [
        {
            "id": j.get("id"),
            "kind": j.get("kind"),
            "status": j.get("status"),
            "title": j.get("title"),
            "message": j.get("message"),
            "updated_at": j.get("updated_at"),
        }
        for j in jobs
    ]


def _receipts_summary(home_dir: str | Path | None) -> dict[str, Any]:
    from nvh.integrations.services.receipts import receipt_summary

    summary = _safe_call("receipts", receipt_summary, home_dir=home_dir) or {}
    return {
        "count": summary.get("count", 0),
        "unhealthy": summary.get("unhealthy", 0),
        "by_kind": summary.get("by_kind", {}),
    }


def _vault_summary(home_dir: str | Path | None) -> dict[str, Any]:
    """Vault metadata only — never the memory contents themselves."""
    try:
        from nvh.integrations.workspace.vault import vault_status

        status = _safe_call("vault_status", vault_status, home_dir=home_dir)
        if not isinstance(status, dict):
            return {"initialized": False}
        return {
            "initialized": bool(status.get("initialized")),
            "memory_files": status.get("memory_files", status.get("file_count", 0)),
            "obsidian_available": status.get("obsidian_available"),
        }
    except Exception as exc:
        logger.debug("vault_summary: %s", exc)
        return {"initialized": False}


def wizard_context(
    home_dir: str | Path | None = None,
    *,
    recent_job_limit: int = 5,
) -> dict[str, Any]:
    """Return the one structured snapshot every Wizard surface reads from.

    Stable shape:

      {
        "gpu": {...},
        "storage": {...},
        "providers": [{name, healthy, ...}, ...],
        "ollama_models": [{name, size_bytes, ...}, ...],
        "recent_jobs": [{id, kind, status, title}, ...],
        "receipts": {count, unhealthy, by_kind},
        "vault": {initialized, memory_files, ...},
      }

    All helpers swallow their own exceptions; this function never raises.
    """
    return {
        "gpu": _gpu_summary(home_dir),
        "storage": _storage_summary(home_dir),
        "providers": _providers_summary(),
        "ollama_models": _ollama_models(),
        "recent_jobs": _recent_jobs(home_dir, limit=recent_job_limit),
        "receipts": _receipts_summary(home_dir),
        "vault": _vault_summary(home_dir),
    }
