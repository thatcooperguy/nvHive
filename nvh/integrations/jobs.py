"""Persistent background install jobs for rootless workstation setup."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nvh.integrations.storage import storage_layout

TERMINAL_STATUSES = {"complete", "failed", "canceled", "interrupted"}
RUNNING_STATUSES = {"queued", "running"}
_TASKS: dict[str, asyncio.Task[None]] = {}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def jobs_root() -> Path:
    root = storage_layout().home / "jobs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _job_path(job_id: str) -> Path:
    return jobs_root() / f"{job_id}.json"


def _events_path(job_id: str) -> Path:
    return jobs_root() / f"{job_id}.jsonl"


def _safe_job_id(job_id: str) -> str:
    cleaned = "".join(ch for ch in job_id if ch.isalnum() or ch in {"-", "_"})
    if not cleaned or cleaned != job_id:
        raise KeyError(f"Unknown job: {job_id}")
    return cleaned


def _write_job(job: dict[str, Any]) -> dict[str, Any]:
    path = _job_path(job["id"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(job, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return job


def _read_job(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def create_job(
    *,
    kind: str,
    title: str,
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a persistent queued job record."""
    now = _now()
    job_id = f"{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    job = {
        "id": job_id,
        "kind": kind,
        "title": title,
        "status": "queued",
        "message": "Queued",
        "progress": 0,
        "request": request or {},
        "storage_home": str(storage_layout().home),
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "completed_at": None,
        "event_count": 0,
        "cancel_requested": False,
        "events_path": str(_events_path(job_id)),
    }
    _events_path(job_id).write_text("", encoding="utf-8")
    return _write_job(job)


def load_job(job_id: str, *, reconcile: bool = True) -> dict[str, Any]:
    """Load one job record by id."""
    safe_id = _safe_job_id(job_id)
    path = _job_path(safe_id)
    if not path.exists():
        raise KeyError(f"Unknown job: {job_id}")
    job = _read_job(path)
    if reconcile:
        job = reconcile_job(job)
    return job


