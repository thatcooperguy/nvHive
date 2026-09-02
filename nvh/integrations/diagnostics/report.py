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

from nvh.integrations.workspace.storage import storage_layout, storage_status
from nvh.utils.hw_ids import detect_machine
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
    "|".join(
        [
            r"sk-[A-Za-z0-9_\-]{8,}",
            r"Bearer\s+[A-Za-z0-9._\-]{8,}",
            r"Basic\s+[A-Za-z0-9+/=._\-]{8,}",
            r"gh[pousr]_[A-Za-z0-9_]{8,}",
            r"hf_[A-Za-z0-9_\-]{8,}",
            r"nvapi-[A-Za-z0-9_\-]{8,}",
            r"ngc_[A-Za-z0-9_\-]{8,}",
            r"(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)=([^&\s]+)",
            r"X-Amz-Signature=[A-Fa-f0-9]{16,}",
            r"Signature=[A-Za-z0-9%._\-]{16,}",
        ]
    ),
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


def _path_replacements(layout: Any) -> list[tuple[str, str]]:
    paths: list[tuple[str, str]] = []
    for raw, replacement in (
        (getattr(layout, "home", None), "$NVH_HOME"),
        (Path.home(), "~"),
    ):
        if raw is None:
            continue
        path = str(raw)
        if path:
            paths.append((path, replacement))
            paths.append((path.replace("\\", "/"), replacement))
    return sorted(set(paths), key=lambda item: len(item[0]), reverse=True)


def _redact_paths(value: Any, layout: Any) -> Any:
    if isinstance(value, str):
        redacted = value
        for needle, replacement in _path_replacements(layout):
            redacted = redacted.replace(needle, replacement)
        return redacted
    if isinstance(value, list):
        return [_redact_paths(item, layout) for item in value]
    if isinstance(value, dict):
        return {key: _redact_paths(item, layout) for key, item in value.items()}
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
    home = logs_dir.parent
    paths.extend(
        [
            logs_dir / "api.log",
            logs_dir / "nvhive.log",
            logs_dir / "install.log",
            logs_dir / "ollama-install.log",
            logs_dir / "model-pull.log",
            logs_dir / "webui-bootstrap.log",
            home / "comfyui" / "comfyui.log",
            home / "studio" / "ollama.log",
            home / "studio" / "openclaw.log",
            home / "studio" / "nemoclaw.log",
        ]
    )
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
        if path.exists() or path == Path(explicit or ""):
            unique.append(path)
        if len(unique) >= 10:
            break
    return unique


def _tail_log_file(path: Path, *, max_lines: int) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 256_000))
            data = handle.read()
        lines = data.decode("utf-8", errors="replace").splitlines()
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


def _registry_checks(home_dir: str | Path | None = None) -> dict[str, Any]:
    """The ``nvh status --deep`` rows, so the Wizard and the CLI read one registry."""
    from nvh.integrations.diagnostics.checks import DEEP, CheckContext, run_checks_sync, summarize

    ctx = CheckContext(home_dir=str(home_dir) if home_dir else None)
    return summarize(run_checks_sync(DEEP, ctx))


