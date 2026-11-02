"""Persistent background install jobs for rootless workstation setup."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nvh.integrations.workspace.storage import storage_layout

TERMINAL_STATUSES = {"complete", "failed", "canceled", "interrupted"}
RUNNING_STATUSES = {"queued", "running"}
_TASKS: dict[str, asyncio.Task[None]] = {}
logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def jobs_root(home_dir: str | Path | None = None) -> Path:
    root = storage_layout(home_dir).home / "jobs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _job_path(job_id: str, home_dir: str | Path | None = None) -> Path:
    return jobs_root(home_dir) / f"{job_id}.json"


def _events_path(job_id: str, home_dir: str | Path | None = None) -> Path:
    return jobs_root(home_dir) / f"{job_id}.jsonl"


def _safe_job_id(job_id: str) -> str:
    cleaned = "".join(ch for ch in job_id if ch.isalnum() or ch in {"-", "_"})
    if not cleaned or cleaned != job_id:
        raise KeyError(f"Unknown job: {job_id}")
    return cleaned


def _write_job(job: dict[str, Any], home_dir: str | Path | None = None) -> dict[str, Any]:
    path = _job_path(job["id"], home_dir)
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
    logger.info(
        "Setup job queued",
        extra={"job_id": job_id, "kind": kind},
    )
    return _write_job(job)


def load_job(
    job_id: str,
    *,
    reconcile: bool = True,
    include_recent_events: bool = True,
    home_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Load one job record by id."""
    safe_id = _safe_job_id(job_id)
    path = _job_path(safe_id, home_dir)
    if not path.exists():
        raise KeyError(f"Unknown job: {job_id}")
    job = _read_job(path)
    if reconcile:
        job = reconcile_job(job, home_dir=home_dir)
    if include_recent_events:
        job = with_recent_events(job, home_dir=home_dir)
    return job