def list_jobs(
    *,
    kind: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return recent jobs, newest first."""
    jobs: list[dict[str, Any]] = []
    for path in sorted(jobs_root().glob("*.json"), reverse=True):
        try:
            job = reconcile_job(_read_job(path))
        except Exception:
            continue
        if kind and job.get("kind") != kind:
            continue
        if status and job.get("status") != status:
            continue
        jobs.append(job)
        if len(jobs) >= limit:
            break
    return jobs


def read_events(
    job_id: str,
    *,
    after: int = 0,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Read persisted events after a sequence number."""
    safe_id = _safe_job_id(job_id)
    events_path = _events_path(safe_id)
    if not events_path.exists():
        raise KeyError(f"Unknown job: {job_id}")

    events: list[dict[str, Any]] = []
    with events_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            event = json.loads(line)
            if int(event.get("sequence", 0)) <= after:
                continue
            events.append(event)
            if len(events) >= limit:
                break
    return events


def _progress_for_event(job: dict[str, Any], payload: dict[str, Any]) -> int:
    if payload.get("event") == "complete" or payload.get("status") == "complete":
        return 100
    if payload.get("event") == "error" or payload.get("status") == "failed":
        return int(job.get("progress", 0))
    if payload.get("event") == "plan":
        return max(int(job.get("progress", 0)), 5)
    if payload.get("event") in {"pack", "model"}:
        return min(90, max(int(job.get("progress", 0)), 15 + int(job.get("event_count", 0)) * 3))
    if payload.get("event") == "step" and payload.get("status") == "complete":
        return min(92, max(int(job.get("progress", 0)), 20 + int(job.get("event_count", 0)) * 2))
    if payload.get("event") == "log":
        return min(90, max(int(job.get("progress", 0)), 10 + int(job.get("event_count", 0))))
    return min(90, max(int(job.get("progress", 0)), 10))


def append_event(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Append one event to the job log and update the job summary."""
    job = load_job(job_id, reconcile=False)
    sequence = int(job.get("event_count", 0)) + 1
    now = _now()
    event = {
        "job_id": job_id,
        "sequence": sequence,
        "timestamp": now,
        "event": payload.get("event", "progress"),
        "status": payload.get("status", job.get("status", "running")),
        "message": payload.get("message", ""),
        "payload": payload,
    }
    with _events_path(job_id).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")

    job["event_count"] = sequence
    job["updated_at"] = now
    job["message"] = event["message"] or job.get("message", "")
    job["progress"] = _progress_for_event(job, payload)
    if job.get("status") == "queued":
        job["status"] = "running"
        job["started_at"] = job.get("started_at") or now
    if payload.get("event") == "complete" or payload.get("status") == "complete":
        job["status"] = "complete"
        job["progress"] = 100
        job["completed_at"] = now
    elif payload.get("event") == "error" or payload.get("status") == "failed":
        job["status"] = "failed"
        job["completed_at"] = now
    elif payload.get("event") == "canceled" or payload.get("status") == "canceled":
        job["status"] = "canceled"
        job["completed_at"] = now
    else:
        job["status"] = "running"
    _write_job(job)
    return event


def reconcile_job(job: dict[str, Any]) -> dict[str, Any]:
    """Mark orphaned running jobs from previous server sessions as interrupted."""
    if job.get("status") not in RUNNING_STATUSES:
        return job
    if job["id"] in _TASKS and not _TASKS[job["id"]].done():
        return job
    job["status"] = "interrupted"
    job["message"] = (
        "Server restarted or disconnected while this job was running. "
        "Retry to resume setup."
    )
    job["updated_at"] = _now()
    job["completed_at"] = job["updated_at"]
    return _write_job(job)


async def _consume_job_events(
    job_id: str,
    source_factory: Callable[[], AsyncIterator[dict[str, Any]]],
) -> None:
    try:
        job = load_job(job_id, reconcile=False)
        job["status"] = "running"
        job["started_at"] = job.get("started_at") or _now()
        job["message"] = "Starting"
        job["updated_at"] = job["started_at"]
        _write_job(job)

        saw_terminal = False
        async for payload in source_factory():
            current = load_job(job_id, reconcile=False)
            if current.get("cancel_requested"):
                append_event(
                    job_id,
                    {
                        "event": "canceled",
                        "status": "canceled",
                        "message": "Install job canceled by user.",
                    },
                )
                saw_terminal = True
                break
            append_event(job_id, payload)
            if payload.get("event") in {"complete", "error", "canceled"}:
                saw_terminal = True
                break

        if not saw_terminal:
            append_event(
                job_id,
                {
                    "event": "complete",
                    "status": "complete",
                    "message": "Install job complete.",
                },
            )
    except asyncio.CancelledError:
        append_event(
            job_id,
            {
                "event": "canceled",
                "status": "canceled",
                "message": "Install job canceled by user.",
            },
        )
        raise
    except Exception as exc:
        append_event(
            job_id,
            {
                "event": "error",
                "status": "failed",
                "message": str(exc),
            },
        )
    finally:
        _TASKS.pop(job_id, None)


def start_job(
    *,
    kind: str,
    title: str,
    request: dict[str, Any] | None,
    source_factory: Callable[[], AsyncIterator[dict[str, Any]]],
) -> dict[str, Any]:
    """Create and start a background job for an async event source."""
    job = create_job(kind=kind, title=title, request=request)
    task = asyncio.create_task(_consume_job_events(job["id"], source_factory))
    _TASKS[job["id"]] = task
    return load_job(job["id"], reconcile=False)


def cancel_job(job_id: str) -> dict[str, Any]:
    """Request cancellation for a running job."""
    job = load_job(job_id, reconcile=False)
    if job.get("status") in TERMINAL_STATUSES:
        return job
    job["cancel_requested"] = True
    job["updated_at"] = _now()
    job["message"] = "Cancel requested"
    _write_job(job)
    task = _TASKS.get(job["id"])
    if task and not task.done():
        task.cancel()
    else:
        append_event(
            job["id"],
            {
                "event": "canceled",
                "status": "canceled",
                "message": "Install job canceled by user.",
            },
        )
    return load_job(job_id, reconcile=False)