def diagnostics_report(
    home_dir: str | Path | None = None,
    *,
    request_id: str | None = None,
    include_logs: bool = True,
    log_lines: int = 80,
    smoke_tests: dict[str, Any] | None = None,
    registry_checks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a redacted support report that is safe to copy from the wizard.

    ``smoke_tests`` / ``registry_checks`` accept results a caller already
    has (``nvh status --report`` runs both before writing the snapshot) so
    they are embedded instead of run a second time.
    """
    checked_at = _now()
    layout = storage_layout(home_dir)
    request_id = request_id or get_request_id()
    report_id = f"diag-{checked_at.replace(':', '').replace('.', '-')[:19]}"
    if request_id:
        report_id = f"{report_id}-{request_id[-8:]}"

    storage = _safe_call("storage", lambda: storage_status(home_dir=home_dir).as_dict())

    def _readiness() -> dict[str, Any]:
        from nvh.integrations.diagnostics.production_readiness import production_readiness_report

        return production_readiness_report(home_dir=home_dir)

    def _jobs() -> dict[str, Any]:
        from nvh.integrations.services.jobs import list_jobs, read_events

        jobs = list_jobs(limit=8, home_dir=layout.home)
        failed = [job for job in jobs if job.get("status") in {"failed", "interrupted"}]
        failed_event_tails: list[dict[str, Any]] = []
        for job in failed[:3]:
            job_id = str(job.get("id", ""))
            if not job_id:
                continue
            events = read_events(job_id, limit=80, home_dir=layout.home)
            failed_event_tails.append(
                {
                    "job_id": job_id,
                    "kind": job.get("kind"),
                    "status": job.get("status"),
                    "message": job.get("message"),
                    "events": events[-25:],
                }
            )
        return {
            "count": len(jobs),
            "failed_or_interrupted": len(failed),
            "jobs": jobs,
            "failed_event_tails": failed_event_tails,
        }

    def _receipts() -> dict[str, Any]:
        from nvh.integrations.services.receipts import receipt_summary

        return receipt_summary(home_dir=layout.home)

    def _local_ai_runtime() -> dict[str, Any]:
        from nvh.integrations.installs.studio_packs import ollama_runtime_doctor

        return ollama_runtime_doctor(home_dir=layout.home)

    def _workspace_state() -> dict[str, Any]:
        from nvh.integrations.diagnostics.workspace_state import workspace_state

        report = workspace_state(home_dir=layout.home)
        return {
            "phase": report.get("phase"),
            "ready": report.get("ready"),
            "summary": report.get("summary"),
            "health_score": report.get("health_score"),
            "next_action": report.get("next_action"),
            "checks": report.get("checks", []),
        }

    def _compatibility() -> dict[str, Any]:
        from nvh.integrations.diagnostics.compatibility import compatibility_report

        report = compatibility_report(home_dir=layout.home)
        return {
            "summary": report.get("summary"),
            "ready": report.get("ready"),
            "issue_count": report.get("issue_count"),
            "blocked_count": report.get("blocked_count"),
            "rootless_fixable_count": report.get("rootless_fixable_count"),
            "recommended_torch_profile": report.get("recommended_torch_profile"),
            "apps": [
                {
                    "id": app.get("id"),
                    "title": app.get("title"),
                    "status": app.get("status"),
                    "summary": app.get("summary"),
                    "recommended_action_id": app.get("recommended_action_id"),
                }
                for app in report.get("apps", [])
            ],
        }

    def _smoke_tests() -> dict[str, Any]:
        if smoke_tests is not None:
            return smoke_tests
        from nvh.integrations.diagnostics.smoke_tests import smoke_test_report

        return smoke_test_report(home_dir=str(layout.home))

    def _registry() -> dict[str, Any]:
        if registry_checks is not None:
            return registry_checks
        return _registry_checks(home_dir=layout.home)

    diagnostics = {
        "report_id": report_id,
        "checked_at": checked_at,
        "request_id": request_id,
        "summary": "Redacted rootless setup diagnostics for nvHive support.",
        "environment": {
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            # hw_ids.detect_machine() is WOW64-aware: an x64 Python under
            # Windows-on-Arm emulation reports AMD64 from platform.machine().
            # compatibility.host.machine already uses it, so the two sections
            # agree; the raw value is kept alongside for support triage.
            "machine": detect_machine(),
            "machine_raw": platform.machine(),
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
            "registry": _safe_call("registry", _registry),
            "local_ai_runtime": _safe_call("local_ai_runtime", _local_ai_runtime),
            "workspace_state": _safe_call("workspace_state", _workspace_state),
            "compatibility": _safe_call("compatibility", _compatibility),
            "smoke_tests": _safe_call("smoke_tests", _smoke_tests),
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
    safe_report = _redact_paths(_redact("diagnostics", diagnostics), layout)
    return json.loads(json.dumps(safe_report, default=str))
