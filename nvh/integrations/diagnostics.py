"""Redacted diagnostics reports for no-root setup support."""

from __future__ import annotations

import json
import os
import platform
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nvh.integrations.storage import storage_layout, storage_status
from nvh.utils.logging import get_request_id

ENV_ALLOWLIST = [
    "NVH_HOME",
    "NVH_LOGS",
    "NVH_RUNTIME_HOME",
    "NVH_APPS_HOME",
    "NVH_WEB_HOME",
    "NVH_STUDIO_HOME",
    "COMFYUI_HOME",
    "OLLAMA_MODELS",
    "HIVE_CONFIG_HOME",
    "HIVE_LOG_LEVEL",
    "HIVE_LOG_FORMAT",
    "NVH_BOOT_PREFLIGHT",
    "NVH_USE_BINARY",
]

SECRET_KEY_RE = re.compile(r"(api[_-]?key|token|secret|password|authorization|bearer)", re.I)
SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,})",
    re.I,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _redact_text(value: str) -> str:
    return SECRET_VALUE_RE.sub("[redacted]", value)


def _redact(key: str, value: Any) -> Any:
    if value is None:
        return None
    if SECRET_KEY_RE.search(key):
        return "[redacted]"
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_redact(key, item) for item in value]
    if isinstance(value, dict):
        return {str(k): _redact(str(k), v) for k, v in value.items()}
    return value


def _safe_call(label: str, fn: Callable[[], Any]) -> dict[str, Any]:
    try:
        return {"ok": True, "data": _redact(label, fn())}
    except Exception as exc:
        return {
            "ok": False,
            "error": {
                "type": type(exc).__name__,
                "message": _redact_text(str(exc)),
            },
        }


def _candidate_log_files(logs_dir: Path) -> list[Path]:
    paths: list[Path] = []
    explicit = os.environ.get("HIVE_LOG_FILE")
    if explicit:
        paths.append(Path(explicit).expanduser())
    try:
        paths.extend(sorted(logs_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True))
    except Exception:
        pass

    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
        if len(unique) >= 5:
            break
    return unique


def _tail_log_file(path: Path, *, max_lines: int) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    interesting = [
        _redact_text(line)
        for line in lines[-500:]
        if any(marker in line.lower() for marker in ("error", "warning", "failed", "traceback"))
    ]
    return interesting[-max_lines:]


def _recent_log_entries(logs_dir: Path, *, max_lines: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    remaining = max(0, max_lines)
    for path in _candidate_log_files(logs_dir):
        if remaining <= 0:
            break
        lines = _tail_log_file(path, max_lines=remaining)
        if not lines:
            continue
        entries.append(
            {
                "path": str(path),
                "lines": lines,
            }
        )
        remaining -= len(lines)
    return entries


def diagnostics_report(
    home_dir: str | Path | None = None,
    *,
    request_id: str | None = None,
    include_logs: bool = True,
    log_lines: int = 80,
) -> dict[str, Any]:
    """Build a redacted support report that is safe to copy from the wizard."""
    checked_at = _now()
    layout = storage_layout(home_dir)
    request_id = request_id or get_request_id()
    report_id = f"diag-{checked_at.replace(':', '').replace('.', '-')[:19]}"
    if request_id:
        report_id = f"{report_id}-{request_id[-8:]}"

    storage = _safe_call("storage", lambda: storage_status(home_dir=home_dir).as_dict())

    def _readiness() -> dict[str, Any]:
        from nvh.integrations.production_readiness import production_readiness_report

        return production_readiness_report(home_dir=home_dir)

    def _jobs() -> dict[str, Any]:
        from nvh.integrations.jobs import list_jobs

        jobs = list_jobs(limit=8)
        failed = [job for job in jobs if job.get("status") in {"failed", "interrupted"}]
        return {
            "count": len(jobs),
            "failed_or_interrupted": len(failed),
            "jobs": jobs,
        }

    def _receipts() -> dict[str, Any]:
        from nvh.integrations.receipts import receipt_summary

        return receipt_summary()

    diagnostics = {
        "report_id": report_id,
        "checked_at": checked_at,
        "request_id": request_id,
        "summary": "Redacted rootless setup diagnostics for nvHive support.",
        "environment": {
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "executable": sys.executable,
            "cwd": str(Path.cwd()),
            "env": {
                key: _redact(key, os.environ.get(key))
                for key in ENV_ALLOWLIST
                if os.environ.get(key) is not None
            },
        },
        "paths": {
            "home": str(layout.home),
            "logs": str(layout.logs_dir),
            "jobs": str(layout.home / "jobs"),
            "config": str(layout.config_dir),
            "models": str(layout.models_dir),
            "apps": str(layout.apps_dir),
        },
        "checks": {
            "storage": storage,
            "production_readiness": _safe_call("production_readiness", _readiness),
            "jobs": _safe_call("jobs", _jobs),
            "receipts": _safe_call("receipts", _receipts),
        },
        "logs": {
            "included": include_logs,
            "files": [str(path) for path in _candidate_log_files(layout.logs_dir)],
            "recent": _recent_log_entries(layout.logs_dir, max_lines=max(0, min(log_lines, 200)))
            if include_logs
            else [],
        },
    }

    # Last-pass redaction catches nested messages from third-party tools.
    return json.loads(json.dumps(_redact("diagnostics", diagnostics), default=str))