def list_jobs(
    *,
    kind: str | None = None,
    status: str | None = None,
    limit: int = 20,
    home_dir: str | Path | None = None,
    schedule_id: str | None = None,
    exclude_kind: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent jobs, newest first.

    Every filter is applied before ``limit``: ``kind`` / ``exclude_kind`` on
    the job's kind (the Setup page passes ``exclude_kind="schedule-run"`` so
    an hourly schedule never pushes its install jobs out of view), ``status``
    on the reconciled status, and ``schedule_id`` on
    ``request.schedule_id`` (a schedule's run log).
    """
    jobs: list[dict[str, Any]] = []
    for path in sorted(jobs_root(home_dir).glob("*.json"), reverse=True):
        try:
            job = reconcile_job(_read_job(path), home_dir=home_dir)
        except Exception:
            continue
        if kind and job.get("kind") != kind:
            continue
        if exclude_kind and job.get("kind") == exclude_kind:
            continue
        if status and job.get("status") != status:
            continue
        if schedule_id and (job.get("request") or {}).get("schedule_id") != schedule_id:
            continue
        jobs.append(with_recent_events(job, home_dir=home_dir))
        if len(jobs) >= limit:
            break
    return jobs


def read_events(
    job_id: str,
    *,
    after: int = 0,
    limit: int = 200,
    home_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Read persisted events after a sequence number."""
    safe_id = _safe_job_id(job_id)
    events_path = _events_path(safe_id, home_dir)
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


def with_recent_events(
    job: dict[str, Any],
    *,
    limit: int = 6,
    home_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Attach a compact tail of recent events for list/detail UI status."""
    job_id = str(job.get("id", ""))
    event_count = max(0, int(job.get("event_count", 0) or 0))
    if not job_id or event_count <= 0:
        job["recent_events"] = []
        return job
    try:
        after = max(0, event_count - max(1, limit))
        job["recent_events"] = read_events(job_id, after=after, limit=limit, home_dir=home_dir)
    except Exception:
        job["recent_events"] = []
    return job


def _progress_for_event(job: dict[str, Any], payload: dict[str, Any]) -> int:
    if isinstance(payload.get("progress"), (int, float)):
        return max(0, min(100, int(payload["progress"])))
    if payload.get("event") == "complete":
        return 100
    if payload.get("event") == "error" or payload.get("status") == "failed":
        return int(job.get("progress", 0))
    if payload.get("event") == "plan":
        return max(int(job.get("progress", 0)), 5)
    if payload.get("event") in {"pack", "model"}:
        return min(90, max(int(job.get("progress", 0)), 15 + int(job.get("event_count", 0)) * 3))
    if payload.get("event") == "stage-complete" or (
        payload.get("event") == "step" and payload.get("status") == "complete"
    ):
        return min(92, max(int(job.get("progress", 0)), 20 + int(job.get("event_count", 0)) * 2))
    if payload.get("event") == "log":
        return min(90, max(int(job.get("progress", 0)), 10 + int(job.get("event_count", 0))))
    return min(90, max(int(job.get("progress", 0)), 10))


def append_event(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Append one event to the job log and update the job summary."""
    job = load_job(job_id, reconcile=False, include_recent_events=False)
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
    if payload.get("event") == "complete":
        job["status"] = "complete"
        job["progress"] = 100
        job["completed_at"] = now
        logger.info(
            "Setup job complete",
            extra={"job_id": job_id, "kind": job.get("kind", "")},
        )
    elif payload.get("event") == "error" or payload.get("status") == "failed":
        job["status"] = "failed"
        job["completed_at"] = now
        logger.error(
            "Setup job failed: %s",
            event["message"] or "unknown error",
            extra={"job_id": job_id, "kind": job.get("kind", "")},
        )
    elif payload.get("event") == "canceled" or payload.get("status") == "canceled":
        job["status"] = "canceled"
        job["completed_at"] = now
        logger.info(
            "Setup job canceled",
            extra={"job_id": job_id, "kind": job.get("kind", "")},
        )
    else:
        job["status"] = "running"
    _write_job(job)
    return event


def _interrupted_message(kind: str) -> str:
    """The wording an orphaned job gets, by kind: a schedule run is not retried by hand."""
    if kind == "schedule-run":
        return (
            "Server restarted or disconnected while this run was in flight; "
            "the schedule will run again at its next interval."
        )
    return (
        "Server restarted or disconnected while this job was running. "
        "Retry to resume setup."
    )


def _canceled_message(kind: str, *, by_user: bool) -> str:
    """The wording a canceled job gets, by kind and by who canceled it.

    ``by_user`` is the ``cancel_requested`` flag (``POST /v1/jobs/{id}/cancel``);
    otherwise the consumer task itself was cancelled — the server stopping
    while a schedule run was in flight — and nobody clicked anything, so a
    schedule run says so and names what happens next.
    """
    if kind == "schedule-run":
        if by_user:
            return "Run canceled by user."
        return (
            "Run canceled before it finished (the server stopped); "
            "the schedule will run again at its next interval."
        )
    return "Install job canceled by user."


def reconcile_job(
    job: dict[str, Any],
    *,
    home_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Mark orphaned running jobs from previous server sessions as interrupted."""
    if job.get("status") not in RUNNING_STATUSES:
        return job
    if job["id"] in _TASKS and not _TASKS[job["id"]].done():
        return job
    job["status"] = "interrupted"
    job["message"] = _interrupted_message(str(job.get("kind", "")))
    job["updated_at"] = _now()
    job["completed_at"] = job["updated_at"]
    logger.warning(
        "Setup job marked interrupted after server restart",
        extra={"job_id": job.get("id", ""), "kind": job.get("kind", "")},
    )
    return _write_job(job, home_dir)


async def _consume_job_events(
    job_id: str,
    source_factory: Callable[[], AsyncIterator[dict[str, Any]]],
) -> None:
    kind = ""
    try:
        job = load_job(job_id, reconcile=False, include_recent_events=False)
        kind = str(job.get("kind", "") or "")
        job["status"] = "running"
        job["started_at"] = job.get("started_at") or _now()
        job["message"] = "Starting"
        job["updated_at"] = job["started_at"]
        _write_job(job)

        saw_terminal = False
        source = source_factory()
        try:
            async for payload in source:
                current = load_job(job_id, reconcile=False, include_recent_events=False)
                if current.get("cancel_requested"):
                    if payload.get("event") == "canceled":
                        # The source saw the flag itself and says where it stopped (the
                        # playbook engine's stopped_after_step, receipt_path and audit):
                        # keep its account rather than the generic line.
                        append_event(job_id, {**payload, "status": "canceled"})
                    else:
                        append_event(
                            job_id,
                            {
                                "event": "canceled",
                                "status": "canceled",
                                "message": _canceled_message(kind, by_user=True),
                            },
                        )
                    saw_terminal = True
                    break
                append_event(job_id, payload)
                if payload.get("event") in {"complete", "error", "canceled"}:
                    saw_terminal = True
                    break
        finally:
            # Close the source here, not when the garbage collector gets to it:
            # an async generator left at a yield runs its cleanup (the playbook
            # engine writes its receipt there) before the job is marked finished.
            aclose = getattr(source, "aclose", None)
            if aclose is not None:
                await aclose()

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
        # The consumer task was cancelled: by ``cancel_job`` (the user asked —
        # ``cancel_requested`` is set first) or by the process itself (the
        # lifespan stopping the schedule runner, Ctrl+C on `nvh schedule
        # start`), which is not a user click and says so for a schedule run.
        by_user = False
        try:
            by_user = bool(load_job(job_id, reconcile=False, include_recent_events=False).get("cancel_requested"))
        except Exception:  # the record is gone; the generic wording is the honest one
            pass
        append_event(
            job_id,
            {
                "event": "canceled",
                "status": "canceled",
                "message": _canceled_message(kind, by_user=by_user),
            },
        )
        raise
    except Exception as exc:
        logger.exception(
            "Setup job crashed",
            extra={"job_id": job_id},
        )
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


def start_job_with_id(
    *,
    kind: str,
    title: str,
    request: dict[str, Any] | None,
    source_factory: Callable[[str], AsyncIterator[dict[str, Any]]],
) -> dict[str, Any]:
    """:func:`start_job` for a source that needs its own job id.

    The factory receives the new job's id before the source starts — the
    schedule runner records it on the schedule (``mark_run(job_id=...)``)
    from inside the source. Everything else (the record, the consumer task,
    cancellation) is ``start_job``.
    """
    job = create_job(kind=kind, title=title, request=request)
    job_id = str(job["id"])
    task = asyncio.create_task(_consume_job_events(job_id, lambda: source_factory(job_id)))
    _TASKS[job_id] = task
    return load_job(job_id, reconcile=False)


def cancel_job(job_id: str) -> dict[str, Any]:
    """Request cancellation for a running job."""
    job = load_job(job_id, reconcile=False, include_recent_events=False)
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
                "message": _canceled_message(str(job.get("kind", "") or ""), by_user=True),
            },
        )
    return load_job(job_id, reconcile=False)
